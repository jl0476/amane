"""测试裁剪策略中立面: 策略解析、默认态判定、cv2 可用性访问器与 Face 检测执行 (检测先于键构造)."""

import shutil
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
from PIL import Image, ImageDraw
from structlog.testing import capture_logs
from structlog.types import EventDict

from amane.enums import CropStrategy
from amane.media import crop as crop_mod
from amane.media import derived_locator, optional_cv2
from amane.media.crop import (
    CropPlan,
    acquire_face_crop,
    pick_face_box,
    resolve_crop_plan,
    resolve_face_execution,
)
from amane.parsing import ContentType

# 基线尺寸: thumb 为标准封面 (800, 538); 候选高 430, b/h ≈ 0.799 < 0.9, 命中裁剪路径.
_BASE_THUMB = (800, 538)
_CROP_CANDIDATE = (379, 430)

# ---------------------------------------------------------------- Face (检测先于键构造)

# 含人脸样例: opencv/opencv 4.x samples/data/lena.jpg (512x512), YuNet 检出框约 (207,182,145,206).
_FACE_FIXTURE = Path(__file__).parent / "fixtures" / "yunet_face.jpg"
_RATIO = 0.7
_DEGRADE_EVENT = "face crop degraded to right"


def _face_plan() -> CropPlan:
    """Face 请求的基线 plan: 有码类型请求 face, 无 poster 候选 → executed=FACE, legacy 比例 0.7."""
    return resolve_crop_plan(
        ContentType.CENSORED,
        strategy_map={ContentType.CENSORED: CropStrategy.FACE},
        aspect="legacy",
        low_res_height=None,
        poster_ratio=_RATIO,
        skip_ratio=0.9,
        thumb_size=(512, 512),
        candidate_size=None,
    )


async def _acquire_face(
    store: Any,
    thumb_path: Path,
    src_url: str,
    plan: CropPlan | None = None,
) -> Any:
    return await acquire_face_crop(
        store,
        src_url,
        thumb_path,
        plan=plan if plan is not None else _face_plan(),
        aspect="legacy",
        jpeg_quality=95,
    )


def _reset_face_warn() -> None:
    """重置降级告警的每任务一次标记, 隔离各状态的告警计数."""
    crop_mod._face_degrade_warned.set(False)


def _degrade_warnings(logs: list[EventDict]) -> list[EventDict]:
    return [entry for entry in logs if entry.get("event") == _DEGRADE_EVENT]


class _ExplodingDetector:
    """探针可用而 detect 必抛的检测器替代实现: 模拟运行期检测异常."""

    def setInputSize(self, size: tuple[int, int]) -> None:
        return None

    def detect(self, image: np.ndarray) -> tuple[int, np.ndarray | None]:
        raise RuntimeError("detector exploded")


def _oracle_faces(img_path: Path) -> list[tuple[int, int, int, int]]:
    """测试内独立检测 (不经被测实现): 直接调用 YuNet, 用于推导期望裁剪窗."""
    cv2 = optional_cv2.module()
    assert cv2 is not None
    img = Image.open(img_path).convert("RGB")
    width, height = img.size
    bgr = np.asarray(img)[:, :, ::-1]
    detector = cv2.FaceDetectorYN.create(str(crop_mod._model_path()), "", (width, height), 0.6)
    detector.setInputSize((width, height))
    _, faces = detector.detect(bgr)
    if faces is None:
        return []
    return [(int(face[0]), int(face[1]), int(face[2]), int(face[3])) for face in faces]


def _expected_window(
    boxes: list[tuple[int, int, int, int]], image_size: tuple[int, int], ratio: float
) -> tuple[int, int, int, int]:
    """契约口径的期望裁剪窗: 面积最大者 (并列取最左), 窗口水平中心对齐人脸中心, 越界钳制, 高度取全图."""
    assert boxes, "样例图必须检出人脸"
    face = max(boxes, key=lambda box: (box[2] * box[3], -box[0]))
    width, height = image_size
    target_w = int(height * ratio)
    left = max(0, min(round(face[0] + face[2] / 2 - target_w / 2), width - target_w))
    return (left, 0, left + target_w, height)


def _assert_same_window(product: Path, source: Path, window: tuple[int, int, int, int]) -> None:
    """产物像素与源图期望窗口一致 (JPEG 重编码容忍小差异)."""
    produced = np.asarray(Image.open(product).convert("RGB"), dtype=np.int16)
    expected = np.asarray(Image.open(source).convert("RGB").crop(window), dtype=np.int16)
    assert produced.shape == expected.shape
    assert float(np.abs(produced - expected).mean()) < 3.0


def _resolve_plan(
    content_type: ContentType = ContentType.CENSORED,
    strategy_map: dict[ContentType, CropStrategy] | None = None,
    *,
    aspect: Literal["legacy", "emby", "full"] = "legacy",
    low_res_height: int | None = None,
    poster_ratio: float = 0.7,
    skip_ratio: float = 0.9,
    thumb_size: tuple[int, int] | None = _BASE_THUMB,
    candidate_size: tuple[int, int] | None = _CROP_CANDIDATE,
) -> CropPlan:
    """resolve_crop_plan 的测试封装: 基线为有码类型 + 裁剪路径 + legacy 比例."""
    return resolve_crop_plan(
        content_type,
        strategy_map={} if strategy_map is None else strategy_map,
        aspect=aspect,
        low_res_height=low_res_height,
        poster_ratio=poster_ratio,
        skip_ratio=skip_ratio,
        thumb_size=thumb_size,
        candidate_size=candidate_size,
    )


def test_crop_strategy_in_enums_neutral_layer() -> None:
    """CropStrategy 定义于 enums 中立层; 干净进程双序导入 config 与 media 均无环 (R2 PLAN-01 回归)."""
    assert issubclass(CropStrategy, StrEnum)
    assert CropStrategy.NONE.value == "none"
    assert CropStrategy.RIGHT.value == "right"
    assert CropStrategy.FACE.value == "face"

    for statement in ("import amane.config; import amane.media", "import amane.media; import amane.config"):
        result = subprocess.run([sys.executable, "-c", statement], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"双序导入失败: {statement}\n{result.stderr}"


def test_optional_cv2_accessor_two_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """available()/module() 两态经 monkeypatch 模块存在性验证: module() 返回 None 或模块本身."""
    present = types.ModuleType("cv2")
    monkeypatch.setattr(optional_cv2, "cv2", present)
    assert optional_cv2.available() is True
    assert optional_cv2.module() is present

    monkeypatch.setattr(optional_cv2, "cv2", None)
    assert optional_cv2.available() is False
    assert optional_cv2.module() is None


def test_resolve_crop_plan_defaults() -> None:
    """空策略映射回退默认矩阵: censored/uncensored=right, 其余 5 类型=none; requested == executed."""
    expected = {
        ContentType.CENSORED: CropStrategy.RIGHT,
        ContentType.UNCENSORED: CropStrategy.RIGHT,
        ContentType.CHINESE: CropStrategy.NONE,
        ContentType.WESTERN: CropStrategy.NONE,
        ContentType.FC2: CropStrategy.NONE,
        ContentType.AMATEUR: CropStrategy.NONE,
        ContentType.HENTAI: CropStrategy.NONE,
    }
    assert len(ContentType) == 7  # 默认矩阵必须覆盖全部内容类型
    for content_type, strategy in expected.items():
        plan = _resolve_plan(content_type)
        assert plan.requested is strategy, content_type
        assert plan.executed is strategy, content_type

    # 稀疏映射: 显式条目覆盖默认矩阵, 未列出的类型回退默认.
    plan = _resolve_plan(ContentType.HENTAI, strategy_map={ContentType.HENTAI: CropStrategy.RIGHT})
    assert plan.requested is CropStrategy.RIGHT
    assert plan.executed is CropStrategy.RIGHT


@pytest.mark.parametrize(
    ("aspect", "poster_ratio", "expected_ratio"),
    [
        pytest.param("legacy", 0.7, 0.7, id="legacy-default"),
        pytest.param("legacy", 0.75, 0.75, id="legacy-custom"),
        pytest.param("emby", 0.7, 2 / 3, id="emby"),
        pytest.param("full", 0.7, 2.12 / 3, id="full"),
    ],
)
def test_legacy_ratio_uses_poster_ratio(
    aspect: Literal["legacy", "emby", "full"], poster_ratio: float, expected_ratio: float
) -> None:
    """legacy 沿用传入的 poster_ratio (含自定义 0.75); emby=2/3; full=2.12/3."""
    plan = _resolve_plan(aspect=aspect, poster_ratio=poster_ratio)
    assert plan.ratio == expected_ratio


@pytest.mark.parametrize(
    ("thumb_size", "candidate_size", "expected_executed", "desc"),
    [
        ((800, 538), (379, 538), CropStrategy.NONE, "b/h=1.0 ≥ skip_ratio: 用候选原样"),
        ((800, 538), (379, 430), CropStrategy.RIGHT, "b/h≈0.799 < skip_ratio: 执行裁剪"),
        ((800, 538), None, CropStrategy.RIGHT, "无候选: 无比较对象, 用裁剪产物"),
        (None, (379, 430), CropStrategy.NONE, "无 thumb: 不裁, 用候选原样"),
        ((800, 0), (379, 430), CropStrategy.NONE, "退化高度: 不裁, 用候选原样"),
    ],
)
def test_low_res_none_uses_skip_ratio_short_circuit(
    thumb_size: tuple[int, int] | None,
    candidate_size: tuple[int, int] | None,
    expected_executed: CropStrategy,
    desc: str,
) -> None:
    """low_res_height=None 时判定采用 skip_ratio 短路 (现状算法); 短路只影响 executed, requested 保持请求值."""
    plan = _resolve_plan(thumb_size=thumb_size, candidate_size=candidate_size)
    assert plan.requested is CropStrategy.RIGHT, desc
    assert plan.executed is expected_executed, desc


def test_skip_ratio_is_explicit_input() -> None:
    """同一组尺寸 (b/h ≈ 0.929): skip_ratio=0.9 → 用候选原样; skip_ratio=0.95 → 执行裁剪 (可配置阈值, 不固定 0.9)."""
    plan_keep = _resolve_plan(skip_ratio=0.9, thumb_size=(800, 538), candidate_size=(379, 500))
    assert plan_keep.requested is CropStrategy.RIGHT
    assert plan_keep.executed is CropStrategy.NONE

    plan_crop = _resolve_plan(skip_ratio=0.95, thumb_size=(800, 538), candidate_size=(379, 500))
    assert plan_crop.executed is CropStrategy.RIGHT


# ---------------------------------------------------------------- 低清阈值模式 (任务 11, 规格 §3.3 判定顺序 5)

# 阈值模式判定矩阵: case → [(resolve 覆盖参数, 期望 executed)]; 「用候选原样」编码为 executed=NONE.
# 同 case 多行钉住双向语义; 基线 thumb (800, 538) / 候选 (379, 430) 即默认态裁剪路径 (b/h≈0.799 < 0.9).
_LOW_RES_CASES: dict[str, list[tuple[dict[str, Any], CropStrategy]]] = {
    # 候选 < 阈值, thumb 高 > 候选高 → 裁剪产物 (5b: 高度提升).
    "below_threshold_crop_wins": [
        ({"low_res_height": 800}, CropStrategy.RIGHT),
    ],
    # 候选 == 阈值 → 用候选原样 (5a 边界取 b ≥ T).
    "at_threshold_candidate_wins": [
        ({"low_res_height": 800, "candidate_size": (379, 800)}, CropStrategy.NONE),
    ],
    # 候选 > 阈值 → 用候选原样, 即使 skip_ratio 判定应裁 (b/h≈0.892 < 0.9).
    "above_threshold_candidate_wins": [
        ({"low_res_height": 450, "candidate_size": (379, 480)}, CropStrategy.NONE),
    ],
    # 两者都低于阈值 → 高者胜: h > b 裁剪, h < b 用候选.
    "both_below_higher_wins": [
        ({"low_res_height": 800, "thumb_size": (400, 300), "candidate_size": (250, 200)}, CropStrategy.RIGHT),
        ({"low_res_height": 800, "thumb_size": (400, 300), "candidate_size": (250, 350)}, CropStrategy.NONE),
    ],
    # 同高 → 用候选原样 (5b: h ≤ b, 裁剪无高度收益).
    "same_height_candidate_wins": [
        ({"low_res_height": 800, "candidate_size": (379, 538)}, CropStrategy.NONE),
    ],
    # 无候选 → 无比较对象, 用裁剪产物 (判定顺序 3).
    "no_candidate_crops": [
        ({"low_res_height": 800, "candidate_size": None}, CropStrategy.RIGHT),
    ],
    # thumb 尺寸不可读 → 不裁, 用候选原样 (判定顺序 2).
    "thumb_unreadable_no_crop": [
        ({"low_res_height": 800, "thumb_size": None}, CropStrategy.NONE),
    ],
    # 候选尺寸不可读 (thumb 可读) → 用裁剪产物; 期望与 thumb_unreadable_no_crop 相反.
    "candidate_unreadable_crops": [
        ({"low_res_height": 800, "candidate_size": None}, CropStrategy.RIGHT),
    ],
    # b/h < skip_ratio 但候选 ≥ 阈值 → 用候选 (阈值取代 skip_ratio 短路).
    "threshold_overrides_skip_ratio": [
        ({"low_res_height": 400}, CropStrategy.NONE),
    ],
    # 阈值模式下改 skip_ratio (0.9↔0.95) 选择不变: 质量底线两侧都不随 skip_ratio 翻转
    # (对照: 默认态下 b/h≈0.929 在 0.9/0.95 间翻转, 见 test_skip_ratio_is_explicit_input).
    "threshold_ignores_skip_ratio_value": [
        ({"low_res_height": 400, "skip_ratio": 0.9, "candidate_size": (379, 500)}, CropStrategy.NONE),
        ({"low_res_height": 400, "skip_ratio": 0.95, "candidate_size": (379, 500)}, CropStrategy.NONE),
        ({"low_res_height": 800, "skip_ratio": 0.9, "candidate_size": (379, 500)}, CropStrategy.RIGHT),
        ({"low_res_height": 800, "skip_ratio": 0.95, "candidate_size": (379, 500)}, CropStrategy.RIGHT),
    ],
    # low_res=None → skip_ratio 短路现状算法 (4a/4b, 逐字节).
    "default_none_matches_current": [
        ({"low_res_height": None, "skip_ratio": 0.9, "candidate_size": (379, 500)}, CropStrategy.NONE),
        ({"low_res_height": None, "skip_ratio": 0.95, "candidate_size": (379, 500)}, CropStrategy.RIGHT),
    ],
}


@pytest.mark.parametrize("case", list(_LOW_RES_CASES))
def test_low_res_decision_matrix(case: str) -> None:
    """低清阈值模式全边界判定矩阵 (规格 §3.3 判定顺序): 逐 case 断言 executed 编码的产物选择."""
    for resolve_kwargs, expected in _LOW_RES_CASES[case]:
        plan = _resolve_plan(**resolve_kwargs)
        assert plan.executed is expected, f"{case}: {resolve_kwargs}"
        assert plan.requested is CropStrategy.RIGHT, f"{case}: 判定不改变 requested"


def test_threshold_reachability_changes_selection() -> None:
    """可达性断言 (规格 §3.3 复审验收): b=500/h=538/skip_ratio=0.9 下仅改阈值即产生不同选择.

    T=None → 4a (b/h≈0.929 ≥ 0.9) 用候选; T=800 → 5b (b=500 < 800 且 h=538 > 500) 裁剪.
    """
    kwargs: dict[str, Any] = {"thumb_size": (800, 538), "candidate_size": (379, 500), "skip_ratio": 0.9}
    default_plan = _resolve_plan(low_res_height=None, **kwargs)
    threshold_plan = _resolve_plan(low_res_height=800, **kwargs)
    assert default_plan.executed is CropStrategy.NONE
    assert threshold_plan.executed is CropStrategy.RIGHT
    assert default_plan.executed is not threshold_plan.executed


# ---------------------------------------------------------------- Face 执行 (任务 7)


@pytest.mark.asyncio
async def test_face_detection_before_key_construction(
    resource_store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检测先于键构造的四态 (经真实 acquire_derived 核对 locator/meta):

    face 检出 → "face:*" 键; 无人脸 → "right:*" 键 (无 warning); 检测异常 → "right:*" 键 + warning;
    后端不可用 → "right:*" 键 + warning. face 键永不存储 right 降级产物; frozen plan 不被修改.
    """
    plan = _face_plan()
    src_url = "https://img.example.com/covers/face.jpg"
    plain_url = "https://img.example.com/covers/plain.jpg"

    # 流程前提: thumb 已下载到本地 (调用方保证), 这里以本地文件模拟.
    thumb = tmp_path / "thumb_face.jpg"
    shutil.copyfile(_FACE_FIXTURE, thumb)

    # 无人脸样例: 平滑渐变不含可检人脸.
    plain = tmp_path / "thumb_gradient.jpg"
    gradient = Image.new("RGB", (800, 538))
    draw = ImageDraw.Draw(gradient)
    for x in range(800):
        draw.line((x, 0, x, 538), fill=(x % 256, (x * 2) % 256, 128))
    gradient.save(plain, format="JPEG", quality=95)

    # 前置 (非法用例): 非 Face 请求不接受检测固化 — 流程只服务 Face 请求.
    right_plan = replace(plan, requested=CropStrategy.RIGHT, executed=CropStrategy.RIGHT)
    with pytest.raises(ValueError, match="FACE"):
        resolve_face_execution(right_plan, thumb)

    _reset_face_warn()
    with capture_logs() as logs:
        # 态 1: face 检出 → face 键.
        face_res = await _acquire_face(resource_store, thumb, src_url, plan)
        assert face_res is not None
        assert face_res.meta == {"op": "crop", "src": src_url, "args": "face:legacy:0.7000"}
        assert face_res.url == derived_locator(src_url, "crop", "face:legacy:0.7000")
        assert _degrade_warnings(logs) == []

        # 态 2: 无人脸 → right 键 (预期回退, 无 warning).
        res = await _acquire_face(resource_store, plain, plain_url, plan)
        assert res is not None
        assert res.meta == {"op": "crop", "src": plain_url, "args": "right:legacy:0.7000"}
        assert res.url == derived_locator(plain_url, "crop", "right:legacy:0.7000")
        assert _degrade_warnings(logs) == []
    face_key_of_plain = derived_locator(plain_url, "crop", "face:legacy:0.7000")
    assert await resource_store.get_by_url(face_key_of_plain) is None

    # 态 3: 运行期检测异常 → right 键 + warning (每任务一次标记重置后重新计数).
    _reset_face_warn()
    monkeypatch.setattr(crop_mod, "_load_detector", lambda: _ExplodingDetector())
    with capture_logs() as logs:
        res = await _acquire_face(resource_store, thumb, src_url, plan)
        assert res is not None
        assert res.meta == {"op": "crop", "src": src_url, "args": "right:legacy:0.7000"}
        assert len(_degrade_warnings(logs)) == 1

    # 态 4: 后端不可用 (cv2 缺失) → right 键 + warning; 原 plan 仍是 FACE (固化产生新实例).
    _reset_face_warn()
    monkeypatch.setattr(optional_cv2, "cv2", None)
    with capture_logs() as logs:
        res = await _acquire_face(resource_store, thumb, src_url, plan)
        assert res is not None
        assert res.meta == {"op": "crop", "src": src_url, "args": "right:legacy:0.7000"}
        assert len(_degrade_warnings(logs)) == 1
    assert plan.executed is CropStrategy.FACE
    # face 键未被降级产物覆盖: 态 1 的 face 产物字节仍在.
    face_record = await resource_store.get_by_url(derived_locator(src_url, "crop", "face:legacy:0.7000"))
    assert face_record is not None
    assert face_record.content_hash == face_res.content_hash


@pytest.mark.asyncio
async def test_face_crop_aligns_face_window(resource_store: Any, tmp_path: Path) -> None:
    """含人脸样例图 → 裁剪窗水平对齐人脸中心; 人脸贴左缘 / 右缘时越界钳制."""
    plan = _face_plan()

    # 中央: fixture 原样; 左钳制: 裁去 fixture 左侧 120px 使人脸中心落入窗口半径内; 右钳制: 裁去右侧.
    center = tmp_path / "center.jpg"
    shutil.copyfile(_FACE_FIXTURE, center)
    left_clamped = tmp_path / "left_clamped.jpg"
    Image.open(_FACE_FIXTURE).convert("RGB").crop((120, 0, 512, 512)).save(left_clamped, format="JPEG", quality=95)
    right_clamped = tmp_path / "right_clamped.jpg"
    Image.open(_FACE_FIXTURE).convert("RGB").crop((0, 0, 392, 512)).save(right_clamped, format="JPEG", quality=95)
    cases = [("center", center), ("left-clamp", left_clamped), ("right-clamp", right_clamped)]

    _reset_face_warn()
    center_product: Path | None = None
    center_height = 0
    with capture_logs() as logs:
        for i, (desc, thumb) in enumerate(cases):
            url = f"https://img.example.com/cases/{i}.jpg"
            res = await _acquire_face(resource_store, thumb, url, plan)
            assert res is not None, desc
            assert res.meta is not None and res.meta["args"] == "face:legacy:0.7000", desc

            width, height = Image.open(thumb).size
            expected = _expected_window(_oracle_faces(thumb), (width, height), plan.ratio)
            left = expected[0]
            if desc == "center":
                assert 0 < left < width - int(height * plan.ratio), desc  # 未钳制, 窗口真正随人脸移动
                center_product = resource_store.full_path(res)
                center_height = height
            elif desc == "left-clamp":
                assert left == 0, desc
            else:
                assert left == width - int(height * plan.ratio), desc
            _assert_same_window(resource_store.full_path(res), thumb, expected)

    assert center_product is not None
    # 对齐的行为面: 无钳制时人脸中心落在产物窗口中心 (允许 JPEG 重编码引起 ±1px 检测抖动).
    product_faces = _oracle_faces(center_product)
    assert product_faces, "产物应保留人脸"
    face_cx = product_faces[0][0] + product_faces[0][2] / 2
    target_w = int(center_height * plan.ratio)
    assert abs(face_cx - target_w / 2) <= 2
    assert _degrade_warnings(logs) == []


@pytest.mark.asyncio
async def test_multi_face_largest_area_leftmost_tie(resource_store: Any, tmp_path: Path) -> None:
    """多人脸取面积最大者 (真实双脸画布); 并列取最左 (纯选择函数表测试 — 编码噪声使真实框无法严格并列)."""
    plan = _face_plan()
    lena = Image.open(_FACE_FIXTURE).convert("RGB")
    face_src = lena.crop((148, 123, 414, 450))
    canvas = Image.new("RGB", (900, 600), (40, 40, 48))
    canvas.paste(face_src.resize((200, 246)), (60, 120))  # 左侧小脸
    canvas.paste(face_src.resize((320, 394)), (480, 100))  # 右侧大脸
    canvas_path = tmp_path / "multi_face.jpg"
    canvas.save(canvas_path, format="JPEG", quality=95)

    _reset_face_warn()
    with capture_logs() as logs:
        res = await _acquire_face(resource_store, canvas_path, "https://img.example.com/covers/multi.jpg", plan)
        assert res is not None
        assert res.meta is not None and res.meta["args"] == "face:legacy:0.7000"
        assert _degrade_warnings(logs) == []

    boxes = _oracle_faces(canvas_path)
    assert len(boxes) >= 2, "样例画布必须检出两张人脸"
    expected = _expected_window(boxes, canvas.size, plan.ratio)
    largest = max(boxes, key=lambda box: box[2] * box[3])
    assert largest[0] > 400  # 面积最大者是被贴在右侧的大脸: 选窗必须随它而非最左脸
    _assert_same_window(resource_store.full_path(res), canvas_path, expected)

    # 并列取最左; 面积大者胜, 不论位置.
    assert pick_face_box([(200, 0, 100, 100), (100, 5, 100, 100), (0, 9, 100, 100)]) == (0, 9, 100, 100)
    assert pick_face_box([(0, 0, 50, 50), (300, 0, 100, 100)]) == (300, 0, 100, 100)
    assert pick_face_box([(7, 8, 9, 10)]) == (7, 8, 9, 10)
    assert pick_face_box([]) is None


@pytest.mark.asyncio
async def test_backend_unavailable_probe_degrades_right(
    resource_store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cv2 缺失与模型加载失败都在键构造前探出 → right 键缓存; warning 每任务一次; face 键不落产物."""
    thumb = tmp_path / "thumb.jpg"
    shutil.copyfile(_FACE_FIXTURE, thumb)
    url_a = "https://img.example.com/covers/degrade-a.jpg"

    producer_calls: list[Path] = []
    real_crop_poster = crop_mod.crop_poster

    def counting_crop_poster(src: Path, dest: Path, **kwargs: Any) -> bool:
        producer_calls.append(dest)
        return real_crop_poster(src, dest, **kwargs)

    monkeypatch.setattr(crop_mod, "crop_poster", counting_crop_poster)
    real_cv2 = optional_cv2.cv2
    face_key_a = derived_locator(url_a, "crop", "face:legacy:0.7000")

    # 态 A: cv2 未安装.
    _reset_face_warn()
    monkeypatch.setattr(optional_cv2, "cv2", None)
    with capture_logs() as logs:
        res = await _acquire_face(resource_store, thumb, url_a)
        assert res is not None
        assert res.meta == {"op": "crop", "src": url_a, "args": "right:legacy:0.7000"}
        assert len(_degrade_warnings(logs)) == 1
        assert len(producer_calls) == 1

        # 同任务第二次调用: 命中 right 键缓存, 不再告警也不再重跑 producer.
        again = await _acquire_face(resource_store, thumb, url_a)
        assert again is not None and again.url == res.url
        assert len(_degrade_warnings(logs)) == 1
        assert len(producer_calls) == 1
    assert await resource_store.get_by_url(face_key_a) is None

    # 态 B: cv2 在而模型不可加载 (探针在键构造前执行, 新 src 避开态 A 的缓存).
    _reset_face_warn()
    monkeypatch.setattr(optional_cv2, "cv2", real_cv2)
    monkeypatch.setattr(crop_mod, "_model_path", lambda: tmp_path / "missing.onnx")
    crop_mod._load_detector.cache_clear()
    try:
        with capture_logs() as logs:
            res_b = await _acquire_face(resource_store, thumb, "https://img.example.com/covers/degrade-b.jpg")
            assert res_b is not None
            assert res_b.meta == {
                "op": "crop",
                "src": "https://img.example.com/covers/degrade-b.jpg",
                "args": "right:legacy:0.7000",
            }
            assert len(_degrade_warnings(logs)) == 1
    finally:
        crop_mod._load_detector.cache_clear()
    assert (
        await resource_store.get_by_url(
            derived_locator("https://img.example.com/covers/degrade-b.jpg", "crop", "face:legacy:0.7000")
        )
        is None
    )


@pytest.mark.asyncio
async def test_runtime_detection_exception_degrades_right_key(
    resource_store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行期检测异常 → 降级 right + 每任务一次 warning, 产物仍落 right 键 (设计 §4: 不中断, 不回退原候选)."""
    thumb = tmp_path / "thumb.jpg"
    shutil.copyfile(_FACE_FIXTURE, thumb)
    url = "https://img.example.com/covers/exc.jpg"
    monkeypatch.setattr(crop_mod, "_load_detector", lambda: _ExplodingDetector())

    producer_calls: list[Path] = []
    real_crop_poster = crop_mod.crop_poster

    def counting_crop_poster(src: Path, dest: Path, **kwargs: Any) -> bool:
        producer_calls.append(dest)
        return real_crop_poster(src, dest, **kwargs)

    monkeypatch.setattr(crop_mod, "crop_poster", counting_crop_poster)

    _reset_face_warn()
    with capture_logs() as logs:
        res = await _acquire_face(resource_store, thumb, url)
        assert res is not None
        assert res.meta == {"op": "crop", "src": url, "args": "right:legacy:0.7000"}
        assert len(_degrade_warnings(logs)) == 1

        # 产物确实是右窗裁剪.
        width, height = Image.open(thumb).size
        _assert_same_window(resource_store.full_path(res), thumb, (width - int(height * _RATIO), 0, width, height))

        # 缓存幂等: 第二次调用命中 right 键, 不重跑 producer 也不再告警.
        again = await _acquire_face(resource_store, thumb, url)
        assert again is not None and again.url == res.url
        assert len(producer_calls) == 1
        assert len(_degrade_warnings(logs)) == 1
    assert await resource_store.get_by_url(derived_locator(url, "crop", "face:legacy:0.7000")) is None


# ---------------------------------------------------------------- 并发检测隔离 (IMPL-08)

# 并发用例的图尺寸: 互不相同, 共享实例的 setInputSize 会互相覆盖输入尺寸.
_CONCURRENT_SIZES = [(800, 538), (512, 512), (600, 900), (1280, 720)]


def _concurrent_thumbs(tmp_path: Path) -> list[tuple[tuple[int, int], Path]]:
    """按 :data:`_CONCURRENT_SIZES` 构造同源不同尺寸的样例图 (含人脸, 各尺寸均能检出)."""
    source = Image.open(_FACE_FIXTURE).convert("RGB")
    thumbs: list[tuple[tuple[int, int], Path]] = []
    for size in _CONCURRENT_SIZES:
        path = tmp_path / f"concurrent_{size[0]}x{size[1]}.jpg"
        source.resize(size).save(path, format="JPEG", quality=95)
        thumbs.append((size, path))
    return thumbs


def _detect_outcome(
    item: tuple[tuple[int, int], Path],
) -> tuple[tuple[int, int], list[tuple[int, int, int, int]], str | None]:
    """并发用例的单个检测结果: 异常收敛为消息, 便于逐项断言报告错配而非整体中断."""
    try:
        size, boxes = crop_mod._detect_faces(item[1])
    except Exception as exc:
        return item[0], [], f"{type(exc).__name__}: {exc}"
    return size, boxes, None


def test_concurrent_detection_results_match_own_input(tmp_path: Path) -> None:
    """并发检测: 每个结果必须对应自身输入图 (IMPL-08).

    共享检测器实例在并发下互相覆盖输入尺寸与推理状态, 表现为检出框错配或检测异常.
    预热检测器复现生产路径 (实例创建一次后全程复用), 再并发提交不同尺寸的图.
    """
    thumbs = _concurrent_thumbs(tmp_path)
    baseline = {size: _oracle_faces(path) for size, path in thumbs}
    assert all(baseline.values()), "每个尺寸的样例图必须各自检出人脸"

    crop_mod._load_detector.cache_clear()
    crop_mod._detect_faces(thumbs[0][1])  # 预热: 生产路径中实例创建一次后全程复用

    with ThreadPoolExecutor(max_workers=len(thumbs)) as pool:
        outcomes = list(pool.map(_detect_outcome, thumbs))

    for (size, _), (returned_size, boxes, error) in zip(thumbs, outcomes, strict=True):
        assert error is None, f"{size} 检测异常: {error}"
        assert returned_size == size, "返回尺寸必须对应自身输入"
        assert boxes == baseline[size], f"检出框错配: {size} 得到 {boxes}, 期望 {baseline[size]}"


def test_concurrent_cold_start_creates_single_detector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并发冷启动只创建一个检测器实例 (IMPL-08).

    ``functools.cache`` 不合并并发首次调用: 多个线程同时未命中缓存会各建一个实例,
    造成重复的模型加载与额外的内存占用. 创建须在互斥区内完成.
    """
    thumbs = _concurrent_thumbs(tmp_path)
    real_cv2 = optional_cv2.module()
    assert real_cv2 is not None

    created: list[object] = []
    real_create = real_cv2.FaceDetectorYN.create

    def counting_create(*args: Any, **kwargs: Any) -> Any:
        detector = real_create(*args, **kwargs)
        created.append(detector)
        return detector

    # 只替换 FaceDetectorYN.create, 其余 cv2 成员 (utils.logging 等) 仍走真实模块.
    proxy_cv2 = types.SimpleNamespace(
        FaceDetectorYN=types.SimpleNamespace(create=counting_create),
        utils=real_cv2.utils,
    )
    monkeypatch.setattr(optional_cv2, "cv2", proxy_cv2)
    crop_mod._load_detector.cache_clear()
    try:
        with ThreadPoolExecutor(max_workers=len(thumbs)) as pool:
            outcomes = list(pool.map(_detect_outcome, thumbs))
    finally:
        crop_mod._load_detector.cache_clear()

    assert all(error is None for _, _, error in outcomes), "并发冷启动不得出现检测异常"
    assert len(created) == 1, f"并发冷启动应只创建一个实例, 实际 {len(created)} 个"
