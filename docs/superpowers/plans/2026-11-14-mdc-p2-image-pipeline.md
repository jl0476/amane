# MDC P2 图片处理链实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 实现图片处理链——分类化裁剪（7 类型 × None/Right/Face）、OpenCV 人脸裁剪（可选 extra + 降级）、择优下载策略（独占临时 + `commit_temp`）、裁剪比例枚举、低清质量底线、剧照幕后场景——默认配置产物逐字节不变。

**架构：** 沿 P0+P1 已落地的覆盖层与生效配置传递接口扩展：`MetadataConfig` 激活预留的 `metadata` 覆盖节；`crop.py` 承载策略规则唯一定义（双入口共用）；`ResourceStore` 增加独占临时择优与 `commit_temp` 绑定提交；整理期重选前置到 poster 复制判定之前，用户产物（box: / locked）优先；幕后图独立持久三层字段、整理期按库筛选。

**技术栈：** Python 3.14 / Pillow（既有）/ opencv-python（新增 `[opencv]` extra）/ pytest；前端仅打包验证无新 UI。

**规格：** `docs/superpowers/specs/2026-11-13-mdc-p2-image-pipeline-design.md`（第 4 版，已过 3 轮复审；计划的全部取值与协议来自它，执行者两份都读）

## 全局约束

- 禁内部导入（`TYPE_CHECKING` 除外）、禁 `hasattr/getattr` 绕类型、禁全局变量；repo 方法显式赋值。
- Alembic 一律 `uv run alembic revision --autogenerate -m "..."`；SQLite 用 `op.batch_alter_table`。
- 覆盖 payload 禁 null；`{}` 仅作可空字段哨兵；新配置模型 `extra="forbid"`。
- OpenCV 走 `[project.optional-dependencies].opencv = ["opencv-python>=5"]`，**不进主依赖**；缺依赖是预期路径（Face 降级），不得让 import 失败中断任务。
- 语言表达遵守 `docs/dev/writing.md` / `terms.md`；提交说明 Conventional Commits + 中文。
- 单测 `uv run pytest <路径> -x -q`；全量 `just check`（本机无 just 时手动四步）。
- **默认产物逐字节不变**（legacy 0.7 + low_res=None）：每个触碰图片产物路径的任务都必须保持基准测试通过。
- 共享缓存安全铁律：落选清理只触碰本轮独占临时文件（`.amane-tmp-` 命名 + task_token），绝不删除 Resource 与既有缓存。
- 每任务一提交；前端 / 打包验证按任务要求执行。

## 审查重点（Review Focus）

1. **双入口策略一致**（materialize 全局 vs 整理库级，含已有物化海报的 none/Face 切换）→ 集成测试在任务 7。
2. **共享缓存安全**（落选清理不触碰 Resource；并发内容冲突按比较重选）→ 集成测试在任务 6。
3. **Face 降级可恢复**（降级产物入 Right 键，补装后重新检测）→ 表测试在任务 5。
4. **低清阈值可达**（同策略/同 skip_ratio 下仅改阈值即改变选择；默认 None 兼容）→ 表测试在任务 8。
5. **用户产物不被覆盖**（box: 手动 / locked / PATCH 外部 URL 三种形态 × 两库不同配置）→ 集成测试在任务 5。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/amane/media/crop.py`（新建） | `CropStrategy` / `CropPlan`（requested/executed）/ `resolve_crop_plan` / 执行函数（Right 复用 crop_poster、Face YuNet、低清算法） |
| `src/amane/media/models/face_detection_yunet_2023mar.onnx`（新建资产） | YuNet 模型，惰性加载 |
| `src/amane/media/images.py` | `crop_poster` 保留为 Right 实现；`allows_poster_crop` 删除 |
| `src/amane/media/resource_store.py` | `acquire_best`（独占临时择优）、`commit_temp`（绑定字节提交） |
| `src/amane/media/pipeline.py` | `materialize_images` 接全局 MetadataConfig；手动裁剪不变 |
| `src/amane/media/__init__.py` | 导出 crop.py 公共面 |
| `src/amane/config/manager.py` | `MetadataConfig` 挂进 `HotSettings.metadata` |
| `src/amane/config/overrides.py` | `metadata` 节注册（白名单自动扩展） |
| `src/amane/handlers/file.py` | `EffectiveOrganizeConfig` 扩展 metadata；重选前置到复制判定之前；`_download_images_via_store` 策略分派 |
| `src/amane/handlers/scrape.py` | `behind_scenes` 落库透传 |
| `src/amane/crawlers/models.py` | `MediaMetadata.behind_scenes` |
| `src/amane/aggregate/models.py`、`src/amane/aggregate/engine.py` | `behind_scenes` 独立聚合 |
| `src/amane/db/models.py` | `Metadata.behind_scenes` JSON 列 |
| `src/amane/db/repo_types.py` | `MetadataFields.behind_scenes` |
| `src/amane/db/migrations/versions/…` | behind_scenes 列迁移 |
| `pyproject.toml` | `[opencv]` extra |
| `tests/media/test_crop.py` 等 | 各任务测试 |
| `docs/dev/data-model.md`、`docs/dev/organize.md`、`docs/user/organize.md` | 文档同步 |

---

### 任务 1：OpenCV extra 与 YuNet 模型实证入库

**文件：**
- 修改：`pyproject.toml`（`[project.optional-dependencies].opencv = ["opencv-python>=5"]`）
- 新建：`src/amane/media/models/face_detection_yunet_2023mar.onnx`（YuNet 官方模型，232 KB）
- 新建：`src/amane/media/models/__init__.py`（空，使目录可打包）

- [ ] **步骤 1：安装 extra 并冒烟**

```bash
uv sync --extra opencv
uv run python -c "
import cv2
det = cv2.FaceDetectorYN.create('src/amane/media/models/face_detection_yunet_2023mar.onnx', '', (320, 320))
print('yunet ok')
"
```

预期：`yunet ok`（模型从 OpenCV 官方 zoo 下载放入；文件入库）。

- [ ] **步骤 2：验证主依赖环境不装 cv2 的降级路径可用**

```bash
uv sync && uv run python -c "
try:
    import cv2
    raise SystemExit('cv2 不应存在')
except ModuleNotFoundError:
    print('degradation path ok')
"
```

预期：`degradation path ok`（先跑本步再跑步骤 1 亦可，顺序不限，两态都要实测）。

- [ ] **步骤 3：Commit**

```bash
git add pyproject.toml uv.lock src/amane/media/models/
git commit -m "chore(media): OpenCV 可选依赖与 YuNet 模型入库"
```

### 任务 2：MetadataConfig 与 metadata 节激活

**文件：**
- 修改：`src/amane/config/manager.py`、`src/amane/config/overrides.py`
- 测试：`tests/config/test_overrides.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_metadata_config_defaults():
    ...  # crop_type 默认矩阵 censored/uncensored=right 其余=none; crop_aspect="legacy";
    # thumb_download_strategy="priority"; crop_low_res_height=None; extra_fanarts_behind_scenes=False

def test_metadata_section_put_and_synthesis(client):
    ...  # PUT /api/libraries/{id}/overrides/metadata 生效; effective-config 含 metadata

def test_metadata_section_reserved_no_longer_422():
    ...  # 预留列表只剩 download/watermark/provider

def test_crop_type_empty_dict_rejected():
    ...  # {"crop_type": {}} → 422 (哨兵不适用不可空 dict); 未知 ContentType 键 → 422; 非法策略值 → 422

def test_unknown_key_rejected():
    ...  # extra=forbid 未知键 422
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/config/test_overrides.py -x -q`
预期：FAIL（MetadataConfig 不存在）

- [ ] **步骤 3：实现**

`MetadataConfig`（`extra="forbid"`）逐字段照抄规格 §3.1 表（`crop_type: dict[ContentType, CropStrategy]` 稀疏默认矩阵、`crop_aspect`、`thumb_download_strategy`、`crop_low_res_height`、`extra_fanarts_behind_scenes`）；注册进 `_SECTION_BINDINGS`；`HotSettings.metadata` 挂载。`OverridePayload` 对 `crop_type: {}` 抛 ValueError（消息指明「重置 = 省略该键」）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/config/ -q`
预期：PASS（既有 common/naming/nfo 用例不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/config tests/config
git commit -m "feat(config): 激活 metadata 覆盖节承载图片处理策略"
```

### 任务 3：CropPlan 与裁剪执行（Right 复用 + 派生键扩展）

**文件：**
- 新建：`src/amane/media/crop.py`
- 修改：`src/amane/media/pipeline.py`（materialize 的 args 与判定改走 crop.py）、`src/amane/media/images.py`（删 `allows_poster_crop`）
- 测试：`tests/media/test_crop.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_resolve_crop_plan_matrix():
    ...  # 表驱动: 7 ContentType × 默认矩阵 + 显式覆盖; requested/executed 初始相等

def test_legacy_ratio_uses_poster_ratio():
    ...  # crop_aspect="legacy" → ratio == scraping.poster_ratio (0.7 与自定义 0.75 各一)

def test_derived_args_encode_executed_strategy():
    ...  # args = f"{executed}:{aspect}:{ratio:.4f}"; 请求 face 无 cv2 → executed=right → args 含 "right"

def test_default_pipeline_output_unchanged(tmp_path):
    ...  # 默认配置下 materialize 裁剪产物与现状 (376, 538) 逐字节一致
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_crop.py -x -q`
预期：FAIL（crop.py 不存在）

- [ ] **步骤 3：实现**

`crop.py` 按 §3.2：`CropStrategy` / `CropPlan(requested, executed, ratio, low_res_height)` / `resolve_crop_plan`；执行函数 Right 复用 `images.py::crop_poster`；派生 args 由 pipeline 的 `f"{ratio:.4f}"` 改为 `f"{executed}:{aspect}:{ratio:.4f}"`（`acquire_derived` 缓存键自动隔离不同配置）。`materialize_images` 的 `crop_enabled` 判定改走 `resolve_crop_plan`（全局作用域）。`allows_poster_crop` 删除（矩阵取代），其既有测试同步迁移。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/ -q`
预期：PASS（含既有 pipeline 测试——默认行为不变）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media tests/media
git commit -m "feat(media): CropPlan 策略解析与派生键按实际策略扩展"
```

### 任务 4：候选重建与用户产物分类

**文件：**
- 新建：`src/amane/media/candidates.py`
- 测试：`tests/media/test_candidates.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_rebuild_routes_through_current_field_chain():
    ...  # 字段优先级与 raw 键序相反 → 重建序与字段链一致; 仅对 poster 字段排除来源 → 被排除来源不参与

def test_auto_derived_urls_excluded():
    ...  # poster_urls 含数值 args 自动派生内部 URL → 从候选排除

def test_box_manual_url_means_user_product():
    ...  # poster_urls 含 box: 内部 URL → rebuild 返回 "user_product" 信号, 不重选

def test_locked_poster_urls_means_user_product():
    ...  # "poster_urls" ∈ locked_fields → "user_product" 信号 (PATCH 外部 URL 场景)

def test_empty_rebuild_falls_back():
    ...  # raw 无候选 → 空候选 + fallback 信号
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_candidates.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`candidates.py`（纯函数 + repo 读取注入）：`rebuild_poster_candidates(metadata, cfg, route) -> CandidateResult`；路由用 `compile_priority`（POSTER 字段，当前配置）作用于 raw 各来源原值；内部 URL 经 `get_by_url_hash` 反解 locator，`box:` → user_product 信号；`poster_urls ∈ locked_fields` → user_product 信号（优先级最高，先判定）。raw 无候选 → 空候选 + fallback 信号。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_candidates.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/candidates.py tests/media/test_candidates.py
git commit -m "feat(media): 海报候选重建经字段链路由与用户产物分类"
```

### 任务 5：整理期重选接线（none 参与 + 用户产物优先）

**文件：**
- 修改：`src/amane/handlers/file.py`（`EffectiveOrganizeConfig` 加 `metadata`；`_place_library_images` poster 分支前置重选）、`src/amane/handlers/scrape.py`（无改动，全局作用域已在 pipeline）
- 测试：`tests/handlers/test_file_ops.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_reselect_none_uses_uncropped_candidate(tmp_path):
    ...  # 既有自动派生 poster (两库): 库 A 配 none → 产物来自未裁剪原始候选; 库 B 配 face/不同 aspect → 各自产物

def test_user_manual_product_survives_reorganize(tmp_path):
    ...  # 手动裁剪 (box:) 后两库不同配置重新整理 → 手动产物不被覆盖

def test_patched_external_poster_survives(tmp_path):
    ...  # raw 保留来源 A, PATCH 外部图片 B (MANUAL+locked) → 整理后产物为 B (默认与不同库级策略)

def test_overwrite_image_false_still_skips(tmp_path):
    ...  # poster 目标已存在 + overwrite_image=False → 跳过 (重选与复制路径都适用)

def test_switch_off_falls_back_to_materialized_copy(tmp_path):
    ...  # 总开关 False → 沿用物化图复制路径 (现状)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/handlers/test_file_ops.py -x -q`
预期：FAIL（重选未实现）

- [ ] **步骤 3：实现**

`EffectiveOrganizeConfig` 加 `metadata: MetadataConfig`（`resolve_effective_config` 增加 metadata 节解析）；`_place_library_images` poster 分支**前置**重选：总开关 True → `rebuild_poster_candidates` → user_product 信号则直接用用户产物；否则按 `CropPlan`（none → 候选原样 / right+face → 裁剪路径）产出 poster；重建为空 → 回退物化图复制。`overwrite_image=False` 既有跳过语义整体保留（重选产物同样适用）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/handlers/ -q`
预期：PASS（既有用例不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/handlers/file.py tests/handlers/test_file_ops.py
git commit -m "feat(handler): 整理期海报重选 (none 参与 用户产物优先 回退路径)"
```

### 任务 6：独占临时择优与 commit_temp

**文件：**
- 修改：`src/amane/media/resource_store.py`
- 测试：`tests/media/test_resource_store.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_acquire_best_picks_largest_area():
    ...  # 三候选不同分辨率 → 面积最大者胜

def test_acquire_best_tie_breaks_by_bytes():
    ...  # 同面积 → 字节数大者胜

def test_acquire_best_partial_failure():
    ...  # 部分候选失败 → 在成功者中择优; failed 清单完整

def test_acquire_best_all_failure():
    ...  # 全失败 → AcquireResult(success=False)

def test_cached_candidate_readonly_in_comparison():
    ...  # 候选命中共享缓存 → 复制到独占临时参与比较; 共享文件不被修改删除

def test_commit_temp_binds_compared_bytes():
    ...  # 提交后持久路径字节 == 参与比较的字节; mock 断言无第二次远端请求

def test_commit_temp_concurrent_existing_record():
    ...  # 提交时已有记录 → 以既有记录为准, 删本轮临时, 返回既有路径

def test_commit_temp_concurrent_different_content_reselects():
    ...  # 提交时同 URL 不同 hash 且更低分辨率缓存 → 既有内容纳入比较 → 本轮高分辨率胜 → 覆盖缓存 + warning; 既有更优 → 返回既有

def test_cancel_cleans_temp(monkeypatch):
    ...  # 比较中途异常 → 本轮独占临时全部清理; 共享缓存不动
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_resource_store.py -x -q`
预期：FAIL（acquire_best / commit_temp 不存在）

- [ ] **步骤 3：实现**

按规格 §3.4：`acquire_best` 独占临时（`.amane-tmp-` + task_token，不写记录不进缓存；命中共享缓存复制参与）；比较面积→字节；`commit_temp(url, temp)` 原子提交绑定字节、并发命中按内容 hash 分派（同 hash 幂等 / 异 hash 纳入比较重选、本轮胜覆盖缓存并同步记录）；清理责任全在本轮。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/ -q`
预期：PASS（既有 acquire/derived 用例不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/resource_store.py tests/media/test_resource_store.py
git commit -m "feat(media): 独占临时择优下载与 commit_temp 绑定提交"
```

### 任务 7：下载策略分派与双入口集成

**文件：**
- 修改：`src/amane/media/resource_store.py`、`src/amane/media/pipeline.py`、`src/amane/handlers/file.py`
- 测试：`tests/handlers/test_file_ops.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_priority_strategy_matches_current_behavior(tmp_path):
    ...  # priority → acquire_first 路径, 产物与现状一致

def test_quality_strategy_picks_best(tmp_path):
    ...  # quality → 两候选 (低分辨率在前) 产出高分辨率者

def test_dual_entry_same_matrix_consistent(tmp_path):
    ...  # 同策略矩阵下 materialize (全局) 与整理 (库级) 产物一致

def test_no_media_file_number_scrape_face_and_none(tmp_path):
    ...  # 无媒体文件按番号刮削: face 与 none 各自持久化 URL 与实际图片符合策略 (全局作用域)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/handlers/test_file_ops.py -x -q`
预期：FAIL（分派未实现）

- [ ] **步骤 3：实现**

`_download_images_via_store` 按 `metadata.thumb_download_strategy` 分派 `acquire_first` / `acquire_best`（thumb 与 poster 候选共用）；materialize 同分派（全局 metadata 配置）；择优产物进入既有裁剪判定（先择优后裁剪）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/handlers/ tests/media/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media src/amane/handlers tests
git commit -m "feat(media): 下载策略分派与双入口集成"
```

### 任务 8：Face 实现（YuNet + 降级 + 多人脸）

**文件：**
- 修改：`src/amane/media/crop.py`
- 测试：`tests/media/test_crop.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_face_crop_aligns_face_window():
    ...  # 构造含人脸样例图 → 裁剪窗水平对齐人脸中心; 越界钳制

def test_multi_face_largest_area_leftmost_tie():
    ...  # 多人脸取面积最大; 并列取最左

def test_no_face_falls_back_to_right():
    ...  # 无人脸 → Right 语义产物

def test_missing_cv2_degrades_to_right():
    ...  # monkeypatch import 失败 → 降级 Right; warning 每任务一次 (contextvars 计数)

def test_model_load_failure_degrades():
    ...  # ONNX 加载失败 → 降级 Right + warning

def test_degraded_result_not_cached_under_face_key():
    ...  # 降级产物派生键含 "right"; 补装后同源同配置重新检测产出 Face
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_crop.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

Face 执行：惰性 `import cv2`（`ModuleNotFoundError` → 降级）→ `FaceDetectorYN` 惰性加载模型（失败 → 降级）→ 检测 → 无人脸回退 / 多人脸按决策 10 → 裁剪窗对齐钳制。降级与回退都更新 `CropPlan.executed`（派生键随之正确）。warning 用 contextvars 任务计数。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_crop.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/crop.py tests/media/test_crop.py
git commit -m "feat(media): YuNet 人脸对齐裁剪与降级路径"
```

### 任务 9：低清决策算法

**文件：**
- 修改：`src/amane/media/crop.py`
- 测试：`tests/media/test_crop.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
@pytest.mark.parametrize("case", [
    "below_threshold_crop_wins",      # 候选 < 阈值, thumb 高 > 候选高 → 裁剪产物
    "at_threshold_candidate_wins",    # 候选 == 阈值 → 用候选原样
    "above_threshold_candidate_wins", # 候选 > 阈值 → 用候选原样 (即使 skip_ratio 判定应裁)
    "both_below_higher_wins",         # 两者都低于阈值 → 高者胜
    "same_height_crop_wins",          # 同高 → 裁剪产物
    "no_candidate_crops",             # 无候选 → 裁剪产物
    "unreadable_size_no_crop",        # 尺寸不可读 → 不裁
    "skip_ratio_short_circuit_none_mode",  # low_res=None 时 skip_ratio 短路 (现状)
    "threshold_overrides_skip_ratio", # 阈值模式不咨询 skip_ratio (b/h < s 也可能用候选)
])
def test_low_res_decision_matrix(case):
    ...
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_crop.py -x -q`
预期：FAIL（阈值模式未实现）

- [ ] **步骤 3：实现**

按规格 §3.3 判定顺序实现（阈值启用时取代 skip_ratio 短路）；可达性断言：b=500/h=538/s=0.9 下 T=None 与 T=800 选择不同。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_crop.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/crop.py tests/media/test_crop.py
git commit -m "feat(media): 低清质量底线决策算法"
```

### 任务 10：剧照幕后场景（三层字段 + 迁移 + 筛选）

**文件：**
- 修改：`src/amane/crawlers/models.py`、`src/amane/aggregate/models.py`、`src/amane/aggregate/engine.py`、`src/amane/db/models.py`、`src/amane/db/repo_types.py`、`src/amane/handlers/scrape.py`、`src/amane/handlers/file.py`
- 新建：Alembic 迁移（`Metadata.behind_scenes` JSON 列）
- 测试：`tests/aggregate/test_engine.py`、`tests/handlers/test_file_ops.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_behind_scenes_persisted_regardless_of_toggle():
    ...  # 开关开/关都落库 behind_scenes (原始数据归属)

def test_organize_filters_behind_scenes_by_library(tmp_path):
    ...  # 同番号两库相反开关 → extrafanart 集合各按开关; 普通剧照不受影响

def test_scrape_off_organize_on_no_rescrape(tmp_path):
    ...  # 刮削关 → 整理开: 无需重刮, 重新整理即生效

def test_no_source_noop():
    ...  # 来源未提供 behind_scenes → 开关 no-op
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/aggregate/test_engine.py tests/handlers/test_file_ops.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现与迁移**

三层字段：`MediaMetadata.behind_scenes`（默认 []）→ `AggregatedMetadata.behind_scenes: dict[str, list[str]]`（按来源分组聚合）→ `Metadata.behind_scenes` JSON 列（迁移）；`ScrapeHandler` 落库透传；整理期 `_download_images_via_store` 按库开关把 behind_scenes 候选追加到 extrafanart 集合（普通在前、幕后在后、既有键去重、经命名模板）。来源支持度核实结果写入 `docs/dev/data-model.md`。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/aggregate/ tests/handlers/ tests/db/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane tests src/amane/db/migrations/versions
git commit -m "feat(media): 剧照幕后场景独立持久与库级筛选"
```

### 任务 11：demo 可视对比页

**文件：**
- 新建：`docs/superpowers/demos/2026-11-14-mdc-p2-crop-demo.html`（静态页，含内嵌 base64 样张）

- [ ] **步骤 1：用实现生成对比样张**

构造同一样张（含人脸位置信息），分别以 None/Right/Face 与 aspect 三值渲染产物，base64 内嵌静态 HTML 页（None/Right/Face 并排 + aspect 对比 + 择优前后对比；中文无乱码）。

- [ ] **步骤 2：人工验收**

浏览器打开页面确认：三策略差异可见、aspect 对比可见、无乱码。demo 是一次性产物，按需保留或删除。

- [ ] **步骤 3：Commit**

```bash
git add docs/superpowers/demos/
git commit -m "docs(demo): P2 裁剪策略可视对比页"
```

### 任务 12：打包验证与文档同步

**文件：**
- 修改：`docs/dev/data-model.md`（behind_scenes 字段）、`docs/dev/organize.md`（裁剪策略 / 下载策略 / 低清算法 / 降级）、`docs/user/organize.md`（用户可见行为）、`docs/dev/index.md`（如需）
- 验证：`just check`（本机手动四步）+ 打包冒烟

- [ ] **步骤 1：文档同步**

先精简再加；`metadata` 覆盖节字段、降级行为、`image_strict_mode` 已知限制如实标注。

- [ ] **步骤 2：打包冒烟**

PyInstaller 桌面版构建后启动确认 `import cv2`（按 extra 装配）与 YuNet 加载正常；Docker 镜像同验。

- [ ] **步骤 3：全量检查**

运行：`just check`（或手动 lint/typecheck/web/test 四步）
预期：PASS

- [ ] **步骤 4：Commit**

```bash
git add docs src pyproject.toml uv.lock
git commit -m "docs: 同步 MDC P2 图片处理链知识库并验证打包"
```

---

## 自检记录

- **规格覆盖度**：§3.1（任务 2）、§3.2（任务 3/4/5）、§3.3（任务 9）、§3.4（任务 6/7）、§3.5（任务 10）、§3.6（任务 4/5）、§3.7（任务 1/12）、§4（各任务测试行）、§5（任务内测试与规格测试表逐行对应）、§6（任务编号即其步骤展开）、§7 已知限制（任务 10 文档标注 + 任务 12 文档同步）。
- **类型一致性**：`CropPlan(requested, executed, ratio, low_res_height)` 任务 3 定义、任务 5/8/9 引用；`MetadataConfig` 任务 2 定义、任务 5/7/10 引用；`acquire_best`/`commit_temp` 任务 6 定义、任务 7 接线；`rebuild_poster_candidates` 任务 4 定义、任务 5 接线。
- **审查重点**：五条分别钉在任务 7（双入口一致）、6（共享缓存安全）、5（Face 降级可恢复 + 用户产物不被覆盖）、9（低清可达）、5（用户产物三形态）。
- **比例**：计划约为规格 60% 篇幅；代码块只有测试骨架断言与规格定死的字面量，无函数体。
