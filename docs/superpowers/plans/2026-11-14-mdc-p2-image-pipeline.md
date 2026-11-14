# MDC P2 图片处理链实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 实现图片处理链——分类化裁剪（7 类型 × None/Right/Face）、OpenCV 人脸裁剪（可选 extra + 降级）、择优下载策略（独占临时 + `commit_temp`）、裁剪比例枚举、低清质量底线、剧照幕后场景——默认配置产物逐字节不变。

**架构：** 沿 P0+P1 已落地的覆盖层与生效配置传递接口扩展：`MetadataConfig` 激活预留的 `metadata` 覆盖节（含 API/schema/client 联动）；`CropStrategy` 放**中立基础层 `enums.py`**（config 与 media 均已依赖 enums，避免 `config.manager → media.crop → media.__init__ → config` 的包初始化循环，R2 PLAN-01）；`crop.py` 承载策略规则唯一定义（双入口共用）；Face **检测先于派生键构造**（键按实际执行策略）；异步协调（候选重建 / 策略解析 / 缓存提交）在 `_download_images_via_store` 前的异步层完成，同步落盘 helper 只接收已准备的本地路径；整理期重选前置到 poster 复制判定之前，用户产物（box: 手动 / locked / PATCH 外部）优先；幕后图独立持久三层字段、刮削物化与整理期双消费。

**技术栈：** Python 3.14 / Pillow（既有）/ opencv-python（新增 `[opencv]` extra）/ pytest；前端 `just generate` + i18n（新增配置节）。

**规格：** `docs/superpowers/specs/2026-11-13-mdc-p2-image-pipeline-design.md`（第 4 版，R4 PASS；计划的全部取值与协议来自它，执行者两份都读）

## 全局约束

- 禁内部导入（`TYPE_CHECKING` 除外）——**OpenCV 经专用访问器模块的模块级 try-import 加载**（任务 3 的 `optional_cv2.py`），不在函数体内 import；YuNet 模型文件仍惰性加载
- **`CropStrategy` 定义于 `src/amane/enums.py`**（中立基础层，R2 PLAN-01 修订）——`CropPlan` / 执行函数在 `media/crop.py` 并从 enums 导入
- Alembic 一律 `uv run alembic revision --autogenerate -m "..."`；SQLite 用 `op.batch_alter_table`
- 覆盖 payload 禁 null；`{}` 仅作可空字段哨兵（`crop_type: {}` → 422）；新配置模型 `extra="forbid"`
- 语言表达遵守 `docs/dev/writing.md` / `terms.md`；提交说明 Conventional Commits + 中文
- 单测 `uv run pytest <路径> -x -q`；全量 `just check`（本机无 just 时手动四步）
- **默认产物逐字节不变**（legacy 0.7 + low_res=None）：每个触碰图片产物路径的任务都必须保持基准测试通过
- 共享缓存安全铁律：落选清理只触碰本轮独占临时文件（`.amane-tmp-` 命名 + task_token），绝不删除 Resource 与既有缓存
- 每任务一提交；**每个提交结束必须可导入、可运行其声明的测试**（删除符号与导出/调用者迁移同提交；新增调用者与被调函数同提交）

## 审查重点（Review Focus）

1. **双入口策略一致**（materialize 全局 vs 整理库级，含已有物化海报的 none/Face 切换与总开关回退）→ 集成测试在任务 10。
2. **共享缓存安全**（落选清理不触碰 Resource；并发内容冲突纳入比较重选）→ 集成测试在任务 9。
3. **Face 降级可恢复 + 语义对齐设计**（检测先于键构造；无人脸/异常均按 Right 键降级）→ 集成测试在任务 7。
4. **低清阈值可达**（同策略/同 skip_ratio 下仅改阈值即改变选择；默认 None 兼容）→ 表测试在任务 11。
5. **用户产物不被覆盖**（box: 手动 / locked / PATCH 外部 URL 三种形态 × 两库不同配置）→ 集成测试在任务 10。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/amane/enums.py` | **`CropStrategy` 枚举（中立基础层，R2 PLAN-01）** |
| `src/amane/media/optional_cv2.py`（新建） | OpenCV 可选访问器：模块级 try-import，`available()` / `module()` |
| `src/amane/media/crop.py`（新建） | `CropPlan`（requested/executed）/ `resolve_crop_plan`（含低清决策）/ 执行函数（Right 复用 crop_poster、Face YuNet、检测先于键构造） |
| `src/amane/media/models/face_detection_yunet_2023mar.onnx`（新建资产） | YuNet 模型，惰性加载 |
| `src/amane/media/candidates.py`（新建） | 海报候选重建（字段链路由 + 用户产物分类），repo 数据注入式纯函数 |
| `src/amane/media/images.py` | `crop_poster` 保留为 Right 实现；`allows_poster_crop` 删除（含导出/调用者迁移） |
| `src/amane/media/resource_store.py` | `acquire_best`（独占临时择优）、`commit_temp`（绑定字节提交） |
| `src/amane/media/pipeline.py` | `materialize_images` 接全局 MetadataConfig 与下载策略分派；手动裁剪不变 |
| `src/amane/config/manager.py` | `MetadataConfig` 挂进 `HotSettings.metadata` |
| `src/amane/config/overrides.py` | `metadata` 节注册（白名单自动扩展） |
| `src/amane/api/models/libraries.py`、`src/amane/api/routes/libraries.py` | `EffectiveConfigResponse` 扩展 metadata 节 |
| `src/amane/handlers/file.py` | `EffectiveOrganizeConfig` 扩展 metadata；异步协调 `_prepare_poster_product`；重选前置；下载策略分派 |
| `src/amane/handlers/scrape.py` | `behind_scenes` 落库透传；materialize 新签名调用者 |
| `src/amane/crawlers/models.py`、`src/amane/aggregate/models.py`、`src/amane/aggregate/engine.py` | `behind_scenes` 独立聚合 |
| `src/amane/db/models.py`、`src/amane/db/repo_types.py`、`src/amane/db/repos/metadata.py` | `Metadata.behind_scenes` 列 + 显式写入 + `PartialMetadata` 排除 |
| `src/amane/db/migrations/versions/…` | behind_scenes 列迁移 |
| `scripts/build_windows_app.ps1`、`scripts/build_macos_app.sh`、`Dockerfile` | ONNX 资产收集 / opencv extra 装配 |
| `pyproject.toml` | `[opencv]` extra |
| `web/openapi.json`、`web/src/client/…`（`just generate` 产物） | API 联动 |
| `tests/media/test_crop.py` 等 | 各任务测试 |
| `docs/dev/config.md`、`docs/dev/data-model.md`、`docs/dev/organize.md`、`docs/user/organize.md`、`docs/dev/index.md` | 文档同步 |

---

### 任务 1：OpenCV extra 与 YuNet 模型实证入库

**文件：**
- 修改：`pyproject.toml`（`[project.optional-dependencies].opencv = ["opencv-python>=5"]`）
- 新建：`src/amane/media/models/face_detection_yunet_2023mar.onnx`（YuNet 官方模型，232 KB）、`src/amane/media/models/__init__.py`

- [ ] **步骤 1：两态实测**

```bash
uv run python -c "
try:
    import cv2
    raise SystemExit('cv2 不应存在')
except ModuleNotFoundError:
    print('degradation path ok')
"
uv sync --extra opencv
uv run python -c "
import cv2
det = cv2.FaceDetectorYN.create('src/amane/media/models/face_detection_yunet_2023mar.onnx', '', (320, 320))
print('yunet ok')
"
```

预期：先 `degradation path ok`，后 `yunet ok`（模型从 OpenCV 官方 zoo 下载入库）。

- [ ] **步骤 2：Commit**

```bash
git add pyproject.toml uv.lock src/amane/media/models/
git commit -m "chore(media): OpenCV 可选依赖与 YuNet 模型入库"
```

### 任务 2：demo 前置交付（设计 §5 要求：实施前确认效果）

**文件：**
- 新建：`docs/superpowers/demos/2026-11-14-mdc-p2-crop-demo.html`（静态页，样张 base64 内嵌）
- 一次性原型脚本放临时目录，不提交

- [ ] **步骤 1：原型脚本生成对比样张**

用 Pillow 直写三策略原型（None=原图、Right=现有 `crop_poster` 逻辑、Face=YuNet 原型检测对齐）与 aspect 三值，产出对比 HTML（None/Right/Face 并排 + aspect 三值对比 + 择优前后对比；中文无乱码）。原型脚本标注为一次性产物。

- [ ] **步骤 2：用户确认效果**

人工打开页面验收：三策略差异可见、aspect 对比可见、无乱码。**确认结果记录后本任务才关闭**；验收口径变化则回改设计再继续。

- [ ] **步骤 3：Commit**

```bash
git add docs/superpowers/demos/
git commit -m "docs(demo): P2 裁剪策略可视对比页 (实施前验收)"
```

### 任务 3：CropStrategy 入 enums + crop.py 策略中立面

**文件：**
- 修改：`src/amane/enums.py`（`CropStrategy` 中立层）
- 新建：`src/amane/media/optional_cv2.py`、`src/amane/media/crop.py`
- 测试：`tests/media/test_crop.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_crop_strategy_in_enums_neutral_layer():
    ...  # CropStrategy ∈ amane.enums; 干净进程先 import amane.config 再 import amane.media 均成功 (R2 PLAN-01 回归)

def test_optional_cv2_accessor_two_states():
    ...  # optional_cv2.available() 两态可测 (monkeypatch 模块存在性); module() 返回 None 或模块

def test_resolve_crop_plan_defaults():
    ...  # 7 ContentType 默认矩阵 (censored/uncensored=right 其余=none); requested == executed

def test_legacy_ratio_uses_poster_ratio():
    ...  # crop_aspect="legacy" → ratio == 传入的 poster_ratio (0.7 与自定义 0.75 各一); emby=2/3; full=2.12/3

def test_low_res_none_uses_skip_ratio_short_circuit():
    ...  # low_res=None: b/h ≥ skip_ratio → 用候选 (现状); b/h < skip_ratio → 裁剪 (现状)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_crop.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`enums.py` 加 `CropStrategy(StrEnum)`（none/right/face——**中立层：enums 不依赖 config/media**，两侧均可导入）。`optional_cv2.py`：模块级 `try: import cv2 / except ModuleNotFoundError: cv2 = None`，提供 `available()` / `module()`。`crop.py`（从 `..enums` 导入 CropStrategy）：`CropPlan(requested, executed, ratio, low_res_height)`；`resolve_crop_plan(content_type, *, strategy_map, aspect, low_res_height, poster_ratio, thumb_size, candidate_size) -> CropPlan` —— **纯输入参数签名，不引用 MetadataConfig**（该模型任务 4 才存在）；含 §3.3 默认态算法（skip_ratio 短路）。本任务不含 Face 执行与任何接线。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_crop.py -q && uv run python -c "import amane.config; import amane.media; print('no cycle')"`
预期：PASS 且双序导入无环

- [ ] **步骤 5：Commit**

```bash
git add src/amane/enums.py src/amane/media/optional_cv2.py src/amane/media/crop.py tests/media/test_crop.py
git commit -m "feat(media): CropStrategy 入中立层与裁剪策略中立面"
```

### 任务 4：MetadataConfig 激活 metadata 节（含 API/schema/client 联动）

**文件：**
- 修改：`src/amane/config/manager.py`、`src/amane/config/overrides.py`、`src/amane/config/__init__.py`（导出）、`src/amane/api/models/libraries.py`（`EffectiveConfigResponse` 加 metadata）、`src/amane/api/routes/libraries.py`（`get_effective_config` 构造扩展）、`web/openapi.json` 与 `web/src/client/…`（`just generate` 产物）、`web/src/i18n/locales/{zh-CN,en}/settings.json`（新节翻译）
- 测试：`tests/config/test_overrides.py`、`tests/api/test_library_overrides.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/config/test_overrides.py
def test_metadata_config_defaults():
    ...  # 默认矩阵 / legacy / priority / None / False (规格 §3.1 表); crop_type 引用 enums.CropStrategy

def test_crop_type_empty_dict_rejected():
    ...  # {"crop_type": {}} → 422; 未知 ContentType 键 → 422; 非法策略值 → 422

# tests/api/test_library_overrides.py (API fixture 所在)
def test_metadata_section_put_effective_delete_roundtrip(client):
    ...  # PUT metadata → GET effective-config 含 metadata 节 (合成) → DELETE 回继承; 实际 HTTP 往返

def test_reserved_list_no_longer_contains_metadata(client):
    ...  # 预留 section 拒绝列表更新: download/watermark/provider 仍 422; metadata 不再 422
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/config/test_overrides.py tests/api/test_library_overrides.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`MetadataConfig`（`crop_type` 引用 `enums.CropStrategy`）挂进 `HotSettings.metadata` + `_SECTION_BINDINGS`；`EffectiveConfigResponse` 与 `get_effective_config` 扩展 metadata 节；`config/__init__.py` 导出新模型；PUT/DELETE 白名单断言更新。随后：

```bash
just generate
```

检查 `web/openapi.json` / `web/src/client/types.gen.ts` 差异并随本任务提交；`web/src/i18n` 补新节翻译。

- [ ] **步骤 4：运行测试与前端检查验证通过**

运行：`uv run pytest tests/config/ tests/api/ -q && cd web && pnpm check`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/config src/amane/api tests web/openapi.json web/src/client web/src/i18n
git commit -m "feat(config): 激活 metadata 覆盖节并联动 API schema 与 client"
```

### 任务 5：候选重建与用户产物分类

**文件：**
- 新建：`src/amane/media/candidates.py`
- 测试：`tests/media/test_candidates.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_rebuild_routes_through_current_field_chain():
    ...  # 字段优先级与 raw 键序相反 → 重建序与字段链一致; 仅对 poster 字段排除来源 → 被排除来源不参与; 快照含已停用来源 → 不参与

def test_auto_derived_urls_excluded():
    ...  # poster_urls 含数值 args 自动派生内部 URL → 从候选排除

def test_box_manual_url_means_user_product():
    ...  # poster_urls 含 box: 内部 URL (经 get_by_url_hash 反解 locator) → user_product 信号

def test_locked_poster_urls_means_user_product():
    ...  # "poster_urls" ∈ locked_fields → user_product 信号 (PATCH 外部 URL 场景)

def test_empty_rebuild_falls_back():
    ...  # raw 无候选 → 空候选 + fallback 信号
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_candidates.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`candidates.py`：`rebuild_poster_candidates(...) -> CandidateResult`（候选列表 + user_product 信号 + fallback 信号）。**repo/资源读取由调用方异步完成后以参数注入**（raw、locked_fields、Resource 反解结果）——本模块保持纯函数；`box:` 判定优先于 `locked_fields`（次序：先 user_product 短路，再排除自动派生，再路由）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_candidates.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/candidates.py tests/media/test_candidates.py
git commit -m "feat(media): 海报候选重建经字段链路由与用户产物分类"
```

### 任务 6：Right 双入口接线与派生键扩展（默认态基准）

**文件：**
- 修改：`src/amane/media/pipeline.py`、`src/amane/media/images.py`（删 `allows_poster_crop`）、`src/amane/media/__init__.py`（导出迁移）、`src/amane/handlers/file.py`（调用者迁移）、`src/amane/handlers/scrape.py`（materialize 新签名调用者）
- 测试：`tests/media/test_pipeline.py`、`tests/media/test_images.py`（迁移）

- [ ] **步骤 1：编写失败的测试**

```python
def test_derived_args_encode_executed_strategy():
    ...  # args = f"{executed}:{aspect}:{ratio:.4f}"; legacy → "right:legacy:0.7000"

def test_materialize_global_scope_right(tmp_path):
    ...  # materialize 按全局 MetadataConfig 裁剪; 默认 legacy 0.7 → 产物 (376, 538) 逐字节一致

def test_allows_poster_crop_removed():
    ...  # media 包不再导出 allows_poster_crop; handlers 无残留引用 (导入检查)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_pipeline.py tests/media/test_images.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`materialize_images` 增加 `metadata_cfg: MetadataConfig` 参数（**调用者 `handlers/scrape.py` 同步迁移**，从 `config.hot.metadata` 传入——全局作用域；新参数与调用者同提交）；裁剪判定改走 `resolve_crop_plan`（§3.3 默认态）+ `crop.py` Right 执行；派生 args 扩展。**`allows_poster_crop` 删除与 `media/__init__.py` 导出、`handlers/file.py` 导入/调用迁移同一提交**（提交结束可导入）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/ tests/handlers/ -q && uv run python -c "import amane.media, amane.handlers.file, amane.handlers.scrape"`
预期：PASS 且导入成功

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media src/amane/handlers/file.py src/amane/handlers/scrape.py tests/media
git commit -m "feat(media): Right 策略双入口接线与派生键扩展"
```

### 任务 7：Face 实现（检测先于派生键构造）

**文件：**
- 修改：`src/amane/media/crop.py`
- 测试：`tests/media/test_crop.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_face_detection_before_key_construction():
    ...  # thumb 先下载并本地检测 → 按检测结果固化 executed 与裁剪信息 → 再调正确策略的 acquire_derived
    # 从真实 acquire_derived 入口核对 Resource locator/meta 四态:
    #   face 检出 → "face:*" 键; 无人脸 → "right:*" 键; 检测异常 → "right:*" 键 + warning; 后端不可用 → "right:*" 键

def test_face_crop_aligns_face_window():
    ...  # 含人脸样例图 → 裁剪窗水平对齐人脸中心; 越界钳制

def test_multi_face_largest_area_leftmost_tie():
    ...  # 多人脸面积最大; 并列取最左

def test_backend_unavailable_probe_degrades_right():
    ...  # cv2/模型不可用 → executed=right → Right 键缓存; warning 每任务一次 (contextvars)

def test_runtime_detection_exception_degrades_right_key():
    ...  # 运行期检测异常 → 降级 Right + warning → Right 键缓存 (设计 §4 口径), 不回退原候选
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_crop.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

**检测先于键构造**（R2 PLAN-05 修订）：Face 请求的流程 = thumb 已在本地（调用方保证）→ 运行检测（`optional_cv2` + 惰性模型加载）→ **按检测结果固化 `executed` 与裁剪信息**（检出 → executed=face + box；无人脸 → executed=right；检测异常 → executed=right + warning）→ 以固化 executed 构造派生键调用 `acquire_derived`（face 检出时 producer 使用**预计算的 box** 闭包裁剪，缓存 miss 重跑同 box 确定性一致）。后端不可用探针（`optional_cv2.available()` + 模型可加载）同样在键构造前执行 → executed=right + warning。**face 键永不存储 Right 降级产物**（无人脸/异常均落 Right 键，对齐设计 §3.2/§4）。不修改 frozen 实例。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_crop.py tests/media/test_resource_store.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/crop.py tests/media/test_crop.py
git commit -m "feat(media): YuNet 人脸对齐裁剪与检测先于键构造的降级契约"
```

### 任务 8：整理期重选接线（异步协调边界 + none 参与 + 用户产物优先）

**文件：**
- 修改：`src/amane/handlers/file.py`
- 测试：`tests/handlers/test_file_ops.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_reselect_none_uses_uncropped_candidate(tmp_path):
    ...  # 既有自动派生 poster: 库 A 配 none → 产物来自未裁剪原始候选; 库 B 配 face / 不同 aspect → 各自产物
    # 集成经真实异步协调路径 (execute_file_operations), 检查磁盘实际图片

def test_user_manual_product_survives_reorganize(tmp_path):
    ...  # 手动裁剪 (box:) 后两库不同配置重新整理 → 手动产物不被覆盖

def test_patched_external_poster_survives(tmp_path):
    ...  # raw 保留来源 A, PATCH 外部图片 B (MANUAL+locked) → 整理后产物为 B (默认与不同库级策略)

def test_overwrite_image_false_still_skips(tmp_path):
    ...  # poster 目标已存在 + overwrite_image=False → 跳过 (重选与复制路径都适用), skipped 计数

def test_switch_off_falls_back_to_materialized_copy(tmp_path):
    ...  # 总开关 False → 沿用物化图复制路径 (现状); 记日志

def test_empty_rebuild_falls_back(tmp_path):
    ...  # raw 无候选 → 回退物化图复制, 记日志
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/handlers/test_file_ops.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现（同步/异步边界，R1 PLAN-02 已闭合项的落实）**

**异步协调函数** `handlers/file.py::_prepare_poster_product(...)`（async，`execute_file_operations` 内、`_download_images_via_store` 之前调用）：完成 `rebuild_poster_candidates`（含 repo/raw/locked/Resource 反解等异步读取）、`resolve_crop_plan` 与策略执行——none 用 `store.acquire` 下载选中候选，right/face 经 `acquire_derived`（任务 7 的检测先于键构造流程）——产出 `PreparedPoster(local_path, origin)`。**同步 helper `_place_library_images` 只接收已准备的本地路径**，保持 `@in_thread` 文件复制/水印职责；user_product / fallback 信号决定 poster 来源与 skipped 计数；`overwrite_image=False` 既有跳过语义保留。

**metadata 参数传递路径**（完整链）：`OrganizeHandler.handle → resolve_effective_config（+metadata 节）→ EffectiveOrganizeConfig → apply_file_operations → execute_file_operations → _prepare_poster_product → _download_images_via_store → _place_library_images`。刮削入口：`ScrapeHandler.handle → materialize_images(metadata_cfg=config.hot.metadata)`（全局作用域，任务 6 已接）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/handlers/ -q`
预期：PASS（经真实异步协调路径，无未等待协程）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/handlers/file.py tests/handlers/test_file_ops.py
git commit -m "feat(handler): 整理期海报重选异步协调与用户产物优先"
```

### 任务 9：acquire_best / commit_temp / 并发内容冲突比较

**文件：**
- 修改：`src/amane/media/resource_store.py`
- 测试：`tests/media/test_resource_store.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_acquire_best_picks_largest_area():
    ...  # 三候选不同分辨率 → 面积最大者胜

def test_acquire_best_tie_breaks_by_bytes():
    ...  # 同面积 → 字节数大者胜

def test_acquire_best_partial_and_all_failure():
    ...  # 部分失败在成功者中择优; 全失败 success=False

def test_cached_candidate_readonly_in_comparison():
    ...  # 候选命中共享缓存 → 复制到独占临时参与比较; 共享文件不被修改删除

def test_commit_temp_binds_compared_bytes():
    ...  # 提交后持久路径字节 == 参与比较的字节; mock 断言无第二次远端请求

def test_commit_temp_same_hash_idempotent():
    ...  # 并发同 URL 同 hash → 删本轮临时、返回既有路径

def test_commit_temp_different_hash_lower_res_reselects():
    ...  # 并发同 URL 更低分辨率缓存 → 既有纳入比较 → 本轮高分辨率胜 → 覆盖缓存 (记录+文件同步) + warning

def test_commit_temp_different_hash_existing_better_returns_existing():
    ...  # 既有更优 → 返回既有路径; 本轮临时删除

def test_commit_failure_cleans_temp():
    ...  # 提交磁盘错误 → 删本轮临时、返回 FAILED

def test_cancel_cleans_temp(monkeypatch):
    ...  # 比较中途异常 → 本轮独占临时全部清理; 共享缓存不动
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_resource_store.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

按规格 §3.4：独占临时（`.amane-tmp-` + task_token，不写记录不进缓存；命中共享缓存复制参与）；比较面积→字节；`commit_temp(url, temp)` 原子提交绑定字节、**并发不同内容纳入比较重选**（既有优返回既有 / 本轮优覆盖并同步记录）、同 hash 幂等；提交失败删临时返 FAILED；清理责任全在本轮。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/ -q`
预期：PASS（既有用例不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/resource_store.py tests/media/test_resource_store.py
git commit -m "feat(media): 独占临时择优 commit_temp 与并发内容冲突比较"
```

### 任务 10：quality/priority 双入口分派（先择优后裁剪）

**文件：**
- 修改：`src/amane/media/pipeline.py`、`src/amane/handlers/file.py`
- 测试：`tests/handlers/test_file_ops.py`、`tests/media/test_pipeline.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_priority_strategy_matches_current_behavior(tmp_path):
    ...  # priority → acquire_first 路径, 产物与现状一致 (刮削与整理两入口)

def test_quality_strategy_picks_best_both_entries(tmp_path):
    ...  # 两入口输入低分辨率候选在前、高分辨率在后 → quality 产出高分辨率者 (priority 产出首个)
    # 检查最终磁盘图片/持久化 URL, 不只断言 acquire_best 返回值

def test_quality_dispatch_respects_library_scope(tmp_path):
    ...  # 两库不同 thumb_download_strategy → 各自入口按各自生效配置分派
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/handlers/test_file_ops.py tests/media/test_pipeline.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

**双入口分派**（R2 PLAN-09 修订）：thumb 与 poster 候选获取处按生效 `thumb_download_strategy` 分派 `acquire_first` / `acquire_best`——刮削入口在 `materialize_images`（全局配置），整理入口在 `_prepare_poster_product` 的候选获取处（库级配置）；**先择优后裁剪**：quality 胜者即为后续 CropPlan 裁剪/选样的输入，`_prepare_poster_product` 的候选获取先于裁剪决策。`priority` 沿用 `acquire_first` 零改动。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/handlers/ tests/media/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/pipeline.py src/amane/handlers/file.py tests
git commit -m "feat(media): quality/priority 双入口分派 (先择优后裁剪)"
```

### 任务 11：低清阈值模式（全边界 + 可达性）

**文件：**
- 修改：`src/amane/media/crop.py`
- 测试：`tests/media/test_crop.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
@pytest.mark.parametrize("case", [
    "below_threshold_crop_wins",        # 候选 < 阈值, thumb 高 > 候选高 → 裁剪产物
    "at_threshold_candidate_wins",      # 候选 == 阈值 → 用候选原样
    "above_threshold_candidate_wins",   # 候选 > 阈值 → 用候选原样 (即使 skip_ratio 判定应裁)
    "both_below_higher_wins",           # 两者都低于阈值 → 高者胜
    "same_height_candidate_wins",       # 同高 → 用候选原样 (设计 §3.3: h ≤ b → 候选)
    "no_candidate_crops",               # 无候选 → 裁剪产物
    "thumb_unreadable_no_crop",         # thumb 尺寸不可读 → 不裁
    "candidate_unreadable_crops",       # 候选尺寸不可读 → 裁剪产物 (thumb 可用)
    "threshold_overrides_skip_ratio",   # b/h < skip_ratio 但候选 ≥ 阈值 → 用候选 (阈值取代短路)
    "default_none_matches_current",     # low_res=None → skip_ratio 短路 (现状逐字节)
])
def test_low_res_decision_matrix(case):
    ...
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_crop.py -x -q`
预期：FAIL（阈值模式未实现）

- [ ] **步骤 3：实现**

按规格 §3.3 判定顺序实现阈值分支；**可达性断言**：b=500/h=538/s=0.9 下 T=None 与 T=800 选择不同（复审验收）；`thumb_unreadable` 与 `candidate_unreadable` 分列为独立用例（期望不同：前者不裁、后者裁剪）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_crop.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/crop.py tests/media/test_crop.py
git commit -m "feat(media): 低清质量底线决策算法全边界"
```

### 任务 12：剧照幕后场景（三层字段 + 双消费 + 仓储/API 契约）

**文件：**
- 修改：`src/amane/crawlers/models.py`、`src/amane/aggregate/models.py`、`src/amane/aggregate/engine.py`、`src/amane/db/models.py`、`src/amane/db/repo_types.py`、`src/amane/db/repos/metadata.py`、`src/amane/api/models/metadata.py`（`PartialMetadata` ignore）、`src/amane/handlers/scrape.py`、`src/amane/media/pipeline.py`、`src/amane/handlers/file.py`
- 新建：Alembic 迁移（`Metadata.behind_scenes` JSON 列）
- 测试：`tests/aggregate/test_engine.py`、`tests/handlers/test_file_ops.py`、`tests/db/test_metadata_repo.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_behind_scenes_persisted_regardless_of_toggle():
    ...  # 开关开/关都落库 behind_scenes (原始数据归属)

def test_materialize_consumes_behind_scenes_global(tmp_path):
    ...  # 无媒体文件、全局开关开启的 SCRAPE 实际物化幕后图; 开关关闭仍持久化但不物化

def test_organize_filters_behind_scenes_by_library(tmp_path):
    ...  # 同番号两库相反开关 → extrafanart 集合各按开关; 普通剧照不受影响

def test_scrape_off_organize_on_no_rescrape(tmp_path):
    ...  # 刮削关 → 整理开: 无需重刮, 重新整理即生效

def test_no_source_noop():
    ...  # 来源未提供 behind_scenes → 开关 no-op

def test_metadata_repo_roundtrip_and_patch_excluded():
    ...  # update_metadata 显式写入 behind_scenes; PATCH (PartialMetadata) 不含该字段; 旧 Metadata 行升级后得到空集合
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/aggregate/test_engine.py tests/handlers/test_file_ops.py tests/db/test_metadata_repo.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现与迁移**

三层字段（`MediaMetadata.behind_scenes` → `AggregatedMetadata.behind_scenes` 按来源分组 → `Metadata.behind_scenes` JSON 列 + 迁移，旧行升级为空集合）；`MetadataFields` 加键 + `update_metadata` **显式赋值行** + `PartialMetadata` ignore 加 `"behind_scenes"`（后端写入，PATCH 不可写，沿 `field_sources` 先例）；`ScrapeHandler` 落库透传；**刮削物化消费**：`materialize_images` 全局开关开启时把 behind_scenes 候选并入 extrafanart 物化；**整理消费**：按库开关追加（普通在前、幕后在后、既有键去重、经命名模板）。来源支持度核实结果写入 `docs/dev/data-model.md`。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/aggregate/ tests/handlers/ tests/db/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane tests src/amane/db/migrations/versions
git commit -m "feat(media): 剧照幕后场景三层持久 物化与整理双消费"
```

### 任务 13：打包验证与文档同步

**文件：**
- 修改：`scripts/build_windows_app.ps1`、`scripts/build_macos_app.sh`（`media/models/*.onnx` 数据收集）、`Dockerfile`（`uv sync --extra opencv`）、`docs/dev/config.md`（metadata 节状态）、`docs/dev/data-model.md`、`docs/dev/organize.md`、`docs/user/organize.md`、`docs/dev/index.md`
- 验证：全量四步 + 打包冒烟

- [ ] **步骤 1：打包脚本与 Docker 装配**

Windows/macOS 脚本补 `media/models/*.onnx` 数据收集；Dockerfile 的 `uv sync` 加 `--extra opencv`。

- [ ] **步骤 2：两态打包冒烟（从独立目录运行，不依赖仓库源码）**

带 extra 产物：内置模型存在 + `FaceDetectorYN` 实际完成一次人脸检测；无 extra 产物：`import cv2` 失败走 Right 降级。Docker 对应变体同验。

- [ ] **步骤 3：实现后回归对比页**

用正式实现重新生成任务 2 的对比页（回归产物，验证实现与原型一致），另存 `docs/superpowers/demos/2026-11-14-mdc-p2-crop-demo-final.html`。

- [ ] **步骤 4：文档同步**

先精简再加：`metadata` 覆盖节字段、降级行为、幕后场景来源支持度、`image_strict_mode` 已知限制如实标注。

- [ ] **步骤 5：全量检查**

运行：`just check`（或手动四步）
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add docs scripts Dockerfile src pyproject.toml uv.lock web
git commit -m "docs: 同步 MDC P2 图片处理链知识库并验证打包"
```

---

## 自检记录

- **规格覆盖度**：§3.1（任务 4）、§3.2（任务 3/6/7）、§3.3（任务 6 默认态 / 任务 11 阈值模式）、§3.4（任务 9/10）、§3.5（任务 12）、§3.6（任务 5/8）、§3.7（任务 1/13）、§4（各任务测试行）、§5（任务内测试与规格测试表逐行对应）、§6 顺序（依赖已重排：enums/中立面 3 → 配置 4 → Right 接线 6 → Face 7 → acquire_best 9 → 分派 10 → 重选后置依赖满足）、demo 前置（任务 2，设计 §5 顺序）、§7（任务 13 文档）。
- **类型一致性**：`CropStrategy` **任务 3 定义于 enums.py**（中立层，R2 PLAN-01）、任务 4/6/8 引用；`CropPlan(requested, executed, ratio, low_res_height)` 任务 3 定义、任务 7/8/10/11 引用；`MetadataConfig` 任务 4 定义、任务 6/8/10/12 引用；`rebuild_poster_candidates` 任务 5 定义、任务 8 接线；`acquire_best`/`commit_temp` 任务 9 定义、任务 10 分派引用（顺序：定义先于引用）。
- **审查重点**：五条分别钉在任务 10（双入口一致）、9（共享缓存安全 + 并发冲突）、7（Face 降级可恢复 + 语义对齐设计）、11（低清可达）、8（用户产物三形态）。
- **比例**：计划约为规格 55% 篇幅；代码块只有测试骨架断言与规格定死的字面量，无函数体。
