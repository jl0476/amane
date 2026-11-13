# 设计规格：图片处理链（MDC 迁移 P2）

> 日期: 2026-11-13
> 状态: 待审阅（第 2 版，按首轮设计复审修订 1 项 P1 / 3 项 P2 与 2 项非阻塞补充）
> 范围: MDC 能力迁移第二阶段 —— 分类化裁剪、人脸识别裁剪、下载策略、裁剪比例、裁剪结果优先、剧照幕后场景
> 上游总纲: `docs/superpowers/mdc-roadmap.md`
> 前置: P0+P1 已合入 main（`efbf33c`），本文引用的覆盖层 / naming 分派 / 水印交互均以其落地形态为基线

## 0. 已定决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | OpenCV 依赖形态 | **可选 extra + 降级**：`[opencv]` extra；缺依赖时 Face 策略降级为 Right，任务日志明确提示 |
| 2 | 下载策略成本语义 | **策略可配，默认顺序优先**：`quality` 策略显式开启才付全候选流量代价 |
| 3 | 实现载体 | **策略 Protocol**：`CropPlan` / 下载策略轻量抽象，P4 插件化时接口已就位 |
| 4 | 高清海报源（日亚 / Tenhow） | 不实现，P4 插件能力（roadmap 既有结论） |
| 5 | `image_strict_mode` | 不实现——调研未捕获其行为语义，不臆造；记已知限制 |
| 6 | **图片处理双入口**（第 1 版复审 P1） | **刮削物化**（`media/pipeline.py::materialize_images`，全局配置作用域）与**整理期重选**（`handlers/file.py`，库级生效配置作用域）共用同一套策略规则；派生缓存键纳入策略与比例 |
| 7 | **裁剪比例三值枚举**（第 1 版复审 P2） | `crop_aspect: legacy(默认, 沿用 scraping.poster_ratio) / emby(2/3) / full(2.12/3)`——`legacy` 保证默认逐字节不变，显式选择才覆盖；旧自定义比例在 legacy 下原样生效 |
| 8 | **择优的临时文件所有权**（第 1 版复审 P2） | 择优在**调用方独占临时文件**上进行；胜者经 store 提交入缓存；共享 Resource 与本轮落选临时文件严格区分，绝不删除共享缓存 |
| 9 | **幕后图独立持久 + 整理期筛选**（第 1 版复审 P2） | `behind_scenes` 作为**独立字段**贯穿聚合与落库（不并入 `extrafanart_urls`）；筛选发生在整理/物化阶段按库级生效配置执行 |
| 10 | 多人脸选取 | 面积最大者；并列取最左（可重复、可验收） |
| 11 | `crop_type` 空对象 | **422**——`{}` 哨兵仅适用于可空模板字段；重置 `crop_type` 的方式是在 PUT payload 中省略该键（PUT 整体替换语义） |

## 0.1 审查记录

第 1 版经独立复审确认 1 项 P1、3 项 P2 与 2 项非阻塞补充
（`logs/reviews/2026-11-14-mdc-p2-image-pipeline-design-review.md`），全部核实成立，本版修订：

| 级别 | 问题 | 修订 | 落点 |
|---|---|---|---|
| P1 | 接线遗漏刮削期图片物化：主裁剪入口是 `media/pipeline.py::materialize_images`（派生 Resource + `result_poster_urls` 整体替换为内部 URL），设计只安排整理阶段 | 双入口契约（决策 6）：刮削物化按**全局**配置、整理期重选按**库级**生效配置，共用策略规则；派生缓存键纳入策略与比例；原始候选从 raw 快照重建 | §3.2、§3.6 |
| P2 | 默认比例误记 2/3：实际 `ScrapingConfig.poster_ratio` 默认 **0.7**（可配 0.3–1.0），新枚举两值都不能逐字节保留现状 | 三值枚举 `legacy/emby/full`（决策 7），`legacy` 委托既有 `poster_ratio`；旧自定义比例在 legacy 下原样生效 | §3.1、§3.2 |
| P2 | 择优清理越权：`acquire` 返回共享持久缓存路径（去重命中直出 / 内部 URL 引用 / `.part` 已原子落库），删落选会破坏共享引用 | 独占临时文件择优 + 胜者提交（决策 8）：临时命名、清理责任、胜者入缓存去重全部定义 | §3.4 |
| P2 | 幕后图聚合期并入单一 `extrafanart_urls`（番号级）后无法按库级开关筛选，也无法从关闭态聚合结果取回 | 独立字段贯穿聚合与落库 + 整理期筛选（决策 9）：来源优先级、合并顺序、去重规则全部定义 | §3.5 |
| 补充 | 多人脸对齐目标未定 | 面积最大、并列取最左（决策 10），进验收 | §3.3 |
| 补充 | `{"crop_type": {}}` 会被哨兵规范化为 null，与不可空 dict 冲突 | 空对象 422；重置 = PUT 省略该键（决策 11） | §3.1 |

## 1. 目标

把 MDC 的「按影片类型精准化图片处理」能力以原生 Python 实现进 Amane：裁剪策略按类型独立、
产物质量可择优、裁剪比例可配，**刮削与整理两个入口行为一致**，默认配置下产物逐字节不变。

**成功标准**（可验证）：

1. 同一条目切换 `content_type` 后，裁剪行为随之变化（censored 裁右侧、策略 none 不裁）——**刮削与整理两个入口一致**。
2. `thumb_download_strategy=quality` 时，多候选场景产出分辨率/体积最优者；`priority` 时行为与现状一致。
3. 装有 `[opencv]` extra 时 Face 策略按人脸对齐裁剪；未装时降级 Right 且日志明确（每任务一次）。
4. 默认配置下（`crop_aspect=legacy` 且 `poster_ratio=0.7`），整理与刮削产物的图片文件与升级前**逐字节一致**；既有自定义 `poster_ratio` 在 legacy 下原样生效。
5. 新配置字段经覆盖层四层合成生效；库级覆盖对**整理期**产物生效（刮削期为全局作用域，见 §3.2）。
6. 同一番号关联多个媒体库时，各库按各自生效配置得到不同裁剪产物；切换库级配置后重新整理生效。
7. 择优不破坏共享缓存：落选的已缓存 Resource 与记录保持可读、一致。

**非目标**：

- 高清海报源（日亚 / Tenhow）→ P4 插件能力。
- `image_strict_mode` → 语义未调研，不实现（已知限制）。
- 水印逐类开关 → P3。
- 超分链路（`src/amane/sr/`）不动；裁剪 → 超分的既有顺序保持。

## 2. 现状与约束

### 2.1 刮削物化链（第 1 版遗漏的主入口）

- `src/amane/media/pipeline.py::materialize_images` —— **刮削期主裁剪入口**：
  - `crop_enabled = config.scraping.crop_poster and allows_poster_crop(content_type)`（硬编码 censored/uncensored）
  - thumb 全候选下载，poster 全候选下载取首个成功者
  - `should_crop_poster(thumb_size, cand_size, skip_ratio=config.scraping.poster_crop_skip_ratio)` 判定
  - 命中后 `store.acquire_derived(thumb_src, "crop", f"{poster_ratio:.4f}", producer)` 生成**派生 Resource**
  - 成功后 `result_poster_urls` **整份替换**为派生图的单个内部 URL
  - 派生产物随后经 `_maybe_upscale`（裁剪 → 超分顺序是既有契约）
- `handlers/scrape.py::ScrapeHandler.handle` 调用 materialize 并把 `poster_out` 落库——**落库的 `Metadata.poster_urls` 可能已是派生内部 URL**，原始外部候选只在 `raw` 快照（爬虫原值）中保留
- 整理阶段：`handlers/file.py::_place_library_images` 在 `poster_local` 缺失时才走裁剪补充路径（有物化图直接复制）——整理期裁剪是**次要路径**

### 2.2 既有比例与下载链

- `ScrapingConfig.poster_ratio: float = Field(default=0.7, ge=0.3, le=1.0)`；`images.py::_DEFAULT_POSTER_RATIO = 0.7`
- 高度 538 的图片：0.7 → 宽 376；2/3 → 358；2.12/3 → 380 —— **两个新枚举值都不能逐字节保留现状默认**
- `ResourceStore.acquire_first` 顺序优先；`acquire` 返回 **Resource 共享持久路径**（URL hash 去重命中直出、内部 URL 返回被引用资源、新下载 `.part` 原子落库并写 Resource 记录）——**返回路径不是调用方独占**
- `acquire_derived(src_url, op, args, producer)` 缓存键 = `(src_url, op, args)`；同键且字节一致则直出

### 2.3 覆盖层与聚合现状

- `config/overrides.py::SECTION_MODELS` 只注册 `common` / `naming` / `nfo`；`metadata` 预留（422）
- `aggregate/engine.py::_assemble_aggregate_fields` 把 extrafanart 候选写入**番号级** `Metadata.extrafanart_urls: dict[str, list[str]]`（按来源分组）；整理期 `acquire_extrafanart` 只消费这份列表——聚合期合并后的列表无法按库区分，来源未提供幕后图时聚合结果也不含
- `OverridePayload` 把 `{}` 规范化为 null（可空模板字段的显式取空哨兵）——对**不可空 dict 字段**会产生语义冲突，需显式规定

### 2.4 OpenCV 实测（本机已验证）

- `opencv-python` 5.0.0 wheel 在 Python 3.14 / Windows 直装成功；`cv2.FaceDetectorYN` 存在
- YuNet 需要 ONNX 模型文件（`face_detection_yunet_2023mar.onnx`，约 232 KB，Intel/BSD 许可）——**随包分发**，放 `src/amane/media/models/`
- 依赖形态：`pyproject.toml` 新增 `[project.optional-dependencies].opencv = ["opencv-python>=5"]`，**不进主依赖**

## 3. 设计

### 3.1 配置面：激活 `metadata` 覆盖节

`src/amane/config/manager.py` 新增 `MetadataConfig`（`extra="forbid"`），挂进 `HotSettings.metadata`；
`config/overrides.py` 将 `MetadataConfig` 注册进 `_SECTION_BINDINGS`（PUT/DELETE 白名单自动扩展）。

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `crop_type` | `dict[ContentType, CropStrategy]` | censored/uncensored=`right`，其余=`none` | 稀疏覆盖行为不变默认矩阵；键集非冻结 |
| `crop_aspect` | `Literal["legacy", "emby", "full"]` | `"legacy"` | legacy=沿用 `scraping.poster_ratio`（默认 0.7，含用户自定义值）；emby=2/3；full=2.12/3 |
| `thumb_download_strategy` | `Literal["priority", "quality"]` | `"priority"` | priority=现状顺序优先；quality=全候选独占临时下载后择优 |
| `crop_low_res_height` | `int \| None` | `None` | 低清阈值：原海报候选高度低于此值才参与裁剪比较；None 关闭（现状判定） |
| `extra_fanarts_behind_scenes` | `bool` | `false` | 整理/物化期把幕后图并入 extrafanart 候选（独立持久字段筛选，见 §3.5） |

沿用既有契约：payload 禁 null、`extra="forbid"`。**`crop_type` 的 `{}` 值返回 422**（`{}` 哨兵只定义给可空字段；
重置方式是 PUT payload 省略该键——PUT 整体替换节 payload，省略即回落默认矩阵）。

**字段优先级**：`crop_aspect="legacy"` 时 Right 策略的比例取 `scraping.poster_ratio`（既有配置原样生效，
默认 0.7 → 逐字节不变）；显式 `emby` / `full` 覆盖该值。总开关 `scraping.crop_poster=False` 时全类型不裁（现状）；
True 时按 `crop_type` 矩阵执行。`allows_poster_crop` 硬编码被矩阵取代后删除。

### 3.2 双入口裁剪：策略规则的唯一定义与两处作用域

`src/amane/media/crop.py`（新建）：

```python
class CropStrategy(StrEnum):
    NONE = "none"      # 不裁剪: 候选原样使用
    RIGHT = "right"    # 右侧裁剪: 宽度 = height × 比例
    FACE = "face"      # 人脸对齐裁剪: YuNet 检出人脸框, 裁剪窗按比例对齐人脸

@dataclass(frozen=True)
class CropPlan:
    strategy: CropStrategy
    ratio: float                  # 产物宽高比 (w/h): 由 crop_aspect 解析出的实际值
    low_res_height: int | None    # None = 不做低清比较
```

- **策略规则唯一定义**：`resolve_crop_plan(content_type, metadata_cfg, thumb_size, candidate_size) -> CropPlan`
  与「按 plan 执行裁剪」的执行函数只写在 `crop.py`；**两个入口都调用它**，不得各写一份
- **入口 1 —— 刮削物化**（`media/pipeline.py::materialize_images`）：配置作用域 = **全局**
  `HotSettings.metadata`（刮削按番号进行，无库作用域）；
  Right/Face 的派生产物经 `store.acquire_derived(thumb_src, "crop", args, producer)` 落缓存
- **入口 2 —— 整理期重选**（`handlers/file.py`，`poster_local` 缺失时的补充路径）：
  配置作用域 = **库级生效** `MetadataConfig`（`EffectiveOrganizeConfig` 扩展传入）
- **原始候选重建**：整理期重选与 Face 重裁的候选来源 = `Metadata.raw` 快照中的爬虫原始 `poster_urls`
  （按来源顺序拼接；派生内部 URL **排除**——它们是旧策略的产物，不是候选）。
  `Metadata.poster_urls` 保持现状语义（刮削期结果，可能含派生内部 URL），**不迁移、不改写**（已知限制 6）
- **派生缓存键纳入配置**：`acquire_derived` 的 `args` 从 `f"{ratio:.4f}"` 扩展为
  `f"{strategy}:{aspect}:{ratio:.4f}"` 形态——不同策略/比例得到不同派生条目，
  杜绝「换策略后命中旧裁剪缓存」；同配置重复执行直出（幂等）
- **裁剪 → 超分顺序契约**：派生产物的 `_maybe_upscale` 调用保持在裁剪之后（现状），Face/Right 一致；
  整理期重选产物同样先裁剪后超分

### 3.3 Face 策略细节

- YuNet 在 thumb 上检测人脸框；**多人脸取面积最大者，并列取最左**（可重复）
- 检出 → 裁剪窗宽度 = height × ratio，**水平位置对齐人脸中心**（窗在图内平移，越界钳制）
- 未检出人脸 → 回退 Right 语义
- `import cv2` 抛 `ModuleNotFoundError` → 降级 Right，structlog warning **每任务一次**（contextvars 计数）
- ONNX 随包分发（`src/amane/media/models/face_detection_yunet_2023mar.onnx`），首次使用惰性加载并缓存检测器实例；
  加载失败 → 降级 Right + warning，不中断任务

### 3.4 下载策略：独占临时文件择优 + 胜者提交

`resource_store.py` 新增 `acquire_best(urls, client, *, task_token) -> AcquireResult`：

1. **独占下载**：每个候选下载到**调用方独占临时文件**（命名沿用 `overwrite.py` 的 `.amane-tmp-` 命名纪律 +
   task_token；**不写 Resource 记录、不进共享缓存**）。候选命中共享缓存时**复制到独占临时**参与比较
   （共享文件只读，不修改不删除）
2. **比较**：逐个读取分辨率与字节数；**面积最大者胜，面积相同取字节数大者**
3. **胜者提交**：胜者 URL 经 store 既有 acquire 语义入共享缓存——若该 URL 已被缓存命中，直接返回
   既有缓存路径并弃本任务临时副本（保持记录与文件一致）；未缓存则按 acquire 落库后返回持久路径
4. **清理责任**：落选临时文件与全部临时文件（含取消/异常路径）由本轮负责删除；
   **共享 Resource 与既有缓存文件绝不删除**；失败候选照常计入 `failed`
5. 部分失败：≥1 成功即在成功者中择优；全失败返回 `AcquireResult(success=False, failed=all)`
6. 择优不含水印后处理（比较基于原始下载产物）；择优产物**同样经过裁剪判定**——下载择优与裁剪是
   独立阶段，先择优后裁剪

`priority` 策略沿用 `acquire_first`（零改动）。

### 3.5 剧照幕后场景：独立持久 + 整理期筛选

- `crawlers/models.py::MediaMetadata` 新增 `behind_scenes: list[str] = []`（有默认值，存量快照兼容）
- **聚合层**：`AggregatedMetadata.behind_scenes: dict[str, list[str]]`（按来源分组，与 extrafanart_urls 同构）
  —— **独立字段，不并入 `extrafanart_urls`**（决策 9：聚合期合并会让库级筛选失能）
- **落库**：`Metadata.behind_scenes: dict[str, list[str]]` JSON 列（Alembic 迁移）；
  `ScrapeHandler` 落库时透传——**无论开关状态都持久化**（原始数据归属，筛选是消费侧行为）
- **筛选时机**：整理/物化阶段按该库生效 `metadata.extra_fanarts_behind_scenes` 决定是否把
  `behind_scenes` 候选追加到 extrafanart 下载集合
- **合并顺序与去重**：普通剧照在前、幕后在后；URL 去重沿用 `acquire_extrafanart` 既有键；
  幕后图同样经 `extrafanart_name_template` 命名（同目录序列一致）
- **来源支持度**：实现期逐源核实；无来源提供时开关为 no-op（记已知限制）
- 刮削时关闭开关 → 数据仍持久化；开启后**无需重刮**，重新整理即生效

### 3.6 接线与交互

- `EffectiveOrganizeConfig` 扩展 `metadata: MetadataConfig`（任务入口解析一次、向下传参，下层不回读库行）
- `_place_library_images` poster 分支：三重判定改为 `crop_poster 总开关 AND CropPlan.strategy ≠ none
  AND should_crop_poster`；裁剪调用走 `crop.py` 按 plan 分派
- 下载分派：`acquire_first` / `acquire_best` 按 `thumb_download_strategy` 选择；thumb 与 poster 候选共用同一策略
- **水印交互不变**：下载 → 裁剪 → 写入 → 水印（现状顺序）；择优比较基于裁剪前原始产物
- 覆盖层合成：`resolve_effective_config` 增加 `metadata` 节解析（与 common/naming/nfo 同批）

### 3.7 OpenCV 依赖与打包

- `pyproject.toml`：`[project.optional-dependencies].opencv = ["opencv-python>=5"]`，不进主依赖
- 未装 extra 时 `import cv2` 失败是**预期路径**（Face 降级，不失败不静默）
- ONNX 模型随包分发在 `src/amane/media/models/`；license 为 Intel/BSD 可再分发
- 打包验证折进实现收尾任务：PyInstaller 桌面版按 extra 装配、Docker 镜像同验（沿 RE2/打包验证先例）；
  Android 壳本期不验证（roadmap 风险项的桌面部分本期闭环）

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| Face 策略但未装 `[opencv]` | 降级 Right，每任务一次 warning；不失败不静默 |
| YuNet 模型加载失败 / 检测异常 | 降级 Right + warning；不中断任务 |
| Face 检出多个人脸 | 取面积最大者，并列取最左；无人脸回退 Right |
| quality 策略全部候选失败 | `AcquireResult(success=False)`，failed 清单完整 |
| quality 策略部分候选失败 | 在成功者中择优；失败者计入 failed |
| 择优候选命中共享缓存 | 缓存文件只读参与比较（复制到独占临时或按记录读取尺寸），不修改不删除 |
| 本轮取消 / 异常 | 本轮独占临时文件全部清理；共享缓存与 Resource 记录不动 |
| `crop_type` 含未知 ContentType 键 / 非法值 | 422 |
| `crop_type` 值为 `{}` | 422（空对象哨兵不适用不可空 dict；重置 = PUT 省略该键） |
| 裁剪产物低清 | 按 `crop_low_res_height` 比较取优；None 维持现状判定 |
| 幕后场景无来源支持 | 开关为 no-op，记已知限制 |
| 整理期候选重建失败（raw 无候选） | 回退现状路径（物化图复制 / 无 poster 不裁），记日志 |

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| `MetadataConfig` 校验 | 表测试：字段默认值、crop_type 键/值域 422、**`crop_type: {}` 422**、四层合成 |
| `resolve_crop_plan` | 表测试：7 ContentType × 3 策略 × 总开关组合；skip_ratio 与 low_res 并行判定 |
| 双入口一致性 | 集成：同一策略矩阵下 materialize（全局作用域）与整理（库级作用域）产出一致；**无媒体文件的按番号 Face / none 刮削**检查持久化 URL 与实际图片 |
| 派生缓存键 | 表测试：策略 / aspect / ratio 任一变化 → 派生键不同，不命中旧缓存；同配置幂等直出 |
| 整理期候选重建 | 集成：从 raw 重建候选（派生内部 URL 排除）；raw 无候选回退现状路径 |
| Right / Face 裁剪产物 | 表测试：legacy 下与现状逐字节一致（默认 0.7 与自定义比例各一）；Face 按人脸框对齐（样例图断言裁剪窗位置）；**多人脸取面积最大/并列最左**；无人脸回退 |
| Face 降级 | 表测试：无 cv2 → 降级 Right；warning 每任务一次 |
| `acquire_best` | 表测试：择优排序（面积→字节）、部分失败、全失败、**共享缓存候选只读参与**、本轮临时清理、胜者提交去重 |
| 共享资源安全 | 集成：候选预先被另一番号引用、候选为内部 URL、两任务共享同一候选——落选后原缓存文件与记录可读一致 |
| 库级隔离 | 集成：同一番号两库分别 none 与 Face / 不同 aspect——各库产物符合各自配置；**刮削后改库级配置再整理生效** |
| 幕后场景 | 集成：同番号两库相反开关各得所需集合；刮削关→整理开（及反向）均生效；普通剧照不受影响 |
| 默认输出不变 | 基准：默认配置（legacy 0.7）下刮削与整理产物与升级前逐字节一致；`test_poster_cropped_when_candidate_small` 的 `(376, 538)` 断言原样通过 |
| OpenCV wheel | 实测：3.14/Windows 安装 + FaceDetectorYN 冒烟（已预验） |
| 水印 / 超分交互 | 回归：裁剪 → 超分 → 水印顺序与产物不变 |

**demo 交付**（规格批准后、实现计划前）：三策略裁剪对比静态可视页（同一样张 None/Right/Face 并排 +
aspect 三值对比 + 择优前后对比），供验收口径确认。

**回归重点**：默认配置（legacy 0.7）下刮削与整理图片产物逐字节不变；`scraping.crop_poster=False`
行为不变；水印与超分链路零改动；共享缓存安全（落选清理不触碰 Resource）。

## 6. 实施顺序（供实现计划参考）

1. **OpenCV wheel + YuNet 模型实证入库**（extra 声明 + 模型资产 + 冒烟）
2. **`MetadataConfig` + `metadata` 节激活**（模型 / 覆盖层注册 / `{}` 422 校验）
3. **`CropPlan` + Right 复用 + 派生缓存键扩展**（`crop.py`；legacy 行为不变基准）
4. **双入口接线**（materialize_images 全局作用域 + 整理期库级作用域 + raw 候选重建）
5. **Face 实现**（YuNet 对齐 + 降级 + 多人脸规则）
6. **下载策略**（独占临时择优 + 胜者提交 + 分派）
7. **裁剪结果优先**（low_res 阈值比较）
8. **幕后场景**（MediaMetadata/AggregatedMetadata/Metadata 三层字段 + 迁移 + 整理期筛选）
9. **demo 可视对比页**
10. **打包验证 + 文档同步**（`docs/dev/` + `docs/user/organize.md`）

## 7. 已知限制

1. **`image_strict_mode` 不实现** —— 调研未捕获行为语义，不臆造；后续调研补齐后单独立项。
2. **高清海报源（日亚 / Tenhow）不实现** —— P4 插件能力（roadmap 既有结论）。
3. **幕后场景依赖来源支持** —— 内置来源均未提供幕后图集时，开关为 no-op；实现期核实并在文档如实标注。
4. **Android 壳的 OpenCV 打包不在本期验证** —— 桌面（PyInstaller）与 Docker 本期闭环。
5. **YuNet 模型只含正面人脸检测** —— 侧脸 / 遮挡场景回退 Right；模型升级（更大 ONNX）留待后续。
6. **`Metadata.poster_urls` 保持刮削期语义** —— 可能含派生内部 URL；库级差异体现在磁盘产物，
   不改写该列（原始候选的权威来源是 raw 快照）。
7. **水印逐类开关** —— P3 范围；本期保持现状（裁剪后统一叠加）。
