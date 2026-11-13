# 设计规格：图片处理链（MDC 迁移 P2）

> 日期: 2026-11-13
> 状态: 待审阅（第 3 版，按第 2 轮设计复审修订 1 项 P1 / 4 项 P2）
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
| 6 | 图片处理双入口 | 刮削物化（`media/pipeline.py::materialize_images`，全局配置作用域）与整理期重选（`handlers/file.py`，库级生效配置作用域）共用同一套策略规则；派生缓存键纳入策略与比例 |
| 7 | 裁剪比例三值枚举 | `crop_aspect: legacy(默认, 沿用 scraping.poster_ratio) / emby(2/3) / full(2.12/3)`——legacy 保证默认逐字节不变，显式选择才覆盖 |
| 8 | 择优的临时文件所有权 | 择优在**调用方独占临时文件**上进行；胜者经 `commit_temp` 绑定**已比较的字节**提交入缓存；共享 Resource 绝不删除 |
| 9 | 幕后图独立持久 + 整理期筛选 | `behind_scenes` 独立字段贯穿聚合与落库；筛选发生在整理/物化阶段按库级生效配置执行 |
| 10 | 多人脸选取 | 面积最大者；并列取最左（可重复、可验收） |
| 11 | `crop_type` 空对象 | 422——`{}` 哨兵仅适用于可空模板字段；重置方式是 PUT payload 省略该键 |
| 12 | **整理期重选的执行条件与用户产物优先**（R2 P1） | 重选在 poster 复制判定**之前**执行，条件 = 总开关 AND 策略 ≠ none（不依赖 poster_local 缺失）；**用户手动裁剪产物优先于库级策略**——存在 `box:` 手动派生内部 URL 时不重选 |
| 13 | **候选重建经字段链路由 + 按派生参数分类**（R2 P2） | 重建 = raw 各来源原始候选经**当前配置的字段链**（资格 / 排序 / 字段排除）路由；自动派生内部 URL（args 为数值）排除；**手动派生（args 含 `box:`）保留为最终产物** |
| 14 | **胜者提交绑定已比较字节**（R2 P2） | 新增 `ResourceStore.commit_temp(url, temp)`：原子把已比较的临时文件提交为该 URL 的缓存记录；**不得用重新下载替代提交**；并发命中以既有记录为准 |
| 15 | **派生缓存键按实际执行策略**（R2 P2） | 请求 Face 而后端不可用 / 无人脸回退时，派生键按**实际执行的 Right** 编码——依赖修复后 Face 键无条目，自然重新检测；正常就绪下同配置幂等 |
| 16 | **低清比较完整决策算法**（R2 P2） | 见 §3.3 的判定顺序与全部边界（skip_ratio 短路、阈值内外、同高、无候选、尺寸不可读） |

## 0.1 审查记录

第 1 版经复审确认 1 P1 / 3 P2（+2 非阻塞），第 2 版修订后经 R2 复审确认 IMG-01 保持 OPEN、
新增 IMG-05 ~ IMG-08（第 2 轮报告：`logs/reviews/2026-11-14-mdc-p2-image-pipeline-design-review-R2.md`）。
R2 确认 IMG-02 / IMG-03 / IMG-04 已 CLOSED。本版修订：

| 级别 | 问题 | 修订 | 落点 |
|---|---|---|---|
| P1 | 整理重选仍限于 `poster_local` 缺失时，已有物化海报阻断库级策略变化 | 重选移到 poster 复制判定**之前**，条件改为「总开关 AND 策略 ≠ none」；用户手动产物优先；overwrite_image=False 跳过语义保留 | §3.6 |
| P2 | raw 顺序拼接绕过字段优先级 / 排除规则；排除全部派生 URL 会忽略手动裁剪 | 候选重建走**当前配置的字段链路由**（与聚合同一规则）；派生内部 URL 经 Resource 记录按 args 分类——`box:` 手动产物保留为最终产物，数值自动产物排除 | §3.6 |
| P2 | 胜者提交经既有 `acquire` 会重新下载，可能失败或取得不同内容 | 新增 `commit_temp(url, temp)`：绑定已比较字节原子提交；并发命中以既有记录为准；禁止重新下载 | §3.4 |
| P2 | Face 降级产物可能占用 Face 缓存键，补依赖后不重新检测 | 派生键按**实际执行策略**编码；降级产物入 Right 键，Face 键无条目 → 修复后重新检测 | §3.2 |
| P2 | 低清比较的决定规则及与 skip_ratio 的关系缺失 | §3.3 给出完整判定顺序（短路关系、阈值内外、同高、无候选、尺寸不可读全部定义） | §3.3 |

## 1. 目标

把 MDC 的「按影片类型精准化图片处理」能力以原生 Python 实现进 Amane：裁剪策略按类型独立、
产物质量可择优、裁剪比例可配，**刮削与整理两个入口行为一致**，默认配置下产物逐字节不变。

**成功标准**（可验证）：

1. 同一条目切换 `content_type` 后，裁剪行为随之变化——**刮削与整理两个入口一致，且已有物化海报的常规路径同样重选**。
2. `thumb_download_strategy=quality` 时多候选产出最优者；`priority` 时行为与现状一致。
3. 装有 `[opencv]` extra 时 Face 按人脸对齐裁剪；未装时降级 Right 且日志明确（每任务一次）；补装后重新整理得到 Face 产物。
4. 默认配置下（`crop_aspect=legacy` 且 `poster_ratio=0.7`）图片产物与升级前**逐字节一致**；既有自定义 `poster_ratio` 在 legacy 下原样生效。
5. 新配置字段经覆盖层四层合成生效；库级覆盖对整理期产物生效（刮削期为全局作用域）。
6. 同一番号多库各按各自配置得到不同产物；切换库级配置后重新整理生效——**包括已有物化海报的情形**。
7. 择优不破坏共享缓存；胜者返回的字节即参与比较的字节。
8. 用户手动裁剪产物优先于库级策略，重新整理不覆盖。

**非目标**：

- 高清海报源（日亚 / Tenhow）→ P4 插件能力。
- `image_strict_mode` → 语义未调研，不实现（已知限制）。
- 水印逐类开关 → P3。
- 超分链路（`src/amane/sr/`）不动；裁剪 → 超分的既有顺序保持。

## 2. 现状与约束

### 2.1 刮削物化链

- `src/amane/media/pipeline.py::materialize_images` —— 刮削期主裁剪入口：
  `crop_enabled = config.scraping.crop_poster and allows_poster_crop(content_type)`（硬编码 censored/uncensored）；
  `should_crop_poster(thumb_size, cand_size, skip_ratio=config.scraping.poster_crop_skip_ratio)` 判定；
  命中后 `store.acquire_derived(thumb_src, "crop", f"{poster_ratio:.4f}", producer)` 生成派生 Resource；
  成功后 `result_poster_urls` 整份替换为派生图内部 URL；派生产物经 `_maybe_upscale`（裁剪 → 超分既有契约）
- `handlers/scrape.py::ScrapeHandler.handle` 落库 `poster_out`——落库的 `Metadata.poster_urls` 可能已是
  派生内部 URL；原始外部候选只在 `raw` 快照中保留
- 整理阶段：`handlers/file.py::_place_library_images` 在 `poster_local` 存在时直接复制（现状无重选）

### 2.2 手动裁剪与派生 URL 的可区分性

- 手动裁剪：`api/routes/metadata.py::crop_poster_from_thumb` → `media/pipeline.py::manual_crop_image` →
  `store.acquire_derived(source.locator, "crop", args)`，`args = format_crop_box_args(box, rotation)`
  以 **`box:` 前缀**开头（`images.py::CROP_BOX_ARGS_PREFIX`）；结果以 `WriteMode.MANUAL` 整体写入
  `Metadata.poster_urls`
- 自动裁剪：同 op `"crop"`，`args` 为**数值比例**形态（`0.7000`）
- 两者**可区分依据 = 派生 args 形态**；内部 URL 是 locator 的 hash（`/api/resources/{hash}`），
  op/args 需经 Resource 记录反解（`get_by_url_hash` 可达）

### 2.3 既有比例与下载链

- `ScrapingConfig.poster_ratio: float = Field(default=0.7, ge=0.3, le=1.0)`；`images.py::_DEFAULT_POSTER_RATIO = 0.7`
- 高度 538：0.7 → 宽 376；2/3 → 358；2.12/3 → 380 —— 两个新枚举值都不能逐字节保留现状默认
- `ResourceStore.acquire` 返回 **Resource 共享持久路径**（URL hash 去重命中直出、内部 URL 返回被引用资源、
  新下载 `.part` 原子落库并写记录）——返回路径不是调用方独占
- `acquire_derived` 缓存键 = `(src_url, op, args)`；同键且磁盘字节与记录一致则直出，不再调用 producer

### 2.4 覆盖层与聚合现状

- `SECTION_MODELS` 只注册 `common` / `naming` / `nfo`；`metadata` 预留（422）
- `_assemble_aggregate_fields` 写入番号级 `Metadata.extrafanart_urls: dict[str, list[str]]`（按来源分组）；
  聚合期合并后的列表无法按库区分
- `OverridePayload` 把 `{}` 规范化为 null——对不可空 dict 字段是语义冲突，需显式规定
- 字段链路由：`compile_priority` + `_assemble_url_fields` 按 `content_routes` / `field_priority` /
  `field_blacklist` 决定候选资格与排序——**raw 快照保存来源原值，不是路由后的候选列表**

### 2.5 OpenCV 实测（本机已验证）

- `opencv-python` 5.0.0 wheel 在 Python 3.14 / Windows 直装成功；`cv2.FaceDetectorYN` 存在
- YuNet ONNX 模型（约 232 KB，Intel/BSD 许可）随包分发，放 `src/amane/media/models/`
- `[project.optional-dependencies].opencv = ["opencv-python>=5"]`，不进主依赖

## 3. 设计

### 3.1 配置面：激活 `metadata` 覆盖节

`manager.py` 新增 `MetadataConfig`（`extra="forbid"`）挂进 `HotSettings.metadata`；
`overrides.py` 注册进 `_SECTION_BINDINGS`。

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `crop_type` | `dict[ContentType, CropStrategy]` | censored/uncensored=`right`，其余=`none` | 稀疏覆盖行为不变默认矩阵；键集非冻结 |
| `crop_aspect` | `Literal["legacy", "emby", "full"]` | `"legacy"` | legacy=沿用 `scraping.poster_ratio`（含用户自定义值）；emby=2/3；full=2.12/3 |
| `thumb_download_strategy` | `Literal["priority", "quality"]` | `"priority"` | priority=现状顺序优先；quality=全候选独占临时下载后择优 |
| `crop_low_res_height` | `int \| None` | `None` | 低清阈值；None 关闭（现状判定），判定算法见 §3.3 |
| `extra_fanarts_behind_scenes` | `bool` | `false` | 整理/物化期把幕后图并入 extrafanart 候选（§3.5） |

沿用既有契约：payload 禁 null、`extra="forbid"`。**`crop_type` 值为 `{}` 返回 422**
（`{}` 哨兵只定义给可空字段；重置 = PUT payload 省略该键——PUT 整体替换节 payload）。

**字段优先级**：`crop_aspect="legacy"` 时 Right 策略比例取 `scraping.poster_ratio`（既有配置原样生效）；
显式 `emby` / `full` 覆盖。总开关 `scraping.crop_poster=False` 时全类型不裁（现状）；
True 时按 `crop_type` 矩阵执行。`allows_poster_crop` 硬编码被矩阵取代后删除。

### 3.2 双入口裁剪：策略规则唯一定义，派生键按实际执行策略

`src/amane/media/crop.py`（新建）：

```python
class CropStrategy(StrEnum):
    NONE = "none"
    RIGHT = "right"    # 右侧裁剪: 宽度 = height × ratio
    FACE = "face"      # 人脸对齐裁剪: YuNet 检出人脸框

@dataclass(frozen=True)
class CropPlan:
    requested: CropStrategy     # 配置请求的策略
    executed: CropStrategy      # 实际执行的策略 (Face 后端不可用/无人脸回退时 = RIGHT)
    ratio: float                # crop_aspect 解析出的产物宽高比 (w/h)
    low_res_height: int | None
```

- **规则唯一定义**：`resolve_crop_plan(...) -> CropPlan` 与执行函数只写在 `crop.py`，双入口共用
- **派生缓存键按 `executed` 编码**（决策 15）：`args = f"{executed}:{aspect}:{ratio:.4f}"`——
  Face 请求因缺 OpenCV / 模型失败降级 Right 时，产物落在 Right 键；
  补装依赖后 Face 键无条目 → producer 重新执行人脸检测（`acquire_derived` 同键直出语义不会挡住新键）；
  正常就绪下同配置幂等直出
- **无人脸回退**：检测正常执行但无人脸 → 回退 Right，产物按 Right 键缓存（与显式 right 请求语义一致）
- **入口 1 —— 刮削物化**（`materialize_images`）：作用域 = **全局** `HotSettings.metadata`；
  Right/Face 派生产物经 `acquire_derived` 落缓存
- **入口 2 —— 整理期重选**：作用域 = **库级生效** `MetadataConfig`；候选重建见 §3.6；
  派生 / 择优产物经 §3.4 的独占临时 + 提交路径

### 3.3 低清比较：完整决策算法（决策 16）

判定顺序与全部边界（`skip_ratio = config.scraping.poster_crop_skip_ratio`）：

```
1. 总开关 False 或 plan.executed is NONE        → 用候选原样（不裁）
2. 无 thumb 或 thumb 尺寸不可读                  → 不裁，用候选原样（现状 False 语义）
3. 无 poster 候选 或 候选尺寸不可读               → 无比较对象: 用裁剪产物（现状 True 语义）
4. skip_ratio 短路: 候选 b/h ≥ skip_ratio        → 候选已够竖长: 用候选原样, 不裁（现状, 与 low_res 无关）
5. 到此为止需要裁剪。crop_low_res_height = None  → 用裁剪产物（现状）
6. low_res 启用:
   a. 裁剪产物高度 ≥ 阈值                        → 用裁剪产物
   b. 裁剪产物高度 < 阈值 → 与原候选比高度:
        原候选高度 > 裁剪产物高度 → 用原候选（放弃裁剪）
        其余（原候选 ≤ 裁剪产物, 含同高）→ 用裁剪产物
   c. 两者都低于阈值 → 同 b: 高者胜, 同高用裁剪产物
```

- `should_crop_poster` 函数保留，作为第 3/4 步的实现；`crop_low_res_height` 只在通过 skip_ratio
  之后参与产物选择——**skip_ratio 先短路，两者无重叠判定**
- 无效尺寸（宽或高 ≤ 0）按「尺寸不可读」处理

### 3.4 下载策略：独占临时文件择优 + `commit_temp` 绑定已比较字节

`resource_store.py`：

- 新增 `acquire_best(urls, client, *, task_token) -> AcquireResult`：
  1. **独占下载**：每候选下载到调用方独占临时文件（`.amane-tmp-` 命名纪律 + task_token；
     不写 Resource 记录、不进共享缓存）。候选命中共享缓存时**复制到独占临时**参与比较（共享文件只读）
  2. **比较**：分辨率面积最大者胜，同面积取字节数大者
  3. **胜者提交**：`commit_temp(winner_url, temp)` **绑定已比较的字节**——
     按 url_hash 定位缓存路径，原子把临时文件移入并写 Resource 记录；
     **不得重新下载替代提交**（决策 14）
  4. **并发命中**：提交时该 URL 已有缓存记录 → 以既有记录为准（记录与文件一致是缓存不变式），
     删除本轮临时、返回既有持久路径；若需确认内容一致性可比对内容 hash——
     一致静默，不一致记 warning 并返回既有记录
  5. **提交失败**（磁盘错误等）：删除本轮临时文件，返回 FAILED（网络可重试，重新整理即重下载）
  6. **清理责任**：落选临时与全部临时（含取消/异常路径）由本轮删除；
     **共享 Resource 与既有缓存文件绝不删除**
  7. 部分失败：≥1 成功即在成功者中择优；全失败返回 `AcquireResult(success=False, failed=all)`
  8. 择优不含水印；择优产物同样经过裁剪判定——先择优后裁剪

`priority` 策略沿用 `acquire_first`（零改动）。

### 3.5 剧照幕后场景：独立持久 + 整理期筛选

- `MediaMetadata.behind_scenes: list[str] = []`（存量快照兼容）
- `AggregatedMetadata.behind_scenes: dict[str, list[str]]`（按来源分组）——**独立字段，不并入 extrafanart_urls**
- `Metadata.behind_scenes: dict[str, list[str]]` JSON 列（Alembic 迁移）；落库时**无论开关状态都持久化**
- **筛选时机**：整理/物化阶段按该库生效开关决定是否把 behind_scenes 候选追加到 extrafanart 下载集合
- 合并顺序：普通剧照在前、幕后在后；去重沿用 `acquire_extrafanart` 既有键；幕后图经
  `extrafanart_name_template` 命名
- 来源支持度实现期核实；无来源提供 → 开关 no-op（已知限制）
- 刮削时关闭 → 数据仍持久化；开启后无需重刮，重新整理即生效

### 3.6 整理期重选：执行条件、用户产物优先与候选重建

**执行条件与时机**（决策 12，R2 P1 修订）：

- 时机：`_place_library_images` 的 **poster 复制判定之前**——重选胜出者作为本轮 poster 产物，
  替代物化图复制；不再以 `poster_local` 缺失为前提
- 条件：`scraping.crop_poster` 总开关 AND `plan.executed` 对应策略 ≠ none
- **用户手动产物优先**：`Metadata.poster_urls` 存在**手动派生内部 URL**（经 Resource 记录反解，
  locator args 以 `box:` 开头）→ 不重选，直接复制该图（用户指定图片优先于库级策略）；
  手动产物同样受 `overwrite_image=False` 的既有跳过语义约束
- `overwrite_image=False` 的既有跳过语义整体保留：poster 目标已存在 → 跳过写入（重选产物同样适用），
  记入 skipped

**候选重建**（决策 13，R2 IMG-05 修订）：

```
1. Metadata.poster_urls 的内部 URL 逐个经 Resource 记录反解 locator:
   - args 以 "box:" 开头 (手动裁剪) → 用户产物存在, 不重选 (见上)
   - args 为数值形态 (自动裁剪旧产物) → 从候选排除
2. 候选主体 = raw 快照各来源的原始 poster_urls, 经**当前配置的字段链**路由:
   - 资格与排序与聚合同一规则 (compile_priority: content_routes 资格 + field_priority
     前置 + field_blacklist 剔除, 作用于 POSTER 字段)
   - raw 保存来源原值, 不是路由后列表 —— 重建必须过路由, 不得按 raw 键序拼接
3. 字段链外的来源候选不参与 (与聚合口径一致)
4. 候选为空 → 回退现状路径 (物化图复制 / 无 poster 不裁), 记日志
```

- 路由用**当前配置**（整理期配置是纯函数输入，字段优先级 / 排除修改后重建自然跟随）；
  手动裁剪不受路由影响（在步骤 1 已短路）

### 3.7 OpenCV 依赖与打包

- `[project.optional-dependencies].opencv = ["opencv-python>=5"]`，不进主依赖
- 未装 extra 时 `import cv2` 失败是**预期路径**（Face 降级）
- ONNX 模型随包分发；license 为 Intel/BSD 可再分发
- 打包验证折进实现收尾任务：PyInstaller 桌面版按 extra 装配、Docker 同验；Android 壳本期不验证

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| Face 策略但未装 `[opencv]` | 降级 Right（派生键按 Right 实际策略），每任务一次 warning；补装后重新检测 |
| YuNet 模型加载失败 / 检测异常 | 同上降级 Right + warning；不中断任务 |
| Face 检出多个人脸 | 面积最大者，并列取最左；无人脸回退 Right（Right 键缓存） |
| quality 策略全部候选失败 | `AcquireResult(success=False)`，failed 清单完整 |
| quality 策略部分候选失败 | 在成功者中择优；失败者计入 failed |
| 择优候选命中共享缓存 | 复制到独占临时参与比较；共享文件只读 |
| 胜者提交时该 URL 已有缓存记录 | 以既有记录为准：删除本轮临时、返回既有持久路径；内容 hash 不一致记 warning |
| `commit_temp` 磁盘错误 | 删除本轮临时、返回 FAILED；重新整理即重下载 |
| 本轮取消 / 异常 | 本轮独占临时全部清理；共享缓存与记录不动 |
| `crop_type` 键 / 值非法，或值为 `{}` | 422（重置 = PUT 省略该键） |
| 裁剪产物低清 | 按 §3.3 决策算法取优 |
| 幕后场景无来源支持 | 开关 no-op，记已知限制 |
| 整理期候选重建为空 / raw 无候选 | 回退现状路径（物化图复制 / 无 poster 不裁），记日志 |
| 存在用户手动裁剪产物 | 不重选，直接使用手动产物（受 overwrite_image 语义约束） |

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| `MetadataConfig` 校验 | 字段默认值、crop_type 键/值域 422、**`crop_type: {}` 422**、四层合成 |
| `resolve_crop_plan` | 7 ContentType × 3 策略 × 总开关组合；requested 与 executed 分离（降级场景） |
| **低清决策算法**（IMG-08） | 表测试：阈值以下/等于/以上、两图都低清、相同高度、无原候选、尺寸读取失败、**skip_ratio 接受（短路）且候选低于阈值**——全部唯一可判 |
| **双入口一致性**（IMG-01） | 集成：**已有有效物化 poster** 时两库分别 none 与 Face / 不同 aspect；改库级配置再整理生效；检查实际图片与磁盘产物 |
| **派生缓存键**（IMG-07） | 表测试：请求 Face 降级 → Right 键；补装依赖后同源同配置重新检测产出 Face；正常就绪同配置幂等 |
| **候选重建**（IMG-05） | 集成：字段优先级与 raw 键序相反、仅对海报字段排除来源、快照含已停用来源——重建结果与字段链一致；手动裁剪产物（box:）保留不被重建覆盖 |
| **`commit_temp`**（IMG-06） | 表测试：首次下载成功、提交不重新下载（mock 断言无第二次请求）、提交返回已选字节；并发同 URL 缓存命中 → 返回既有记录；任务取消清理临时 |
| 共享资源安全 | 集成：候选被另一番号引用、候选为内部 URL、两任务共享候选——落选后原缓存与记录可读一致 |
| **用户产物优先**（IMG-01） | 集成：手动裁剪后重新整理（两库不同配置）——手动产物不被覆盖 |
| `overwrite_image=False` | 回归：poster 目标已存在 → 跳过（重选与复制路径都适用） |
| Face 降级 | 表测试：无 cv2 → Right；warning 每任务一次 |
| 幕后场景 | 集成：同番号两库相反开关；刮削关→整理开（及反向）；普通剧照不受影响 |
| 默认输出不变 | 基准：legacy 0.7 下刮削与整理产物逐字节一致；`test_poster_cropped_when_candidate_small` 的 `(376, 538)` 原样通过 |
| 水印 / 超分交互 | 回归：裁剪 → 超分 → 水印顺序与产物不变 |

**demo 交付**（规格批准后、实现计划前）：三策略裁剪对比静态可视页（None/Right/Face 并排 +
aspect 三值对比 + 择优前后对比），供验收口径确认。

**回归重点**：默认配置（legacy 0.7）下刮削与整理图片产物逐字节不变；`scraping.crop_poster=False`
行为不变；水印与超分零改动；共享缓存安全；手动裁剪产物不被自动流程覆盖。

## 6. 实施顺序（供实现计划参考）

1. **OpenCV wheel + YuNet 模型实证入库**
2. **`MetadataConfig` + `metadata` 节激活**（含 `{}` 422 校验）
3. **`CropPlan`（requested/executed）+ Right 复用 + 派生键按实际策略扩展**
4. **候选重建**（字段链路由 + box: 手动分类 + raw 来源数据）
5. **双入口接线**（materialize 全局作用域 + 整理期重选前置 + 用户产物优先）
6. **Face 实现**（YuNet 对齐 + 降级 + 多人脸规则）
7. **下载策略**（独占临时择优 + `commit_temp` + 分派）
8. **低清决策算法**（§3.3 全边界）
9. **幕后场景**（三层字段 + 迁移 + 整理期筛选）
10. **demo 可视对比页**
11. **打包验证 + 文档同步**

## 7. 已知限制

1. **`image_strict_mode` 不实现** —— 调研未捕获行为语义；后续补齐后单独立项。
2. **高清海报源（日亚 / Tenhow）不实现** —— P4 插件能力。
3. **幕后场景依赖来源支持** —— 无来源提供时开关为 no-op；实现期核实并如实标注。
4. **Android 壳的 OpenCV 打包不在本期验证** —— 桌面与 Docker 本期闭环。
5. **YuNet 模型只含正面人脸检测** —— 侧脸 / 遮挡回退 Right；模型升级留待后续。
6. **`Metadata.poster_urls` 保持刮削期语义** —— 可能含派生内部 URL；库级差异体现在磁盘产物，
   不改写该列（候选重建的权威输入是 raw 快照 + Resource 记录反解）。
7. **水印逐类开关** —— P3 范围；本期保持现状。
8. **整理期重选引入额外的下载与裁剪开销** —— quality 策略与 Face 策略按库配置显式开启才产生；
   默认路径（legacy + right + priority）零额外开销。
