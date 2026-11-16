"""海报裁剪策略中立面: 策略解析与产物选择判定的唯一定义, 刮削物化与整理期重选双入口共用 (设计规格 §3.2/§3.3)."""

import functools
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
import structlog
from PIL import Image

from ..enums import CropStrategy
from ..parsing import ContentType
from . import optional_cv2
from .images import crop_poster

if TYPE_CHECKING:
    from ..db.models import Resource
    from .resource_store import ResourceStore

logger = structlog.get_logger()

CropAspect = Literal["legacy", "emby", "full"]

# crop_aspect 的固定产物比例: emby 海报 2:3, full 2.12:3.
_EMBY_RATIO = 2 / 3
_FULL_RATIO = 2.12 / 3

# 默认策略矩阵, strategy_map 缺键时回退: 仅封面为横版夹页的内容类型执行右侧裁剪, 其余类型保持候选原样.
_DEFAULT_STRATEGIES: dict[ContentType, CropStrategy] = {
    ContentType.CENSORED: CropStrategy.RIGHT,
    ContentType.UNCENSORED: CropStrategy.RIGHT,
}


@dataclass(frozen=True)
class CropPlan:
    """单次裁剪的完整决策: 请求策略、实际执行与产物比例."""

    requested: CropStrategy
    """配置请求的策略."""

    executed: CropStrategy
    """实际执行的像素策略; NONE 表示候选原样入选, 不执行像素裁剪."""

    ratio: float
    """产物宽高比 (w/h), 由 crop_aspect 解析."""

    low_res_height: int | None
    """候选质量底线阈值; None 表示默认态判定 (skip_ratio 短路)."""


def _resolve_ratio(aspect: CropAspect, poster_ratio: float) -> float:
    """legacy 沿用传入的 poster_ratio (含用户自定义值); emby 与 full 为固定比例."""
    if aspect == "legacy":
        return poster_ratio
    if aspect == "full":
        return _FULL_RATIO
    return _EMBY_RATIO


def _select_candidate(
    thumb_size: tuple[int, int] | None,
    candidate_size: tuple[int, int] | None,
    *,
    skip_ratio: float,
    low_res_height: int | None,
) -> bool:
    """候选选择 (规格 §3.3 判定顺序 2–5): True 表示入选候选原样. 尺寸口径与 images.should_crop_poster 一致.

    low_res_height=None 为默认态 (4a/4b): 候选 b/h ≥ skip_ratio 用候选, 否则裁剪.
    配置阈值 T 时进入阈值模式并取代 skip_ratio 短路 (5a/5b): 候选 b ≥ T 用候选 (质量底线达标);
    b < T 时仅 thumb 高于候选 (h > b, 裁剪有高度提升) 才裁剪, 否则用候选.
    阈值非正值使 b ≥ T 恒成立, 恒用候选.
    """
    if thumb_size is None:
        return True
    if candidate_size is None:
        return False
    thumb_height = thumb_size[1]
    candidate_height = candidate_size[1]
    if thumb_height <= 0 or candidate_height <= 0:
        return True
    if low_res_height is not None:
        if candidate_height >= low_res_height:
            return True
        return thumb_height <= candidate_height
    return candidate_height / thumb_height >= skip_ratio


def resolve_crop_plan(
    content_type: ContentType,
    *,
    strategy_map: Mapping[ContentType, CropStrategy],
    aspect: CropAspect,
    low_res_height: int | None,
    poster_ratio: float,
    skip_ratio: float,
    thumb_size: tuple[int, int] | None,
    candidate_size: tuple[int, int] | None,
) -> CropPlan:
    """解析单次裁剪决策 (规格 §3.3 判定顺序): 策略读取 strategy_map, 缺键回退默认矩阵 (censored/uncensored=right, 其余=none).

    产物选择: 无 thumb → 候选原样 (executed=NONE); 无候选 → 执行裁剪 (executed=requested).
    low_res_height=None (默认) 按 skip_ratio 短路: 候选 b/h ≥ skip_ratio → 候选原样, 否则裁剪.
    配置阈值时阈值模式取代短路: 候选高度 ≥ 阈值用候选原样 (即使 b/h < skip_ratio), 否则仅 thumb
    高于候选才裁剪. skip_ratio 为既有可配置阈值 (0.5–1.0), 由调用方显式传入.
    """
    requested = strategy_map.get(content_type, _DEFAULT_STRATEGIES.get(content_type, CropStrategy.NONE))
    if requested is CropStrategy.NONE or _select_candidate(
        thumb_size, candidate_size, skip_ratio=skip_ratio, low_res_height=low_res_height
    ):
        executed = CropStrategy.NONE
    else:
        executed = requested
    return CropPlan(
        requested=requested,
        executed=executed,
        ratio=_resolve_ratio(aspect, poster_ratio),
        low_res_height=low_res_height,
    )


# ---------------------------------------------------------------- Face 执行: 检测先于键构造 (设计规格 §3.2/§4)

_FACE_MODEL_RELPATH = Path("models") / "face_detection_yunet_2023mar.onnx"
_FACE_SCORE_THRESHOLD = 0.6

# 降级告警每任务一次: 调度器在独立 contextvars 中执行每个任务, 标记随任务上下文隔离.
_face_degrade_warned: ContextVar[bool] = ContextVar("face_crop_degrade_warned", default=False)


class _FaceDetector(Protocol):
    """YuNet 检测器被使用的表面; 实现对象由 cv2.FaceDetectorYN.create 产出, 静态类型不依赖 cv2."""

    def setInputSize(self, size: tuple[int, int]) -> None: ...

    def detect(self, image: np.ndarray) -> tuple[int, np.ndarray | None]: ...


# YuNet 实例持有可变状态 (输入尺寸与 dnn::Net 的推理缓冲), setInputSize 与 detect 之间存在状态依赖:
# 后调用的 setInputSize 会让先调用的 detect 用错尺寸, 并发的 net.setInput/forward 还会互相覆盖推理结果.
# 因此共享实例上的 "设尺寸 + 检测" 必须互斥, 保证每个结果严格对应其输入图.
# 创建纳入同一互斥区: functools.cache 不合并并发首次调用 (多线程会各建一个实例), 放进互斥区后进程内只建一个.
_detector_lock = threading.Lock()


def _model_path() -> Path:
    return Path(__file__).parent / _FACE_MODEL_RELPATH


@functools.cache
def _load_detector() -> _FaceDetector | None:
    """惰性创建 YuNet 检测器: 首次使用时执行 create, 结果进程内复用; 失败态同样缓存, 不再重试.

    并发调用方须持 :data:`_detector_lock` 调用, 不得直接调用.
    """
    cv2 = optional_cv2.module()
    if cv2 is None:
        return None
    try:
        # cv2 5.0 的新图引擎在 create 时输出 net_impl_backend WARN, 级别调整为 ERROR 保证日志输出干净.
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        return cv2.FaceDetectorYN.create(str(_model_path()), "", (0, 0), _FACE_SCORE_THRESHOLD)
    except Exception as exc:
        logger.debug("face detector create failed", error=str(exc))
        return None


def _backend_failure() -> str | None:
    """Face 后端探针 (须在派生键构造之前执行): 可用返回 None, 否则返回不可用原因."""
    if not optional_cv2.available():
        return "cv2 not installed"
    with _detector_lock:
        if _load_detector() is None:
            return "yunet model load failed"
    return None


def _warn_face_degrade_once(reason: str) -> None:
    """Face 降级告警每任务一次."""
    if _face_degrade_warned.get():
        return
    logger.warning("face crop degraded to right", reason=reason)
    _face_degrade_warned.set(True)


def _detect_faces(thumb_path: Path) -> tuple[tuple[int, int], list[tuple[int, int, int, int]]]:
    """YuNet 检测图内全部人脸框. 坐标为原始像素 (不做 EXIF 转正), 与裁剪窗同一坐标系; 异常向上传播.

    取实例与 "设尺寸 + 检测" 在同一互斥区内完成: 实例状态可变, 并发会互相覆盖尺寸与推理结果.
    """
    with Image.open(thumb_path) as img:
        rgb = img.convert("RGB")
        size = rgb.size
        bgr = np.asarray(rgb)[:, :, ::-1]
    with _detector_lock:
        detector = _load_detector()
        if detector is None:
            raise RuntimeError("face backend unavailable")
        detector.setInputSize(size)
        _, faces = detector.detect(bgr)
    if faces is None:
        return size, []
    return size, [(int(face[0]), int(face[1]), int(face[2]), int(face[3])) for face in faces]


def pick_face_box(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    """多人脸选择 (决策 10): 面积最大者, 并列取最左; 无框返回 None."""
    if not boxes:
        return None
    return max(boxes, key=lambda box: (box[2] * box[3], -box[0]))


def _face_window(
    face: tuple[int, int, int, int], image_size: tuple[int, int], *, ratio: float
) -> tuple[int, int, int, int]:
    """Face 裁剪窗: 宽 = 高度 × ratio, 水平中心对齐人脸中心, 越界钳制, 高度取全图."""
    width, height = image_size
    target_w = int(height * ratio)
    if target_w >= width:
        return (0, 0, width, height)
    left = round(face[0] + face[2] / 2 - target_w / 2)
    left = max(0, min(left, width - target_w))
    return (left, 0, left + target_w, height)


@dataclass(frozen=True)
class FaceExecution:
    """Face 请求经检测固化的结果: executed 已按检测定案的新 plan 与预计算裁剪窗 (right 降级时无窗)."""

    plan: CropPlan
    box: tuple[int, int, int, int] | None


def resolve_face_execution(plan: CropPlan, thumb_path: Path) -> FaceExecution:
    """检测先于键构造 (R2 PLAN-05): 探针与检测都在派生键构造之前执行, 按检测结果固化 executed 与裁剪窗.

    检出 → executed=FACE + 预计算窗; 无人脸 → executed=RIGHT (预期回退, 无告警);
    后端不可用或检测异常 → executed=RIGHT + 每任务一次告警. frozen 实例不修改, 固化经 replace 产生新 plan.
    """
    if plan.executed is not CropStrategy.FACE:
        raise ValueError(f"Face 检测固化只处理 FACE 请求, 实际 executed={plan.executed.value}")
    failure = _backend_failure()
    if failure is not None:
        _warn_face_degrade_once(failure)
        return FaceExecution(plan=replace(plan, executed=CropStrategy.RIGHT), box=None)
    try:
        size, boxes = _detect_faces(thumb_path)
    except Exception as exc:
        _warn_face_degrade_once(f"detection failed: {exc}")
        return FaceExecution(plan=replace(plan, executed=CropStrategy.RIGHT), box=None)
    face = pick_face_box(boxes)
    if face is None:
        return FaceExecution(plan=replace(plan, executed=CropStrategy.RIGHT), box=None)
    return FaceExecution(plan=plan, box=_face_window(face, size, ratio=plan.ratio))


def format_crop_args(executed: CropStrategy, aspect: CropAspect, ratio: float) -> str:
    """派生键 args (决策 15): 按实际执行策略编码 ``{executed}:{aspect}:{ratio:.4f}``; Face 降级落 right 键."""
    return f"{executed.value}:{aspect}:{ratio:.4f}"


def crop_face_window(src_path: Path, dest_path: Path, box: tuple[int, int, int, int], *, jpeg_quality: int) -> bool:
    """按预计算窗裁剪落盘, 刮削物化 (acquire_face_crop) 与整理兜底 (handlers/file.py) 双入口共用.

    box 与检测同一坐标系 (原始像素, 不做 EXIF 转正).
    """
    try:
        with Image.open(src_path) as img:
            cropped = img.crop(box)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            cropped.convert("RGB").save(dest_path, format="JPEG", quality=jpeg_quality)
        return True
    except Exception as exc:
        logger.warning("face window crop failed", path=str(src_path), error=str(exc))
        return False


async def acquire_face_crop(
    store: ResourceStore,
    thumb_src: str,
    thumb_path: Path,
    *,
    plan: CropPlan,
    aspect: CropAspect,
    jpeg_quality: int,
) -> Resource | None:
    """Face 请求的派生产物获取 (检测先于键构造). ``thumb_path`` 须已在本地, 由调用方保证.

    流程: 固定源快照 → 检测固化 executed → 以固化 executed 构造派生键 → acquire_derived. face 检出时
    producer 闭包使用预计算窗裁剪, 缓存 miss 重跑同窗, 结果确定性一致; 降级产物落 right 键, face 键永不
    存储 right 产物.

    检测 / 裁剪 / 源版本标记都用同一份源快照: 源在检测之后被换新时, 产物仍按检测时的源裁剪, 版本标记
    也记检测时的源, 二者不会混用两个版本的源. 快照只是独占副本, 派生记录不引用它.

    快照取不到 (复制失败) 时放弃本轮派生: 降级为直接读源文件会产出「源 A 的检测框套在源 B 上」的
    错配产物, 而源版本标记记的是当时盘上的那份. 本轮无派生图, 调用方按「该产物本轮不可得」处理.
    """
    async with store.source_snapshot(thumb_path) as snapshot:
        if snapshot is None:
            logger.warning("face crop skipped", reason="source snapshot unavailable")
            return None

        execution = resolve_face_execution(plan, snapshot.path)
        args = format_crop_args(execution.plan.executed, aspect, execution.plan.ratio)
        if execution.box is not None:
            box = execution.box

            async def producer(dest: Path) -> bool:
                return crop_face_window(snapshot.path, dest, box, jpeg_quality=jpeg_quality)
        else:
            ratio = execution.plan.ratio

            async def producer(dest: Path) -> bool:
                return crop_poster(snapshot.path, dest, poster_ratio=ratio, jpeg_quality=jpeg_quality)

        return await store.acquire_derived(thumb_src, "crop", args, producer, src_snapshot=snapshot)
