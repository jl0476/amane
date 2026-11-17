# MDC P3 水印系统实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 实现水印系统 —— 逐类开关、逐类固定位置（AUTO/显式角位）、样式目录化、布局模式（stack/clockwise）、偏移/间距/起始可配、作用目标扩展 fanart、水印覆盖节激活、预览端点 —— 默认配置产物逐字节不变。

**架构：** 沿用覆盖层与生效配置传递接口（P2 已落地）：`WatermarkConfig` 扩展后挂覆盖白名单（watermark 节从预留变激活）；水印只发生在整理期 `_place_library_images`（库级生效配置），刮削期不加水印；水印不进 Resource 缓存；叠印顺序裁剪 → 超分 → 水印不变。布局与查找链收敛在 `media/images.py` / `media/watermarks/__init__.py`，行为函数保持纯输入参数签名（配置由调用方传入）。

**技术栈：** Python 3.14 / Pillow（既有）/ pytest；前端 `just generate` + i18n（新节翻译）。

**规格：** `docs/superpowers/specs/2026-11-17-mdc-p3-watermark-design.md`（第 2 版；计划的全部取值与协议来自它，执行者两份都读）

## 全局约束

- 禁内部导入（`TYPE_CHECKING` 除外）
- **默认产物逐字节不变**：golden 字节基准在任务 1 从基线 `c412b72` 采集（改动产物路径之前），固定输入集含默认 scale、`target_h < 40` 小图边界与四角组合；每个触碰水印产物的任务以「默认配置产物 == golden」为 GREEN 条件
- 覆盖 payload 禁 null；`extra="forbid"`（WatermarkConfig 现状缺失，任务 1 补齐）；`corners` frozen-keys 校验扩展到新值域
- 水印不进 Resource 缓存；刮削期 `materialize_images` 不加水印（现状不变）
- 叠印顺序裁剪 → 超分 → 水印不变；`overwrite_image=False` 跳过的产物不叠水印（现状语义）
- 语言表达遵守 `docs/dev/writing.md` / `terms.md`；提交说明 Conventional Commits + 中文
- 单测 `uv run pytest <路径> -x -q`；全量 `just check`（本机无 just 时手动四步）
- 每任务一提交；**每个提交结束必须可导入、可运行其声明的测试**

## 审查重点（Review Focus）

1. **默认逐字节**：全部新字段默认值下产物 == golden 基准（任务 1 在基线 `c412b72` 采集，含 target_h < 40 小图）→ 基准测试在任务 1，各任务持续对比。
2. **布局正确**：`clockwise` 轮转规则（相位顺序 × `start`）可复现、显式角位占位不轮转、四角全钉死时 AUTO 回退 → 表测试在任务 3。
3. **查找链**：四级链逐级命中、`style="default"` 旧位置兼容、`style≠default` 旧位置不兜底负向 → 表测试在任务 4。
4. **作用目标**：`targets` 组合 × 产物存在性、fanart 多张逐张叠印、`overwrite_image` 交互 → 集成测试在任务 5。
5. **覆盖节与库级生效**：`watermark` 节 PUT/GET/DELETE 往返、库级覆盖对整理期产物生效（端到端）→ 集成测试在任务 6。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/amane/enums.py` | `WatermarkCorner` 增加 `AUTO` 成员（值域扩展） |
| `src/amane/config/manager.py` | `WatermarkConfig` 扩展（kinds/layout/offset/gap/start/targets/style/custom_folder） |
| `src/amane/media/images.py` | 相位过滤、AUTO 解析、stack/clockwise 布局、offset/gap、targets 语义的行为函数 |
| `src/amane/media/watermarks/__init__.py` | 样式化查找链（custom_folder / style 目录 / 旧位置 / 内置） |
| `src/amane/handlers/file.py` | 整理期水印按生效配置分派（targets / fanart 叠印 / 库级配置传递） |
| `src/amane/config/overrides.py` | watermark 覆盖节激活 |
| `src/amane/api/models/libraries.py`、`src/amane/api/routes/libraries.py` | `EffectiveConfigResponse` 扩展 watermark 节 |
| `src/amane/api/routes/preview.py`（新建） | 水印预览端点 |
| `web/openapi.json`、`web/src/client/…`（`just generate` 产物）、`web/src/i18n/…` | API 联动 |
| `tests/media/test_watermark_*.py` 等 | 各任务测试 |
| `docs/dev/config.md`、`docs/dev/organize.md`、`docs/user/scraping.md`、`docs/user/organize.md`、`docs/dev/index.md` | 文档同步 |

---

### 任务 1：kinds 逐类开关、相位过滤与 golden 基准采集

**文件：**
- 修改：`src/amane/config/manager.py`（`WatermarkConfig.kinds`、`extra="forbid"` 补齐）、`src/amane/media/images.py`（相位过滤）
- 新建：`tests/media/fixtures/watermark_golden/`（基线 `c412b72` 采集的字节基准）、`tests/media/test_watermark_kinds.py`
- 测试：`tests/media/test_watermark_kinds.py`（新建）

- [ ] **步骤 1：采集 golden 基准（在改动产物路径之前）**

在基线 `c412b72` 的隔离检出（git worktree）用 `apply_cover_watermarks` 对固定输入集生成字节基准入库 `tests/media/fixtures/watermark_golden/`：默认 scale、`target_h < 40` 的小图边界（如 200×30）、四角组合、多相位组合。**参照必须产生于任何 P3 产物路径改动之前**（规格 §3.7），后续每任务以「默认配置产物 == golden」为 GREEN 条件。

- [ ] **步骤 2：编写失败的测试**

```python
def test_golden_baseline_unchanged(): ...       # 默认配置产物 == golden (含小图)
def test_kinds_default_all_true(): ...          # 默认 dict 全 true (frozen-keys 补齐)
def test_kinds_false_removes_stamp(): ...       # kinds=false 的相位不出现在产物
def test_kinds_multi_combination(): ...         # 多类组合表测试 (subtitle/uncensored/cracked/leaked/definition)
def test_kinds_invalid_value_rejected(): ...    # 非 bool 值 → ValidationError
def test_unknown_key_rejected(): ...            # 未知键 → ValidationError (extra="forbid" 本期补齐)
```

- [ ] **步骤 3：运行测试验证失败**

运行：`uv run pytest tests/media/test_watermark_kinds.py -x -q`
预期：FAIL

- [ ] **步骤 4：实现**

`WatermarkConfig` 加 `kinds: dict[WatermarkKind, bool]`（默认 `dict.fromkeys(WatermarkKind, True)`，frozen-keys 补齐校验沿 corners 先例）并补 `extra="forbid"`（现状缺失）；`apply_cover_watermarks` 在 `_stamp_stems` 推导后按 `kinds[_stamp_kind(stem)]` 过滤（None 配置 = 全 true 现状语义）。

- [ ] **步骤 5：运行测试验证通过**

运行：`uv run pytest tests/media/test_watermark_kinds.py -q && uv run pytest tests/media/ -q`
预期：PASS 且 golden 基准通过（默认 kinds 下产物与基线逐字节一致）

- [ ] **步骤 6：Commit**

```bash
git add src/amane/config/manager.py src/amane/media/images.py tests/media/test_watermark_kinds.py tests/media/fixtures/watermark_golden/
git commit -m "feat(media): 水印逐类开关、相位过滤与字节基准"
```

### 任务 2：corners 值域扩展（AUTO）与 stack 显式化

**文件：**
- 修改：`src/amane/enums.py`（`WatermarkCorner.AUTO`）、`src/amane/config/manager.py`（corners 值域）、`src/amane/media/images.py`（AUTO 解析）
- 测试：`tests/media/test_watermark_corners.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_corners_default_unchanged(): ...      # 默认全 TOP_LEFT (现状值, 逐字节基准)
def test_explicit_corner_pins_stamp(): ...     # 显式角位钉死 (现状语义)
def test_auto_resolves_stack_top_left(): ...   # stack 下 AUTO = TOP_LEFT
def test_mixed_auto_and_explicit(): ...        # 混合配置
def test_invalid_corner_value_rejected(): ...  # 非枚举/非 AUTO → ValidationError
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_watermark_corners.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`WatermarkCorner` 加 `AUTO = "auto"`；`WatermarkConfig.corners` 值域 `WatermarkCorner | Literal["auto"]`（校验归一化为枚举）；`_corner_for` 返回 AUTO 时由布局层解析（stack → TOP_LEFT，本任务先落 stack 语义）。**基准**：默认配置产物逐字节 == golden（任务 1 采集，含小图）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_watermark_corners.py -q && uv run pytest tests/media/ -q`
预期：PASS 且逐字节基准通过

- [ ] **步骤 5：Commit**

```bash
git add src/amane/enums.py src/amane/config/manager.py src/amane/media/images.py tests/media/test_watermark_corners.py
git commit -m "feat(media): 水印角位值域扩展 AUTO 与 stack 显式化"
```

### 任务 3：clockwise 布局与 start / offset / gap 可配

**文件：**
- 修改：`src/amane/config/manager.py`（layout/offset_x/offset_y/gap/start）、`src/amane/media/images.py`（布局）
- 测试：`tests/media/test_watermark_layout.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_clockwise_rotation_table(): ...       # 相位→角位轮转表测试 (相位顺序 × start)
def test_clockwise_explicit_occupies_slot(): ...  # 显式角位占位不轮转
def test_start_shifts_origin(): ...            # start 平移起始角
def test_offset_moves_anchor(): ...            # offset_x/offset_y 四角落点像素断言
def test_gap_override(): ...                   # gap 可配; None = 现状推导 (逐字节)
def test_gap_small_image_lower_bound(): ...    # target_h < 40 小图: gap=None 时下限 max(4,...) 生效 (golden 对比)
def test_stack_ignores_start(): ...            # stack 忽略 start
def test_clockwise_all_corners_pinned_falls_back(): ...  # 四角全钉死 + AUTO 相位 → 从 start 起始角向内叠

def test_invalid_layout_rejected(): ...        # 非法 layout/gap 负数/start 越界 → 422
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_watermark_layout.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`WatermarkConfig` 加 `layout`/`offset_x`/`offset_y`/`gap`/`start`（默认见规格 §3.1）；`_paste_stamps` 按 `layout` 分派：`stack`（现状路径 + offset/gap 参数化）、`clockwise`（相位顺序顺时针轮转 `TOP_LEFT→TOP_RIGHT→BOTTOM_RIGHT→BOTTOM_LEFT`，起始角 `start`，显式角位占位不轮转，同角第二枚起向内叠）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_watermark_layout.py -q && uv run pytest tests/media/ -q`
预期：PASS 且逐字节基准通过（gap=None / offset=0 与现状一致）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/config/manager.py src/amane/media/images.py tests/media/test_watermark_layout.py
git commit -m "feat(media): 水印 clockwise 布局与偏移间距起始可配"
```

### 任务 4：样式目录化与 custom_folder

**文件：**
- 修改：`src/amane/config/manager.py`（style/custom_folder）、`src/amane/media/watermarks/__init__.py`（查找链）、`src/amane/media/images.py`（style 传递）
- 测试：`tests/media/test_watermark_styles.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_lookup_chain_levels(): ...            # 四级链逐级命中表测试 (含 custom_folder+style 组合行, 逐级根路径唯一确定)
def test_default_style_uses_legacy_dir(): ...  # style=default 兼容旧位置 {data_dir}/watermarks/{stem}.png
def test_non_default_style_skips_legacy(): ... # style≠default 旧位置不兜底 (负向: 章图仅旧位置 → 不命中)
def test_custom_folder_highest_priority(): ... # custom_folder 优先于一切 (完全替代: data_dir 系各级不参与)
def test_style_dir_missing_skips_level(): ...  # 样式目录不存在跳过
def test_custom_folder_missing_skips(): ...    # custom_folder 指向不存在目录 → 第 1 级跳过
def test_corrupt_user_file_falls_back(): ...   # 损坏回退下一级 (现状语义)
def test_invalid_style_rejected(): ...         # style 字符集拒绝 → 422
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_watermark_styles.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`WatermarkConfig` 加 `style`/`custom_folder`；`load_stamp` 改双根签名（data_dir 根 + custom_folder 透传），按规格 §3.2 查找链：custom_folder 非 None 时**完全替代**（data_dir 系各级不参与）；`apply_cover_watermarks` 增加 style 透传；`watermark_dir` 既有单参数链退役（行为函数改双来源）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/test_watermark_styles.py -q && uv run pytest tests/media/ -q`
预期：PASS 且既有水印测试零回归（旧位置兼容）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/config/manager.py src/amane/media/watermarks/__init__.py src/amane/media/images.py tests/media/test_watermark_styles.py
git commit -m "feat(media): 水印样式目录化与自定义目录查找链"
```

### 任务 5：targets 作用目标与库级生效传递

**文件：**
- 修改：`src/amane/config/manager.py`（targets）、`src/amane/handlers/file.py`（分派与 fanart 叠印、库级配置传递）
- 测试：`tests/handlers/test_file_ops.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_targets_default_thumb_poster(): ...   # 默认 {thumb, poster} (现状)
def test_targets_exclude_thumb(): ...          # 不含 thumb → thumb 不叠
def test_targets_fanart_stamps_all(): ...      # 含 fanart → extrafanart 每张叠印
def test_fanart_overwrite_false_skips(): ...   # overwrite_image=False 跳过的张不叠
def test_fanart_stamp_failure_isolated(): ...  # 单张失败不中断其余张
def test_watermark_config_dispatch_changes_product(): ...  # 直接构造不同 WatermarkConfig 传入 → 产物不同 (分派逻辑验证, 不经覆盖层)
```

**注意（WM-P1）**：库级覆盖端到端测试（PUT 覆盖 → 整理 → 产物变化）**不在本任务** —— 覆盖白名单/`_SECTION_BINDINGS` 注册在任务 6，未注册时 `ConfigResolver.resolve` 对 watermark 节返回全默认（`enabled=False`），本任务提交点的库级覆盖测试必然红灯。本任务的分派逻辑验证用**直接构造的 WatermarkConfig** 传入；端到端库级覆盖测试在任务 6 与覆盖节激活同点验收。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/handlers/test_file_ops.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`WatermarkConfig` 加 `targets`；`_place_library_images` 水印段按 `targets` 分派（thumb/poster 沿现状判定、fanart 新增分支：extrafanart 复制后逐张 `apply_cover_watermarks_from_info`，单张失败记 warning 不中断）；watermark 配置经 `EffectiveOrganizeConfig` 传递（库级生效）；水印行为函数改双来源后，调用方传入值为「生效配置解析出的 data_dir 根 + custom_folder」（任务 4 的签名变更在此接线）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/handlers/ -q`
预期：PASS（经真实整理路径，既有水印行为零回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/config/manager.py src/amane/handlers/file.py tests/handlers/test_file_ops.py
git commit -m "feat(handler): 水印作用目标与库级生效配置传递"
```

### 任务 6：watermark 覆盖节激活（含 API/schema/client 联动）

**文件：**
- 修改：`src/amane/config/overrides.py`（白名单）、`src/amane/config/__init__.py`（导出）、`src/amane/api/models/libraries.py`、`src/amane/api/routes/libraries.py`、`web/openapi.json` 与 `web/src/client/…`（`just generate` 产物）、`web/src/i18n/locales/{zh-CN,en}/settings.json`
- 测试：`tests/config/test_overrides.py`、`tests/api/test_library_overrides.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_watermark_config_defaults(): ...         # 默认值矩阵 (规格 §3.1 表)
def test_watermark_invalid_payload_rejected(): ...  # kinds/corners/targets/style/offset 越界 → 422
def test_watermark_unknown_key_rejected(client): ...  # 未知键 → 422 (extra=forbid)
def test_watermark_section_roundtrip(client): ...   # PUT → GET effective-config 含 watermark 节 → DELETE 回继承
def test_watermark_no_longer_reserved(client): ...  # watermark 不再 422; download/provider 仍 422
def test_watermark_library_override_end_to_end(client): ...  # 库级覆盖 PUT → 整理 → 产物变化 (两库不同配置; 自任务 5 移入, 与覆盖节激活同点验收)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/config/test_overrides.py tests/api/test_library_overrides.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`overrides.py` 白名单加入 watermark（预留拒绝列表移除）；`EffectiveConfigResponse` 与 `get_effective_config` 扩展 watermark 节；`config/__init__.py` 导出新模型。随后：

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
git commit -m "feat(config): 激活 watermark 覆盖节并联动 API schema 与 client"
```

### 任务 7：水印预览端点

**文件：**
- 新建：`src/amane/api/routes/preview.py`
- 修改：`src/amane/api/routes/__init__.py`（注册）、`src/amane/api/models/`（请求/响应模型）
- 测试：`tests/api/test_watermark_preview.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_preview_returns_jpeg(client): ...     # 200 image/jpeg, 字节魔数 \xff\xd8 断言, 内容随库级配置与请求相位变化
def test_preview_phase_input_changes_output(): ...  # 请求 phase 四元组变化 → 叠印相位变化 (kinds 过滤同样生效)
def test_preview_uses_library_effective(): ... # 库级覆盖影响预览输出
def test_preview_missing_source_404(client): ...  # 源缺失 → 404
def test_preview_failure_422(client): ...      # 叠印失败 → 422 带原因
def test_preview_does_not_touch_store(client): ...  # 不落库不写 Resource 临时即删
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/api/test_watermark_preview.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`POST /api/libraries/{library_id}/preview/watermark`，请求体 `{url: <内部资源 URL>, phase: {has_subtitle, uncensored, mosaic, definition}}`（**相位四元组必填显式传入**，与 FileInfo 同口径，端点不做资源反查）：库级生效 `WatermarkConfig` → `resolve` 取源 → 临时副本叠印（不落库不写 Resource，临时即删）→ 返回 `image/jpeg`（沿用 `_save_jpeg`，与真实落盘产物同编码）；失败 422 带原因。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/api/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/api tests/api
git commit -m "feat(api): 水印预览端点按库级生效配置试算"
```

### 任务 8：文档同步与验证

**文件：**
- 修改：`docs/dev/config.md`（watermark 节）、`docs/dev/organize.md`、`docs/user/scraping.md`、`docs/user/organize.md`、`docs/dev/index.md`
- 验证：全量四步

- [ ] **步骤 1：文档同步**

先精简再加：watermark 覆盖节字段与语义、quality 与水印的作用边界（quality 仅图片）、预览端点、`thumb_download_strategy` 命名边界如实标注。

- [ ] **步骤 2：打包冒烟豁免确认**

样式目录为运行时 `data_dir` 产物（用户创建），无需打包变更；冒烟确认内置 `watermarks/*.png` 包资产在 PyInstaller/Docker 产物中已收集（任务 13 of P2 既有装配，核对即可），结果记入报告。

- [ ] **步骤 3：全量检查**

运行：`just check`（或手动四步）
预期：PASS

- [ ] **步骤 4：Commit**

```bash
git add docs
git commit -m "docs: 同步 MDC P3 水印系统知识库"
```

---

## 自检记录

- **规格覆盖度**：§3.1（任务 1/2/3/4/5 配置面）、§3.2（任务 4）、§3.3（任务 2/3）、§3.4（任务 5）、§3.6（任务 7）、§3.7（任务 1 基准采集）、§4（各任务测试行）、§6 顺序、覆盖节激活与库级覆盖端到端（任务 6）、文档（任务 8）。
- **类型一致性**：`kinds`/`corners`/`layout`/`targets`/`style` 任务 1–5 定义、任务 6 覆盖节引用（定义先于引用）；`WatermarkCorner.AUTO` 任务 2 定义、任务 3 布局解析引用。
- **审查重点**：五条分别钉在任务 1（默认逐字节）、任务 3（clockwise 轮转）、任务 4（查找链）、任务 5（作用目标）、任务 6（覆盖节与库级生效）。
- **比例**：计划约为规格 60% 篇幅；测试骨架只含断言口径与规格定死的字面量，无函数体。
