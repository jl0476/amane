# 设计规格：图片处理链（MDC 迁移 P2）

> 日期: 2026-11-13
> 状态: 待审阅（第 1 版）
> 范围: MDC 能力迁移第二阶段 —— 分类化裁剪、人脸识别裁剪、下载策略、裁剪比例枚举、裁剪结果优先、剧照幕后场景
> 上游总纲: `docs/superpowers/mdc-roadmap.md`
> 前置: P0+P1 已合入 main（`efbf33c`），本文引用的覆盖层 / naming 分派 / 水印交互均以其落地形态为基线

## 0. 已定决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | OpenCV 依赖形态 | **可选 extra + 降级**：`[opencv]` extra；缺依赖时 Face 策略降级为 Right，任务日志明确提示 |
| 2 | 下载策略成本语义 | **策略可配，默认顺序优先**：`quality` 策略显式开启才付全候选流量代价 |
| 3 | 实现载体 | **策略 Protocol**：`CropPlan` / `DownloadStrategy` 轻量抽象，P4 插件化时接口已就位 |
| 4 | 高清海报源（日亚 / Tenhow） | 不实现，P4 插件能力（roadmap 既有结论） |
| 5 | `image_strict_mode` | 不实现——调研未捕获其行为语义，不臆造；记已知限制 |

## 0.1 头脑风暴输入记录

两项用户拍板：OpenCV 形态（可选 extra + 降级）、下载策略（可配 + 默认顺序优先）。
范围讨论确认：`extra_fanarts_behind_scenes` 并入既有 extrafanart 管线（非新建管线）；
`mdc_symbols_clean.txt` 级别的上游实现细节不作为本规格依据（只依据行为语义）。

## 1. 目标

把 MDC 的「按影片类型精准化图片处理」能力以原生 Python 实现进 Amane：裁剪策略按类型独立、
产物质量可择优、裁剪比例可配，全程保持既有行为不变（默认配置下产物逐字节不变）。

**成功标准**（可验证）：

1. 同一条目切换 `content_type` 后，裁剪行为随之变化（censored 裁右侧、策略 none 不裁）。
2. `thumb_download_strategy=quality` 时，多候选场景产出分辨率/体积最优者；`priority` 时行为与现状一致。
3. 装有 `[opencv]` extra 时 Face 策略按人脸对齐裁剪；未装时降级 Right 且日志明确。
4. `crop_aspect=full` 产物宽高比 ≈ 2.12/3；`emby` ≈ 2/3（现状）。
5. 默认配置下，整理产物的图片文件与升级前逐字节一致。
6. 新配置字段经覆盖层四层合成生效（库级覆盖 > 全局）。

**非目标**：

- 高清海报源（日亚 / Tenhow）→ P4 插件能力。
- `image_strict_mode` → 语义未调研，不实现（已知限制）。
- 水印逐类开关 → P3。
- 超分链路（`src/amane/sr/`）不动。

## 2. 现状与约束

### 2.1 现有裁剪链

- `src/amane/media/images.py::should_crop_poster` —— 尺寸判定：候选高宽比 b/h ≥ `skip_ratio`(0.9) 则跳过裁剪（候选已足够竖长，裁剪有错位风险）；无 thumb 尺寸 False、无 poster 候选 True
- `images.py::allows_poster_crop` —— **硬编码**仅 `CENSORED` / `UNCENSORED` 启用自动裁剪
- `images.py::crop_poster` —— 右侧裁剪：宽度 = height × `poster_ratio`，JPEG 质量参数化
- 调用点：`handlers/file.py::_place_library_images` poster 分支 —— `config.scraping.crop_poster`（总开关）AND `allows_poster_crop(content_type)` AND `should_crop_poster(...)` 三重判定后裁剪
- `poster_ratio` 默认 `2/3`（Emby 口径），单一比例不可配类型

### 2.2 现有下载链

- `src/amane/media/resource_store.py::ResourceStore.acquire_first(urls, client)` —— 顺序优先，第一个成功即停
- 候选来源：聚合层 `poster_urls` / `thumb_urls` / `trailer_urls`（按字段链顺序）
- extrafanart：`acquire_extrafanart(urls_by_site, …)` + 剧照文件名按 `extrafanart_name_template` 渲染（P0+P1 已接 naming 分派）

### 2.3 覆盖层现状

- `config/overrides.py::SECTION_MODELS` 只注册 `common` / `naming` / `nfo`；`metadata` 为预留节，写入返回 422
- 激活新节 = 定义模型 + 加进 `_SECTION_BINDINGS`，`ConfigResolver` 与 PUT/DELETE 白名单自动扩展，无需改 resolver

### 2.4 OpenCV 实测（本机已验证）

- `opencv-python` 5.0.0 wheel 在 Python 3.14 / Windows 直装成功；`cv2.FaceDetectorYN` 存在
- YuNet 需要 ONNX 模型文件（`face_detection_yunet_2023mar.onnx`，约 232 KB，Intel License / BSD 代码）——**随包分发**，放 `src/amane/media/models/`
- 依赖形态：`pyproject.toml` 新增 `[project.optional-dependencies].opencv = ["opencv-python>=5"]`，**不进主依赖**

## 3. 设计

### 3.1 配置面：激活 `metadata` 覆盖节

`src/amane/config/manager.py` 新增 `MetadataConfig`（`extra="forbid"`），挂进 `HotSettings.metadata`：

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `crop_type` | `dict[ContentType, CropStrategy]` | censored/uncensored=`right`，其余=`none` | 行为不变原则；MDC 默认（fc2=Face 等）由用户显式开启 |
| `crop_aspect` | `Literal["emby", "full"]` | `"emby"` | 裁剪产物宽高比：emby=2/3（现状），full=2.12/3（完整海报口径，≈0.703 w/h） |
| `thumb_download_strategy` | `Literal["priority", "quality"]` | `"priority"` | priority=现状顺序优先；quality=全候选下载后择优 |
| `extra_fanarts_behind_scenes` | `bool` | `false` | 幕后花絮图并入 extrafanart 候选 |
| `crop_low_res_height` | `int | None` | `None` | 低清阈值：原海报候选高度低于此值才参与裁剪比较；None 关闭（现状判定） |

`config/overrides.py`：`MetadataConfig` 加入 `_SECTION_BINDINGS`（`metadata` 节激活），PUT/DELETE 白名单自动生效。
沿用既有契约：payload 禁 null、`{}` 显式取空、`extra="forbid"` 未知键 422。

`crop_type` 经 `DictField` 范式校验（值域 none/right/face，键域 ContentType），键集**非冻结**：
用户可只配关心的类型，未配置的类型按行为不变默认矩阵解析（censored/uncensored=right，其余=none）——
即 `crop_type` 是对默认矩阵的稀疏覆盖，与覆盖层语义同构。

**总开关关系**：既有 `scraping.crop_poster`（bool 总开关）保留——False 时全类型不裁（现状）；
True 时按 `crop_type` 矩阵执行。`allows_poster_crop` 硬编码被矩阵取代后删除。

### 3.2 裁剪策略：`CropPlan` 与三策略

`src/amane/media/crop.py`（新建）：

```python
class CropStrategy(StrEnum):
    NONE = "none"      # 不裁剪: 候选原样使用
    RIGHT = "right"    # 右侧裁剪: 宽度 = height × 比例 (现行为)
    FACE = "face"      # 人脸对齐裁剪: YuNet 检出人脸框, 裁剪窗按比例对齐人脸

@dataclass(frozen=True)
class CropPlan:
    strategy: CropStrategy
    aspect: float                       # 产物宽高比 (w/h): emby=2/3, full=2.12/3
    low_res_threshold: int | None       # 原海报候选高度低于此值才裁剪; None = 不做低清判定
```

- `resolve_crop_plan(content_type, metadata_config, thumb_size, candidate_size) -> CropPlan`
  —— 组合三重判定：总开关 → 类型策略 → `should_crop_poster` 尺寸判定（保留）。
  策略 `none` 直接返回不裁。
- **Right 实现**：复用现有 `images.py::crop_poster`，`poster_ratio` 取 `crop_aspect` 对应值
  （emby=2/3, full=2.12/3），`skip_ratio` 判定不变
- **Face 实现**：`cv2.FaceDetectorYN` 在 thumb 上检测人脸框；
  检出 → 裁剪窗宽度 = height × aspect，**水平位置对齐人脸中心**（窗在图内平移，越界钳制）；
  未检出人脸 → 回退 Right 语义；
  `import cv2` 抛 `ModuleNotFoundError` → 降级 Right，`structlog` warning **每任务一次**
  （contextvars 计数，不逐文件刷屏）
- **模型加载**：ONNX 随包分发（`src/amane/media/models/face_detection_yunet_2023mar.onnx`），
  首次使用时惰性加载并缓存检测器实例（加载失败 → 降级 Right + warning，不抛出中断任务）

### 3.3 裁剪结果优先（低清判定）

`crop_when_poster_low_res` 的 Amane 化：`MetadataConfig.crop_low_res_height: int | None = None`
（None = 关闭，保持现状判定）。配置高度阈值后：原海报候选高度 < 阈值 → 视为低清，
**裁剪产物与原候选比较**——裁剪产物高度 ≥ 阈值 → 用裁剪产物；仍低 → 取两者中较高者。
现有 `should_crop_poster` 的 `skip_ratio` 判定与该阈值并行生效（先 skip_ratio 后 low_res）。

### 3.4 下载策略：`priority` / `quality`

`resource_store.py`：

- 既有 `acquire_first` 保留（priority 策略直接沿用，零改动）
- 新增 `acquire_best(urls, client) -> AcquireResult`：
  1. 全部候选**顺序**下载到临时位置（沿用 `acquire` 的限流、去重与记录机制 —— 并发不进本期承诺）
  2. 逐个读取分辨率（宽 × 高）与字节数
  3. 择优规则：分辨率面积最大者；面积相同取字节数大者（JPEG 质量更高的间接信号）
  4. 保留最优、删除其余临时文件；全部失败返回 `AcquireResult(success=False, failed=all)`
- **部分失败语义**：候选中任意 ≥1 个成功即在成功者中择优；失败候选照常计入 `failed`
  （与 priority 的失败清单口径一致）
- 择优**不含水印后处理**——比较基于原始下载产物；水印在落盘阶段叠加（现状顺序不变）
- 择优产物**同样经过裁剪判定**（3.2）——下载择优与裁剪是两个独立阶段，先择优后裁剪

### 3.5 剧照幕后场景

- `crawlers/models.py::MediaMetadata` 新增 `behind_scenes: list[str] = []`（有默认值，存量快照兼容）
- 聚合层：`extra_fanarts_behind_scenes=true` 时并入 extrafanart 候选（`false` 或来源未提供 → 空，现状）
- 来源支持度：实现期逐源核实是否有幕后图集数据；**无来源提供时该开关为配置 no-op**（记已知限制）
- 落盘复用既有 extrafanart 管线（命名模板 / download_resources / naming 分派），不新建管线

### 3.6 接线与交互

- `EffectiveOrganizeConfig` 扩展 `metadata: MetadataConfig`（任务入口解析一次、向下传参，
  下层不回读库行 —— 沿用 P1 生效配置传递接口的契约与测试口径）
- `_place_library_images` poster 分支：三重判定改为 `crop_poster 总开关 AND CropPlan.strategy ≠ none
  AND should_crop_poster`；裁剪调用走 `crop.py` 按 plan 分派
- 下载分派：`acquire_first` / `acquire_best` 按 `thumb_download_strategy` 选择，
  thumb 与 poster 候选共用同一策略（MDC 同口径）
- **水印交互不变**：裁剪发生在落盘前，水印叠加在落盘产物上（现状顺序：下载 → 裁剪 → 写入 → 水印）
- 覆盖层合成：`resolve_effective_config` 增加 `metadata` 节解析（与 common/naming/nfo 同批）

### 3.7 OpenCV 依赖与打包

- `pyproject.toml`：`[project.optional-dependencies].opencv = ["opencv-python>=5"]`
- **不进主依赖**：未装 extra 时 `import cv2` 失败是**预期路径**，Face 降级（决策 #1）
- ONNX 模型（~232 KB）随包分发在 `src/amane/media/models/`，不入 `.gitignore`；license 为 Intel/BSD 可再分发
- 打包验证折进实现收尾任务：PyInstaller 桌面版按 extra 装配、Docker 镜像同验（沿 RE2/打包验证先例）
- Android 壳本期不验证（roadmap 风险项的桌面部分本期闭环，Android 留待壳侧集成期）

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| Face 策略但未装 `[opencv]` | 降级 Right，每任务一次 warning；不失败不静默 |
| YuNet 模型加载失败 | 同上降级 Right + warning |
| Face 检测无人脸 | 回退 Right 语义裁剪 |
| quality 策略全部候选失败 | `AcquireResult(success=False)`，failed 清单完整（与 priority 口径一致） |
| quality 策略部分候选失败 | 在成功者中择优；失败者计入 failed |
| 裁剪产物低清 | 按 3.3 的阈值比较取优；未配阈值维持现状判定 |
| `crop_type` 含未知 ContentType 键 | 配置校验 422（键域即枚举） |
| `crop_type` 值非法 | 422（值域 none/right/face） |
| 幕后场景无来源支持 | 开关为 no-op，记已知限制 |
| OpenCV wheel 与平台不匹配 | 同未安装：降级 Right（import 失败统一处理） |

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| `MetadataConfig` 校验 | 表测试：字段默认值、crop_type 键/值域 422、`{}` 显式取空、覆盖层四层合成 |
| `resolve_crop_plan` | 表测试：7 ContentType × 3 策略 × 总开关组合；`should_crop_poster` 与 low_res 并行判定 |
| Right / Face 裁剪产物 | 表测试：Right 与现状逐字节一致（同参数）；Face 按人脸框对齐（样例图 + 断言裁剪窗位置）；无人脸回退 |
| Face 降级 | 表测试：无 cv2 → 降级 Right；warning 每任务一次 |
| `acquire_best` | 表测试：择优排序（面积优先、同面积比字节）、部分失败、全失败、临时清理 |
| 下载策略分派 | 集成：priority 与现状一致；quality 择优产物入选 |
| 水印交互 | 回归：裁剪后水印叠加顺序与产物不变 |
| 默认输出不变 | 基准：默认配置下图片产物与升级前逐字节一致 |
| 幕后场景 | 表测试：开关开/关 × 来源提供/未提供 |
| OpenCV wheel | 实测：3.14/Windows 安装 + FaceDetectorYN 冒烟（沿用 RE2 先例，已预验） |
| 覆盖层 | 集成：`metadata` 节 PUT/DELETE/四层合成/effective-config |

**demo 交付**（规格批准后、实现计划前）：三策略裁剪对比静态可视页（同一样张
None/Right/Face 并排 + aspect 两值对比 + 择优前后对比），供验收口径确认。

**回归重点**：默认配置下图片产物逐字节不变；`scraping.crop_poster=False` 行为不变；
水印与超分链路零改动。

## 6. 实施顺序（供实现计划参考）

1. **OpenCV wheel 与 YuNet 模型实证**（extra 声明 + 模型入库 + 冒烟）
2. **`MetadataConfig` + `metadata` 节激活**（模型 / 覆盖层注册 / 校验）
3. **`CropPlan` + Right 复用**（`crop.py`，行为不变基准）
4. **Face 实现**（YuNet 对齐裁剪 + 降级）
5. **下载策略**（`acquire_best` + 分派）
6. **裁剪结果优先**（low_res 阈值比较）
7. **幕后场景**（MediaMetadata 字段 + 聚合并入 + 开关）
8. **接线与配置传递**（EffectiveOrganizeConfig 扩展 + handler 分派）
9. **demo 可视对比页**
10. **打包验证 + 文档同步**（`docs/dev/` + `docs/user/organize.md`）

## 7. 已知限制

1. **`image_strict_mode` 不实现** —— 调研未捕获行为语义，不臆造；后续调研补齐后单独立项。
2. **高清海报源（日亚 / Tenhow）不实现** —— P4 插件能力（roadmap 既有结论）。
3. **幕后场景依赖来源支持** —— 内置来源均未提供幕后图集时，开关为 no-op；实现期核实并在文档如实标注。
4. **Android 壳的 OpenCV 打包不在本期验证** —— 桌面（PyInstaller）与 Docker 本期闭环。
5. **YuNet 模型只含正面人脸检测** —— 侧脸 / 遮挡场景回退 Right；模型升级（更大 ONNX）留待后续。
6. **`P3 水印逐类开关` 与本设计的交互** —— 水印策略在 P3 设计，本期保持现状（裁剪后统一叠加）。
