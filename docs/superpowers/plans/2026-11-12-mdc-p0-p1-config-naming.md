# MDC 迁移 P0+P1（配置覆盖层与命名整理）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把 MDC 的逐条目配置覆盖、命名整理与元数据原文双轨以原生 Python 实现进 Amane，全程保持既有数据行为不变。

**架构：** 分五段递进：数据层先补原文双轨（爬虫模型 → 聚合 → DB → NFO 开关化，默认输出逐字节不变）；配置层引入稀疏覆盖表与四层合成（全局 → 库行名单内 → 覆盖表 → 任务显式字段）；模板引擎在自研语法上加 `:` filter 与 `{% if %}`，再叠 Jinja2 兼容后端；整理侧加 inplace、备份/提交点/清理三段覆写协议与 `organize_journal` 恢复；最后 API 与前端接线。

**技术栈：** Python 3.14 / FastAPI / SQLModel + Alembic (SQLite) / pytest；前端 React + Mantine（三处穷尽检查 + i18n）；新增运行时依赖 jinja2、google-re2（RE2 线性时间正则引擎，识别词匹配）。

**规格：** `docs/superpowers/specs/2026-11-12-mdc-p0-p1-config-naming-design.md`（第 7 版，已过六轮复审；计划的全部取值与协议论证来自它，执行者两份都读）

## 全局约束

- Python `requires-python >= 3.14`；禁内部导入（`TYPE_CHECKING` 除外）、禁 `hasattr/getattr` 绕类型、禁全局变量（用依赖注入）、repo update 方法禁 `setattr` 逐字段赋值（`upsert_metadata` 的既有 `setattr` 循环除外）。
- Alembic 迁移一律 `uv run alembic revision --autogenerate -m "..."` 生成，**绝对禁止手写 revision id**；SQLite 加列用 `op.batch_alter_table`；数据迁移参考既有先例（`op.execute` / `conn.execute` 逐行 UPDATE，含 `downgrade`）。
- `config_overrides.payload` 与识别词 pattern **禁止 JSON null**；「显式取空」写空对象 `{}`。
- 所有新配置模型 `model_config = {"extra": "forbid"}`。
- 语言表达（注释、提交说明）遵守 `docs/dev/writing.md` 与 `docs/dev/terms.md`：双音节动词、禁比喻、禁沿革叙事。
- 对外任务入口是根 Justfile：全量测试 `just test`，自动修复 `just fix`，全检查 `just check`；单测用 `uv run pytest <路径> -x -q`。
- 每任务一提交，提交说明用 Conventional Commits（`feat:` / `test:` / `refactor:` / `docs:`），中文描述。
- 前端禁 `as` 类型断言、绝对禁 `any`；`MetadataField` 新成员必须同步三处穷尽检查与 i18n，否则 `tsc -b` 失败。
- NFO 基准比对是同一平台逐字节；写入端不改变 `aiofiles` 现有 newline 行为。

## 审查重点（Review Focus）

1. **并发覆写同一目标**（两个任务同时覆写同一路径）→ 后者必须记 CONFLICT，不得双写或互相删备份；测试在任务 24。
2. **识别词灾难回溯**（`(a|aa)+$`、`(a+)+$` 均合法且短，长度上限与语法树检查都拦不住）→ 匹配引擎换 **RE2**（`google-re2` 主依赖）：线性时间从引擎层消除 ReDoS，不支持的构造（反向引用/环视）编译即拒；测试在任务 15。
3. **Alembic 数据迁移中断后重跑** → 迁移必须幂等（重复执行结果一致），全部写操作在单事务内由 Alembic 保证；测试在任务 12。
4. **覆盖 payload 混入未知键或预留 section**（`extra=forbid` 下保存不该静默成功）→ 422 且不落库；测试在任务 14。
5. **NFO 重写端点条目含 N 个文件、部分失败** → 每文件独立处理，失败清单随响应返回，成功的文件不回滚；测试在任务 29。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/amane/crawlers/sites/avsox.py` | 补 `MULTI_LANGUAGE` trait；简介按语言精确取 |
| `src/amane/crawlers/models.py` | `MediaMetadata` 加 `original_title` / `original_plot` / `field_languages` |
| `src/amane/enums.py` | `MetadataField` 加 `ORIGINAL_TITLE` / `ORIGINAL_PLOT`；`MoveMode.INPLACE` |
| `src/amane/aggregate/engine.py` | `SCALAR_FIELDS` 加两字段；`website` 派生；`_fill_scalar` 记语言 |
| `src/amane/aggregate/models.py` | `AggregatedMetadata` 加四属性 |
| `src/amane/db/models.py` | `Metadata` 四列；`Library.template_overrides`；`parse_rules`；`organize_journal`；`MediaFile.link_path` |
| `src/amane/db/repo_types.py` | `MetadataFields` 加四键 |
| `src/amane/db/repos/metadata.py` | `update_metadata` 显式四字段；`_normalize_text_fields` 归一 `original_plot` |
| `src/amane/api/models/metadata.py` | `MetadataResponse` 手加四行；`PartialMetadata` ignore `website` |
| `src/amane/api/models/media.py` | `MediaFileResponse` 加 `link_path` |
| `src/amane/config/manager.py` | `OrganizeCommonConfig` / `NamingConfig` / `NfoConfig` 挂进 `HotSettings` |
| `src/amane/config/overrides.py`（新建） | `{}` ↔ `None` 映射、payload 校验、`ConfigResolver` 四层合成 |
| `src/amane/handlers/models.py` | `OrganizePayload.explicit_fields` |
| `src/amane/handlers/file.py` | handler 接 resolver；覆写协议与索引接管；`link_path` 落库 |
| `src/amane/parsing/rules.py`（新建） | `ParseRuleSet` 合成排序 + `apply_rules` |
| `src/amane/parsing/file_info.py` | `parse_file_info(rules=...)` |
| `src/amane/media/nfo.py` | `NfoOptions` + 表驱动 `write_nfo` + 元素名清洗 |
| `src/amane/organize/template.py` | `:` filter 链、`{% if %}`、降级链、占位符白名单校验 |
| `src/amane/organize/backend.py`（新建） | `TemplateBackend` Protocol + `NativeTemplateBackend` |
| `src/amane/organize/template_jinja.py`（新建） | `JinjaTemplateBackend` + 兼容 filter + 变量映射 |
| `src/amane/organize/path_templates.py` | 双默认模板、inplace 分支、`resolve_sidecar_paths` |
| `src/amane/organize/overwrite.py`（新建） | 覆写协议（阶段 A/B/C、指纹、回滚/清理分离） |
| `src/amane/api/routes/libraries.py` | 覆盖 CRUD、effective-config、试算、schema `syntaxes` |
| `src/amane/api/routes/parse_rules.py`（新建） | 识别词 CRUD |
| `src/amane/api/routes/metadata.py` | `PUT /api/metadata/{id}/nfo` |
| `src/amane/db/migrations/versions/…` | 5 个 Alembic revision（原文字段 / template_overrides / config_overrides + parse_rules / organize_journal + link_path） |
| `web/src/lib/exhaustive-maps.ts` 等 | 原文字段前端三处穷尽 + i18n |
| `docs/dev/{config,data-model}.md`、`docs/dev/{organize,parsing}.md`（新建）、`docs/user/{organize,media-server}.md`、`docs/dev/index.md` | 文档同步 |

---

### 任务 1：avsox 多语言修复

**文件：**
- 修改：`src/amane/crawlers/sites/avsox.py`
- 测试：`tests/crawlers/test_avsox.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_profile_declares_multi_language():
    from amane.crawlers.site_roles import MULTI_LANGUAGE_SITES
    from amane.enums import SiteName
    assert SiteName.AVSOX in MULTI_LANGUAGE_SITES

def test_plot_prefers_language_track_over_cn_fallback(monkeypatch):
    # 构造 description=None, description_cn="中文", description_ja="日本語"
    # _scrape 按 language=JP 时 plot 应为 "日本語" 而非 "中文"
    ...
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/crawlers/test_avsox.py -x -q`
预期：FAIL（`AVSOX not in MULTI_LANGUAGE_SITES`）

- [ ] **步骤 3：实现**

`profile()` 补 `traits=frozenset({SourceTrait.MULTI_LANGUAGE})`；`_scrape` 的简介改为按请求语言精确取对应 `_ja`/`_cn` 轨（不再 `_first` 中文优先），标题链已正确的不动。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/crawlers/test_avsox.py tests/crawlers/test_enum_consistency.py -q`
预期：PASS（含 `test_multi_language_follows_film_trait`）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/crawlers/sites/avsox.py tests/crawlers/test_avsox.py
git commit -m "fix(crawler): avsox 声明多语言能力并按语言取简介"
```

### 任务 2：MediaMetadata 原文双轨字段

**文件：**
- 修改：`src/amane/crawlers/models.py`
- 测试：`tests/crawlers/test_models.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_media_metadata_original_fields_default_none():
    m = MediaMetadata(number="ABP-123")
    assert m.original_title is None and m.original_plot is None and m.field_languages is None

def test_stale_raw_snapshot_deserializes():
    # 存量快照没有新字段, 反序列化不得抛 ValidationError
    m = MediaMetadata.model_validate({"number": "ABP-123"})
    assert m.original_title is None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/crawlers/test_models.py -x -q`
预期：FAIL（字段不存在）

- [ ] **步骤 3：实现**

`MediaMetadata` 加三个带默认值字段（`str | None = None`、`dict[str, str] | None = None`）。缺默认值会让 `aggregate/engine.py::_fetch_one` 的 `MediaMetadata(**cached)` 抛 `ValidationError`（该处只 `except TypeError`）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/crawlers/test_models.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/crawlers/models.py tests/crawlers/test_models.py
git commit -m "feat(crawler): MediaMetadata 增加原文双轨与逐字段语言"
```

### 任务 3：聚合层接收原文字段

**文件：**
- 修改：`src/amane/enums.py`、`src/amane/aggregate/engine.py`、`src/amane/aggregate/models.py`
- 测试：`tests/aggregate/test_engine.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_metadata_field_has_original_members():
    assert MetadataField.ORIGINAL_TITLE == "original_title"
    assert MetadataField.ORIGINAL_PLOT == "original_plot"

def test_original_fields_aggregate_independently():
    # 标题来自日文源、剧情来自中文源: original_title="日本語", original_plot="中文简介"
    # field_languages == {"original_title": "ja", "original_plot": "zh_cn"}
    ...

def test_undeclared_language_is_skipped():
    # 源未声明 field_languages 的字段不参与原文聚合
    ...

def test_enum_consistency_still_passes():
    ...  # tests/crawlers/test_enum_consistency.py 既有断言不得失败
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/aggregate/test_engine.py -x -q`
预期：FAIL（枚举成员不存在）

- [ ] **步骤 3：实现**

`enums.py::MetadataField` 加 `ORIGINAL_TITLE = "original_title"`、`ORIGINAL_PLOT = "original_plot"`；`engine.py::SCALAR_FIELDS` 加两成员（自动进 `ALL_FIELDS` 与字段链，`compile_priority` 的 defaultdict 自动落到类型路由）；`_fill_scalar` 在记录 `field_sources` 的同时按 `data.field_languages[field]` 写 `result.field_languages[field]`，未声明则跳过原文字段；`aggregate/models.py::AggregatedMetadata` 加 `original_title` / `original_plot` / `website: str | None` / `field_languages: dict[str, str]`。`LANG_METADATA_FIELD_SET` 不加（原文字段不走多语言请求展开）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/aggregate/ tests/crawlers/test_enum_consistency.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/enums.py src/amane/aggregate/ tests/aggregate/
git commit -m "feat(aggregate): 原文双轨按字段独立聚合并记录逐字段语言"
```

### 任务 4：DB 四列与落库链路

**文件：**
- 修改：`src/amane/db/models.py`、`src/amane/db/repo_types.py`、`src/amane/db/repos/metadata.py`、`src/amane/api/models/metadata.py`
- 新建：`src/amane/db/migrations/versions/<autogen>_metadata_original_fields.py`
- 测试：`tests/db/test_metadata_repo.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_update_metadata_accepts_original_fields(session):
    ...  # update_metadata 显式写入 original_title/original_plot/website/field_languages 并读回

def test_original_plot_normalized_as_long_text(session):
    ...  # 传入含 HTML 的 original_plot, 落库为纯文本 (normalize_long_text)

def test_partial_metadata_rejects_website_patch():
    ...  # PartialMetadata 不含 website 字段 (ignore_fields)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/db/test_metadata_repo.py -x -q`
预期：FAIL（列不存在）

- [ ] **步骤 3：实现与迁移**

`Metadata` 加 `original_title` / `original_plot` / `website` / `field_languages: dict[str,str] = Field(default_factory=dict, sa_column=Column(JSON))`；`MetadataFields` TypedDict 加四键；`update_metadata` 显式加四段 `if "xxx" in updates`（项目禁 setattr）；`_normalize_text_fields` 把 `original_plot` 加入长文本归一；`MetadataResponse` 手加四行；`create_partial_model` 的 `ignore_fields` 加 `"website"`（派生列不接受 PATCH）与 `"field_languages"`。迁移生成：

```bash
uv run alembic revision --autogenerate -m "metadata original fields"
uv run alembic upgrade head
```

检查生成文件含 `op.batch_alter_table` 且四个可空列；`field_languages` 默认 `{}`。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/db/ tests/api/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/db src/amane/api/models/metadata.py src/amane/db/migrations/versions tests/db
git commit -m "feat(db): metadata 落原文双轨 website 与逐字段语言"
```

### 任务 5：各源填充原文字段

**文件：**
- 修改：`src/amane/crawlers/sites/{dmm,mgstage,official,wp_works,javlibrary,jav321,xcity,giga,prestige}.py`、`src/amane/crawlers/sites/avsox.py`、`src/amane/crawlers/r18dev/mapper.py`
- 测试：对应 `tests/crawlers/` 既有站点测试文件

- [ ] **步骤 1：编写失败的测试**（表驱动，每站一组）

```python
@pytest.mark.parametrize("crawler,html,expect", [...])
def test_crawler_emits_original_fields(crawler, html, expect):
    meta = ...  # 解析样例页
    assert meta.original_title == expect["original_title"]
    assert meta.original_plot == expect["original_plot"]
    assert meta.field_languages == {"original_title": "ja", "original_plot": "ja"}
```

取值规则（规格 3.3.4）：日文站组（dmm / mgstage / official / wp_works / javlibrary / jav321 / xcity / giga / prestige）`original_title = title`、`original_plot = plot`、`field_languages = {"original_title": "ja", "original_plot": "ja"}`；avsox `original_title = movie.title_ja`、`original_plot = movie.description_ja`；r18dev `original_title = v.title_ja`、`original_plot = v.comment_ja`。Javdb / Javbus / iqqtv 本阶段不改，不声明语言。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/crawlers/ -k "original" -q`
预期：FAIL

- [ ] **步骤 3：实现**

各 `_scrape` 返回处补三个字段；`field_languages` 只在同时给出原文时声明（宁缺勿猜）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/crawlers/ -q`
预期：PASS（全部既有站点测试不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/crawlers tests/crawlers
git commit -m "feat(crawler): 各源填充原标题原文与逐字段语言声明"
```

### 任务 6：website 派生、刮削落库接线与翻译层隔离

**文件：**
- 修改：`src/amane/aggregate/engine.py`、`src/amane/handlers/scrape.py`（`ScrapeHandler.handle` 的 `upsert_metadata` 调用）
- 测试：`tests/aggregate/test_engine.py`、`tests/handlers/test_scrape_db_persistence.py`（新建，集成）

- [ ] **步骤 1：编写失败的测试**

```python
def test_website_from_title_source():
    ...  # source_urls 与 field_sources[TITLE] 同 key 时 website == 该 URL

def test_website_none_when_title_source_has_no_url():
    ...  # r18dev 类 (source_url=None) 为标题来源时 website is None, 不沿链回退

def test_translate_keeps_original_fields():
    ...  # _translate_metadata 只改 title/plot; original_title/original_plot 保持

def test_scrape_persists_original_fields_to_db_and_api(tmp_path, client):
    # 集成: 执行一次刮削任务(样例来源返回含原文的 MediaMetadata)后:
    # DB 行的 original_title/original_plot/website/field_languages 均落库;
    # GET /api/metadata/{id} 四字段可读回
    ...
```

`upsert_metadata` 按 `MetadataFields` 键透传，但 `ScrapeHandler.handle` 是**显式逐参数传名**
（`handlers/scrape.py:177` 起），新字段不会自动进入，必须手加：
`original_title=result.metadata.original_title`、`original_plot=result.metadata.original_plot`、
`website=result.metadata.website`、`field_languages=result.metadata.field_languages`。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/aggregate/test_engine.py tests/handlers/test_scrape_db_persistence.py -x -q`
预期：FAIL（website 恒为 None；落库四字段为空）

- [ ] **步骤 3：实现**

`_assemble_aggregate_fields` 末尾：`result.website = result.source_urls.get(result.field_sources.get(MetadataField.TITLE, "")) or None`——单值取法，不沿链回退（key 空间同为 cache_key）。`_translate_metadata` 无需改动（现状只碰 title/plot），测试锁死该行为。`ScrapeHandler.handle` 补四个实参。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/aggregate/ tests/handlers/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/aggregate src/amane/handlers/scrape.py tests/aggregate tests/handlers
git commit -m "feat(scrape): website 派生与原文四字段落库"
```

### 任务 7：NFO 基准用例（安全网）

**文件：**
- 测试：`tests/media/test_nfo_baseline.py`（新建）
- 参照：`src/amane/media/nfo.py` 现有输出

- [ ] **步骤 1：用当前实现生成基准串**

临时脚本调 `write_nfo` 对一份含全部字段的 `Metadata`（number/title/plot/release/runtime/score/actors/directors/series/studio/publisher/tags/poster_url/thumb_url/trailer_url/external_ids 均非空）输出，保存为测试内的常量字符串 `GOLDEN`（含声明行与两空格缩进）。

- [ ] **步骤 2：编写基准测试**

```python
def test_default_nfo_matches_golden(tmp_path):
    ...  # write_nfo(metadata, path) 后读取全文 == GOLDEN (同平台逐字节)
```

- [ ] **步骤 3：运行测试验证通过（当前实现）**

运行：`uv run pytest tests/media/test_nfo_baseline.py -q`
预期：PASS —— 此任务是给任务 8/9 的改造立安全网，先绿后改。

- [ ] **步骤 4：Commit**

```bash
git add tests/media/test_nfo_baseline.py
git commit -m "test(media): NFO 现状输出基准字符串用例"
```

### 任务 8：NfoOptions 模型与 write_nfo 开关化

**文件：**
- 修改：`src/amane/media/nfo.py`、`src/amane/handlers/file.py`（调用点传 options）
- 测试：`tests/media/test_nfo_write.py`、`tests/media/test_nfo_baseline.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_every_switch_off_removes_element():
    ...  # 表驱动: 每个开关 False 时对应元素从输出消失; True 时出现

def test_default_options_match_golden():
    ...  # NfoOptions() 默认值下输出 == 任务 7 的 GOLDEN
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_nfo_write.py -x -q`
预期：FAIL（NfoOptions 不存在）

- [ ] **步骤 3：实现**

`NfoOptions`（`extra="forbid"`）字段与默认值**逐字照抄规格 3.4.3 的模型清单**：`title/originaltitle/sorttitle/plot/outline/outline_summary(False)/originalplot(False)/actor/director/premiered/releasedate/year/num/runtime/votes(False)/score/criticrating/country(False)/mpaa/mpaa_value="JP-18+"/customrating(False)/series/set/tag/genre/studio/maker/publisher/label/poster/cover/trailer/website(False)/external_ids/add_part_to_title(False)/add_translated_by(False)/tagline_format(None)/set_by_actress(False)/set_by_serial_name(False)/set_by_series(False)/additional_tags([])`，另有 `originaltitle_source: Literal["title","original"] = "title"`。`write_nfo(metadata, nfo_path, options: NfoOptions | None = None)`（None = 默认）；输出元素顺序与现实现完全一致（表驱动：开关关→元素整段不出现）。`handlers/file.py::execute_file_operations` 的调用点改传解析后的 `NfoOptions`（本任务先传默认；任务 13 接 resolver）。默认值下 `test_nfo_baseline` 必须仍 PASS。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/ -q`
预期：PASS（含基准）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/nfo.py src/amane/handlers/file.py tests/media/
git commit -m "feat(media): write_nfo 按 NfoOptions 开关化且默认输出不变"
```

### 任务 9：NFO 原文输出、元素名清洗与摘要

**文件：**
- 修改：`src/amane/media/nfo.py`
- 修改：`docs/user/media-server.md`（NFO 字段开关与 plot/outline 语义）
- 测试：`tests/media/test_nfo_write.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_originaltitle_source_original():
    ...  # originaltitle_source="original" 且 original_title 非空 → <originaltitle> 为原文; 为空 → 回退 <title> 值

def test_originalplot_and_website_opt_in():
    ...  # originalplot=True 且有值才写 <originalplot>; website=True 且有值才写 <website>

def test_external_id_element_name_sanitized():
    ...  # external_ids={"r18dev:jp": "x"} → <r18dev_jp_id>x</r18dev_jp_id>, ElementTree 可解析

def test_outline_summary():
    ...  # outline_summary=True: 取第一句; 无分隔符取前 120 字节不断开多字节; 空则不写
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/media/test_nfo_write.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`<{site}id>` 元素名把非 XML 名称字符（`:` 等）替换为 `_`；`<outline>` 在 `outline_summary=True` 时按规格摘要规则；`docs/user/media-server.md` 的「NFO 内容固定」段落改写为字段开关说明。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/media/ -q`
预期：PASS（基准仍绿——默认值不动输出）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/media/nfo.py tests/media/ docs/user/media-server.md
git commit -m "feat(media): NFO 原文轨道 元素名清洗与摘要开关"
```

### 任务 10：三个配置 section 模型

**文件：**
- 修改：`src/amane/config/manager.py`
- 测试：`tests/config/test_config_manager.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_hot_settings_has_new_sections():
    ...  # hot.common / hot.naming / hot.nfo 存在且默认值正确
    # common.overwrite_video=False, overwrite_image=True
    # naming.video_template == VIDEO_TEMPLATE_DEFAULT, write_nfo=True, copy_resources 全集
    # nfo 全默认 (规格 3.4.3)

def test_unknown_section_key_rejected():
    ...  # extra=forbid: 塞未知键抛 ValidationError
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/config/test_config_manager.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`OrganizeCommonConfig` / `NamingConfig` / `NfoConfig` 逐字照抄规格 3.1.2 的字段清单与默认值（`NamingConfig` 15 个模板类字段 + `syntax`；`NfoConfig` 即 `NfoOptions` 的挂载，不含 `enabled`），挂进 `HotSettings`。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/config/ -q`
预期：PASS（`test_unknown_section_ignored` 等既有用例不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/config/manager.py tests/config/
git commit -m "feat(config): HotSettings 增加整理 命名 NFO 三节"
```

### 任务 11：ConfigResolver 与 config_overrides 表

**文件：**
- 新建：`src/amane/config/overrides.py`
- 修改：`src/amane/db/models.py`（`config_overrides` 表）
- 新建迁移：`uv run alembic revision --autogenerate -m "config overrides table"`
- 测试：`tests/config/test_overrides.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_payload_rejects_null_and_unknown_keys():
    ...  # null 值 422 语义 (ValidationError); 预留 section (download 等) 拒绝

def test_empty_dict_maps_to_none():
    ...  # naming payload {"link_template": {}} → 生效模型 link_template is None
    # {"thumb_template": {}} → None (采用内建默认); 键缺失 → 继承

def test_four_layer_synthesis():
    ...  # 表驱动: 全局 < 库行(名单内) < 覆盖表 < 任务; dict 深合并, list 整体替换
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/config/test_overrides.py -x -q`
预期：FAIL（模块不存在）

- [ ] **步骤 3：实现**

`config_overrides` 表按规格 3.1.1（UNIQUE(scope_type, scope_id, section)）；`ConfigResolver.resolve[S: BaseModel](section, *, scope=None, library=None, payload=None) -> S` 按四层合成，第 3 层键值先过 `{}`→`None` 映射与 null 拒绝。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/config/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/config src/amane/db src/amane/db/migrations/versions tests/config
git commit -m "feat(config): 覆盖表与四层合成解析器"
```

### 任务 12：Library.template_overrides 与历史库迁移

**文件：**
- 修改：`src/amane/db/models.py`、`src/amane/db/repos/libraries.py`、`src/amane/api/routes/libraries.py`
- 新建迁移（含数据迁移：15 个模板字段全量写名单）
- 测试：`tests/db/test_template_overrides_migration.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_migration_pins_all_template_fields():
    ...  # 既有库迁移后 template_overrides 含全部 15 个名字

def test_migrated_library_ignores_global_change():
    ...  # thumb_template=None 的库, 改全局 naming.thumb_template 后 resolve_paths 路径不变

def test_override_beats_pinned_library_row():
    ...  # 迁移后写 naming 覆盖 → 覆盖生效; 不写 → 落盘路径不变

def test_create_request_explicit_field_pins():
    ...  # POST /api/libraries 带 video_template → 名单含它; 不带 → 名单为空
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/db/test_template_overrides_migration.py -x -q`
预期：FAIL（列不存在）

- [ ] **步骤 3：实现与迁移**

`Library.template_overrides: list[str] = Field(default_factory=list, sa_column=Column(JSON))`；数据迁移把每个既有库的 15 个模板类字段名写入该列（`video_template` / `link_template` / `link_mode` / `strm_content_template` / `thumb_template` / `poster_template` / `fanart_template` / `extrafanart_template` / `extrafanart_name_template` / `nfo_template` / `trailer_template` / `subtitle_template` / `subtitle_extensions` / `write_nfo` / `copy_resources`）；迁移幂等（重跑结果一致）。建库/改库路径用请求模型的 `model_fields_set`（FastAPI 直接构造、无 JSON 往返，可靠）把显式给出的模板字段名加入名单。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/db/ tests/api/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/db src/amane/api/routes/libraries.py src/amane/db/repos/libraries.py tests/db
git commit -m "feat(db): 模板显式名单与历史库全量迁移"
```

### 任务 13：任务层 explicit_fields、handler 接入与生效配置传递

**文件：**
- 修改：`src/amane/handlers/models.py`、`src/amane/api/models/tasks.py`、`src/amane/handlers/file.py`（`OrganizeHandler` / `apply_file_operations` / `execute_file_operations`）、`src/amane/organize/path_templates.py`（`resolve_paths` / `resolve_sidecar_paths` / `resolve_subtitle_path` 加 `naming` 参数）、`src/amane/organize/subtitles.py`（`place_subtitles` 透传）
- 测试：`tests/handlers/test_file_ops.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_explicit_fields_survive_json_roundtrip():
    ...  # OrganizePayload(write_nfo=False).model_dump(mode="json") → validate 后 explicit_fields == ["write_nfo"]

def test_apply_library_values_not_explicit():
    ...  # _apply_library 回填的库值不进 explicit_fields

def test_null_payload_field_not_explicit():
    ...  # 请求 write_nfo=None → 不进显式集; copy_resources=[] → 进显式集

def test_legacy_payload_without_key_uses_non_null_fields():
    ...  # raw 缺 explicit_fields 键 → 显式集 = 非 None 的两字段 (旧行为)

def test_manual_and_auto_same_effective_config():
    ...  # 手动提交与 _auto_organize 后继任务对同一库产出相同生效 naming

def test_override_naming_drives_real_dest_path(tmp_path):
    # 集成: 为库写 naming 覆盖 (video_template 换一个目录形态) → OrganizeHandler 执行整理
    # 断言磁盘上视频与 NFO 的实际路径按覆盖后的模板生成 (非库行模板)
    ...

def test_override_subtitle_template_drives_real_subtitle_path(tmp_path):
    # 集成: 覆盖 subtitle_template → 整理带外挂字幕的文件
    # 断言磁盘上字幕实际落盘路径按覆盖模板生成 (place_subtitles → resolve_subtitle_path(naming=…))
    ...

def test_resolve_paths_prefers_naming_over_library_row(tmp_path):
    ...  # resolve_paths(naming=NamingConfig(video_template=X)) 的 video == X 渲染结果,
    # 库行 video_template 被忽略; naming=None 时维持现状 (既有用例不回归)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/handlers/test_file_ops.py -x -q`
预期：FAIL（字段不存在；`resolve_paths` 无 `naming` 参数）

- [ ] **步骤 3：实现**

`OrganizePayload.explicit_fields: list[str] = Field(default_factory=list)`；`OrganizeSubmission.resolve` 在 `_apply_library` **之前**记录 `model_fields_set` 与两字段交集并排除 None 值；`parse_payload` 对缺键的旧 payload 按「非 None 即显式」补写；`OrganizeHandler` 用 `ConfigResolver` 解析一次 `common` / `naming` / `nfo`（第 4 层只读 `explicit_fields`），把 `NamingConfig` 作为参数传入 `apply_file_operations` → `resolve_paths` / `resolve_sidecar_paths`，把 `NfoOptions` 与 `overwrite_video` / `overwrite_image` / `copy_resources` 同路传入 `execute_file_operations`。**下层不回读库行覆盖已解析结果**（规格 3.1.3「生效配置的传递接口」）；`resolve_paths(naming=None)` 保持现状取库行。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/handlers/ tests/api/ tests/organize/ -q`
预期：PASS（`resolve_paths` 既有用例零改动通过 = naming=None 等价）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/handlers src/amane/api/models/tasks.py src/amane/organize/path_templates.py tests/handlers
git commit -m "feat(handler): 生效配置传递至落盘与任务显式字段"
```

### 任务 14：覆盖 CRUD API

**文件：**
- 修改：`src/amane/api/routes/libraries.py`
- 修改：`docs/dev/config.md`（四层合成、稀疏含义、显式标记）
- 测试：`tests/api/test_library_overrides.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_overrides_crud_roundtrip(client):
    ...  # PUT /api/libraries/{id}/overrides/naming → GET 回读; DELETE → 回到继承

def test_put_rejects_reserved_section_and_null():
    ...  # section=download → 422; payload 含 null → 422 且不落库

def test_effective_config_returns_synthesis(client):
    ...  # GET /api/libraries/{id}/effective-config == resolver 结果
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/api/test_library_overrides.py -x -q`
预期：FAIL（端点不存在）

- [ ] **步骤 3：实现**

按规格 3.1.4 四个端点；section 白名单 `common/naming/nfo`，`scope_type` 只收 `library`，其余 422。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/api/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/api/routes/libraries.py tests/api docs/dev/config.md
git commit -m "feat(api): 库级配置覆盖 CRUD 与生效配置端点"
```

### 任务 15：parse_rules 表与 ParseRuleSet

**文件：**
- 修改：`pyproject.toml`（`google-re2` 进 `[project].dependencies`）、`src/amane/db/models.py`、`src/amane/parsing/file_info.py`
- 新建：`src/amane/parsing/rules.py`
- 新建迁移
- 修改：`docs/dev/parsing.md`（新建：作用域、分桶合成、扁平匹配、RE2 引擎）
- 测试：`tests/parsing/test_rules.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_rule_set_flat_order_and_first_hit():
    ...  # 番号段全部先于路径段; 段内 created_at 升序 (同刻按 id); 首条命中即生效

def test_same_pattern_two_types_earlier_wins():
    ...  # 同 pattern 分属 chinese/western → created_at 早者生效

def test_library_scope_replaces_global_per_bucket():
    ...  # 库级 (kind, content_type) 桶存在 → 整体替换全局同桶; 其余桶用全局

def test_apply_rules_needs_path_for_path_rules():
    ...  # apply_rules(path=None, ...) 跳过路径规则; path 大小写不敏感 casefold

def test_parse_file_info_accepts_rules():
    ...  # parse_file_info(path, rules=set) 生效; text= 分支跳过路径规则

def test_invalid_regex_rejected_and_length_capped():
    ...  # RE2 编译失败 → 422 语义; pattern 超 256 字符 → 拒绝 (防滥用)

def test_re2_rejects_unsupported_constructs():
    ...  # 反向引用 (a)\1 / 环视 (?<=x)y → RE2 编译即拒 → 422

def test_catastrophic_pattern_is_linear():
    ...  # (a|aa)+$ 编译通过 (RE2 支持) + 'a'*1000 + 'b' 匹配瞬时完成
    # (线性时间保证, 非 Python re 的指数回溯)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/parsing/test_rules.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现与迁移**

`parse_rules` 表按规格 3.1.6（`scope_id` 用 0 表全局）；`ParseRuleSet` 构造时完成桶合成与扁平排序，`apply_rules(path, info, rules)` 只做匹配；`parse_file_info(..., rules=None)` 在 `path` 分支返回前调用。**匹配引擎用 RE2**（`google-re2` 加入 `[project].dependencies`，`uv sync`；规格 3.1.6）：pattern 保存与 `ParseRuleSet` 构造都用 `re2.compile` 校验——RE2 线性时间从引擎层消除灾难回溯，反向引用 / 环视编译即拒（422 提示不支持的构造），Python `re` 不再参与识别词匹配。`google-re2` 为二进制依赖，Windows/macOS/Linux 主流平台 PyPI 有 wheel（已实测 3.14/Windows），打包验证折进任务 32。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/parsing/ -q`
预期：PASS（`parse_file_info` 既有用例不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/parsing src/amane/db tests/parsing docs/dev/parsing.md src/amane/db/migrations/versions
git commit -m "feat(parsing): 识别词表 作用域合成与扁平匹配"
```

### 任务 16：识别词管理 API 与生产入口注入

**文件：**
- 新建：`src/amane/api/routes/parse_rules.py`
- 修改：`src/amane/app/runtime.py`（`ParseRuleSet` 提供者）、`src/amane/handlers/refresh.py`、`src/amane/handlers/file.py`、`src/amane/api/models/tasks.py`、`src/amane/library/cleanup/footprint.py`、`src/amane/scheduler/service.py`
- 测试：`tests/api/test_parse_rules_api.py`（新建）、`tests/handlers/test_refresh.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_parse_rules_crud(client):
    ...  # POST/GET/PUT/DELETE /api/parse-rules;RE2 编译失败/反向引用/环视 422;超长 422;重复键 409/422

def test_two_libraries_different_rules_via_real_scan(tmp_path, client):
    # 集成: 库 A 配番号关键词→chinese, 库 B 配同番号→western
    # 对两个库各放同一文件名, 走真实 RefreshHandler 扫描
    # 断言两侧解析结果的 FileInfo.content_type 不同,
    # 且 fan-out 的 SCRAPE 任务载荷 content_type 各随其库
    # 注意: MediaFile 没有 content_type 列; 类型在全局唯一的 Metadata 上、首建时定
    # 另断言: 两库指向同一番号时既有 Metadata.content_type 不被库规则改写 (upsert 契约)
    ...

def test_scheduler_and_cleanup_use_rules():
    ...  # scheduler 发现解析与 cleanup 清单解析注入对应库 ParseRuleSet
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/api/test_parse_rules_api.py tests/handlers/test_refresh.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

CRUD 后失效 `ParseRuleSet` 缓存（版本键 `(count, max(updated_at))`）；`Runtime` 提供 `parse_rules_for(library_id) -> ParseRuleSet`（含全局合成）；五个生产入口注入 `rules=`（扫描 `handlers/refresh.py:137`、整理落盘 `handlers/file.py:221`、任务请求解析 `api/models/tasks.py:205`、清理清单 `library/cleanup/footprint.py:201`、调度 `scheduler/service.py:477`）。字幕配对（`organize/subtitles.py`）与 Alembic 重投影不注入（规格 3.1.6 表）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/api/ tests/handlers/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/api/routes/parse_rules.py src/amane/app/runtime.py src/amane/handlers src/amane/api/models/tasks.py src/amane/library src/amane/scheduler tests
git commit -m "feat(api): 识别词管理端点与生产入口注入"
```

### 任务 17：自研 filter 语法与降级链

**文件：**
- 修改：`src/amane/organize/template.py`
- 测试：`tests/organize/test_template_syntax.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_filter_chain_with_colon():
    ...  # {title:upper} / {title:upper:truncate(20)}; truncate 按 UTF-8 字节无省略号

def test_default_chain_nested():
    ...  # {a:default(b:default("未知"))}; "Unknown"/空串/空列表均视为无值

def test_value_mapping_unaffected():
    ...  # {title|truncate=10} 仍是值映射 (渲染行为与今天一致); {name|Unknown=未分类} 不变

def test_quoted_args_allow_comma():
    ...  # {t:replace("a,b","c")}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_template_syntax.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`_parse_placeholder` 先按首个 `|` 切出映射，左半再按首个 `:` 切出 filter 链（grammar 见规格 3.2.2）；7 个 filter（default/upper/lower/truncate/split/first/last/replace/trim 中规格所列集合）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ -q`
预期：PASS（既有路径模板用例不回归）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/organize/template.py tests/organize/test_template_syntax.py
git commit -m "feat(organize): 自研模板 filter 链与降级链"
```

### 任务 18：条件块

**文件：**
- 修改：`src/amane/organize/template.py`
- 测试：`tests/organize/test_template_syntax.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_if_block_truthiness():
    ...  # {% if a %}…{% endif %}: a 有值才渲染; 多变量 {% if a,b %} 全有值才真; {% else %} 分支
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_template_syntax.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

条件只允许变量名或逗号分隔变量名列表（无 elif、无运算符）；「有值」判定与 filter 一致。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/organize/template.py tests/organize/test_template_syntax.py
git commit -m "feat(organize): 模板条件块"
```

### 任务 19：占位符白名单校验

**文件：**
- 修改：`src/amane/organize/template.py`（`validate`）
- 测试：`tests/organize/test_template_syntax.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_validate_rejects_unknown_placeholder_and_filter():
    ...  # {typo} / {title:unknownf} → 校验错误并指出位置

def test_validate_accepts_dir_alias():
    ...  # {dir} 合法 (运行时别名)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_template_syntax.py -x -q`
预期：FAIL（当前 validate 不查名字）

- [ ] **步骤 3：实现**

白名单 `PLACEHOLDERS ∪ {"dir"}`；只作用于保存/切换路径，已落盘渲染行为不变（未知名仍渲染 `Unknown`）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/organize/template.py tests/organize/test_template_syntax.py
git commit -m "feat(organize): 模板保存校验占位符与 filter 名"
```

### 任务 20：Jinja2 依赖与 JinjaTemplateBackend

**文件：**
- 修改：`pyproject.toml`（`jinja2` 进 `[project].dependencies`）
- 新建：`src/amane/organize/backend.py`（`TemplateBackend` Protocol + `NativeTemplateBackend`）、`src/amane/organize/template_jinja.py`
- 测试：`tests/organize/test_template_jinja.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_jinja_backend_renders_and_rejects_attr():
    ...  # {{ studio }} 正常; SandboxedEnvironment 拒绝下划线属性 (SecurityError)

def test_compat_filters_match_native_semantics():
    ...  # default 对空串/空列表/"Unknown" 生效 (非原生语义); truncate 按 UTF-8 字节无省略号

def test_variable_mapping():
    ...  # cd?→cd, sub?→sub, mosaic?→mosaic, def?→definition; 其余同名含 dir
    # {{ cd? }} 在 Jinja 下解析期报错 (非法标识符)

def test_two_engines_agree_on_shared_semantics():
    ...  # 分集/中字四种组合下两引擎渲染路径完全相同 (仅排除变量缺失差异)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_template_jinja.py -x -q`
预期：FAIL（模块不存在）

- [ ] **步骤 3：实现**

`uv sync` 装 jinja2；`JinjaTemplateBackend` 用 `SandboxedEnvironment` 注册自建 `default`/`truncate`/`split`（不用原生 default/truncate），渲染前按映射表改写变量键，输出仍过 `PathEngine.clean` 与 `resolve` 逃逸防护。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add pyproject.toml uv.lock src/amane/organize tests/organize
git commit -m "feat(organize): Jinja2 后端与兼容 filter"
```

### 任务 21：双默认模板、语法切换验证与运行时分派

**文件：**
- 修改：`src/amane/organize/path_templates.py`（双默认 + `resolve_paths` / `resolve_sidecar_paths` / `resolve_subtitle_path` / `render_path_template` / `render_extrafanart_names` 分派）、`src/amane/organize/strm_content.py`（`render_strm_content` 分派）、`src/amane/api/routes/libraries.py`、`src/amane/api/models/libraries.py`
- 测试：`tests/organize/test_path_templates.py`、`tests/api/test_library_overrides.py`、`tests/handlers/test_file_ops.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_switch_rejected_on_placeholder_residual():
    ...  # 自研模板含 {number} 切 jinja → 422 + 指明模板与残留; 反向同理 (含 {{ }} 残留)

def test_switch_validates_inherited_and_fallback_templates():
    ...  # 名单内库级模板、覆盖表模板、全局默认模板全部参与验证

def test_schema_endpoint_lists_syntaxes():
    ...  # GET /api/libraries/path-template-schema 含 syntaxes: 两套默认模板与占位符

def test_jinja_syntax_drives_real_organize(tmp_path):
    # 集成: naming.syntax=jinja + Jinja 视频模板 → OrganizeHandler 整理
    # 断言磁盘实际产物路径按 Jinja 渲染 (视频/附属/字幕/STRM 正文/剧照文件名)
    # 任一入口漏接, 该入口产物路径会出现字面量 "{{"
    ...
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_path_templates.py tests/api/test_library_overrides.py tests/handlers/test_file_ops.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

Jinja2 默认视频模板字面量照抄规格 3.2.3（`{{ studio }}/{{ number }}/{{ number }}{% if cd %}-CD{{ cd }}{% endif %}{% if sub %}-{{ sub }}{% endif %}.{{ ext }}`）；切换时对生效链全部模板做渲染探针（样例变量覆盖全部 30 占位符 + `dir`，残留 `{ident}` / `{{` / `{%` 判失败）；`syntaxes` 下发两套默认与各自占位符名（含 Jinja 变量映射）。**运行时分派**：统一分派函数按生效 `NamingConfig.syntax` 选 backend，接入规格 3.2.1 的**六个**真实入口（`resolve_paths` / `resolve_sidecar_paths` / `resolve_subtitle_path` / `render_strm_content` / `render_extrafanart_names` / `render_path_template`）——字幕配对（识别字幕归属哪个视频，`parse_file_info(text=…)`）不渲染模板、不在分派范围；字幕**落盘路径**（`place_subtitles → resolve_subtitle_path`）渲染 `subtitle_template`，必须分派。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ tests/api/ tests/handlers/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/organize src/amane/api tests
git commit -m "feat(organize): 语法切换验证与运行时分派"
```

### 任务 22：命名试算端点

**文件：**
- 修改：`src/amane/api/routes/libraries.py`
- 测试：`tests/api/test_naming_preview.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_preview_renders_all_products(client):
    ...  # POST /api/libraries/naming/preview → video/thumb/poster/fanart/nfo/trailer/subtitle 路径
    # 与 resolve_paths 对同一输入的输出一致 (同一代码路径)

def test_preview_supports_media_file_id_and_global_default(client):
    ...  # {"media_file_id": N} 取真实文件与元数据; 无 library_id 用全局默认
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/api/test_naming_preview.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

走 `resolve_paths` + `TemplateContext`，纯计算不落盘。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/api/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/api tests/api
git commit -m "feat(api): 命名模板试算端点"
```

### 任务 23：MoveMode.INPLACE

**文件：**
- 修改：`src/amane/enums.py`、`src/amane/organize/file.py`、`src/amane/organize/path_templates.py`
- 测试：`tests/organize/test_file_org.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_inplace_keeps_video_and_writes_sidecars_in_source_dir(tmp_path):
    ...  # 视频不动 (mtime/ inode 不变); 图片与 NFO 落在源目录; video_template 被忽略

def test_inplace_is_not_silent_noop(monkeypatch):
    ...  # mock move/copy 断言未被调用, 但 outcome == PLACED
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_file_org.py -x -q`
预期：FAIL（INPLACE 不存在）

- [ ] **步骤 3：实现**

`MoveMode.INPLACE = "inplace"`；`execute_organize` 的 `match mode:` **必须加分支**（无兜底分支，漏加会静默落空）；`resolve_paths` 加 inplace 入口：`video = source_path`，跳过「视频模板解析为库根目录」守卫。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ tests/handlers/ -q`
预期：PASS（`collect_prune` 条件 `is MOVE` 天然排除 inplace）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/enums.py src/amane/organize tests/organize
git commit -m "feat(organize): 原地整理模式"
```

### 任务 24：organize_journal 与覆写协议

**文件：**
- 修改：`src/amane/db/models.py`（`organize_journal` 表）、`src/amane/handlers/file.py`（`execute_file_operations` 的 `OCCUPIED` 分派与 `overwrite_image` 接入图片写入）
- 新建：`src/amane/organize/overwrite.py`
- 新建迁移
- 修改：`docs/dev/organize.md`（新建：覆写协议、提交点、journal、指纹）
- 测试：`tests/organize/test_overwrite.py`（新建）、`tests/handlers/test_file_ops.py`（扩展）

- [ ] **步骤 1：编写失败的测试**

```python
def test_overwrite_move_backup_replace_and_source_delete(tmp_path):
    ...  # 阶段 A 备份 → C 目标核验 → 接管 → 删备份 → 源核验后 (move) 删源 → journal 删除

def test_pre_commit_failure_restores_backup(tmp_path):
    ...  # copy 中途磁盘满 (模拟写临时文件抛错) → 备份还原, 目标保持旧内容

def test_post_commit_journal_failure_keeps_target(tmp_path, monkeypatch):
    ...  # os.replace 成功、journal 推进抛错 → 目标保留新内容, journal 仍 prepared

def test_cross_filesystem_move_uses_tmp_then_replace(tmp_path, monkeypatch):
    ...  # os.replace(源,目标) 抛 EXDEV → 复制到目标目录临时名 + replace; 接管前源不删除

def test_concurrent_overwrite_of_same_target_conflicts(tmp_path):
    ...  # 第二个任务对同一目标 → CONFLICT, 不双写不互删备份

def test_occupied_dispatch_by_overwrite_video(tmp_path):
    # 集成(从 OrganizeHandler 发起): 目标被占
    # overwrite_video=False → CONFLICT 跳过 (现状);
    # overwrite_video=True → 走覆写协议, 磁盘目标为源内容, 计数 overwritten=1
    ...

def test_overwrite_image_false_skips_existing_images(tmp_path):
    ...  # overwrite_image=False → 已有图片不覆盖, skipped 计数; True (默认) 维持覆盖
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_overwrite.py tests/handlers/test_file_ops.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现与迁移**

`organize_journal` 列逐字照抄规格 3.4.2（`mode/source_path/source_file_id/source_oshash/source_size/source_metadata_id/source_number/target_path/target_file_id/backup_path/phase/last_error`，UNIQUE(source_path, target_path)）。协议三阶段（规格 3.4.2）：阶段 A（前置检查 + 现场补算源指纹写入 journal 独立提交 + 备份 rename）；阶段 B（同 FS `os.replace(源,目标)` / EXDEV 走目标目录临时名，`os.replace` 返回成功即提交点）；阶段 C（**目标核验先行** → 接管+committed 同事务 → 删备份 → 删源前再次核验目标与源指纹 → 删源 → 删 journal）。回滚只在提交点之前；之后任何失败不删目标。

**接入现有冲突分支**：`execute_file_operations` 的 `OCCUPIED` 预检按 `common.overwrite_video` 分派（False → 现状 CONFLICT；True → 阶段 A 起，前置检查即该预检的延伸）；`overwrite_image` 传入 `_place_library_images`（False 时目标已有图片跳过并记 `skipped`）。覆写与跳过共用同一次 `target_state` 预检，分派只发生在预检结果之后。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/test_overwrite.py tests/handlers/test_file_ops.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/db src/amane/organize src/amane/handlers/file.py tests/organize tests/handlers docs/dev/organize.md src/amane/db/migrations/versions
git commit -m "feat(organize): 覆写协议 接入冲突分派与操作日志表"
```

### 任务 25：覆写索引接管（单事务）

**文件：**
- 修改：`src/amane/handlers/file.py`、`src/amane/db/repos/`（media file repo）
- 测试：`tests/organize/test_overwrite.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_takeover_overwrites_identity_fields(session):
    ...  # metadata_id/number 覆盖 (非仅补空), oshash 重算, 相位字段按新路径派生, 单事务

def test_takeover_target_without_index(session):
    ...  # target_file_id=NULL: 源行 path 改目标路径, 无目标旧行可删
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_overwrite.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

独立于 `commit_organized_media_file` 补空分支的接管路径：删目标旧行 + 源行改 path + 字段更新 + journal `phase=committed` 四步同一 `BEGIN IMMEDIATE` 事务。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ tests/handlers/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/handlers/file.py src/amane/db/repos tests/organize
git commit -m "feat(organize): 覆写索引接管单事务提交"
```

### 任务 26：journal 恢复扫描

**文件：**
- 修改：`src/amane/organize/overwrite.py`（恢复入口）、`src/amane/app/runtime.py`（启动挂钩）
- 测试：`tests/organize/test_overwrite_recovery.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_recovery_matrix(tmp_path):
    ...  # prepared×(目标缺失→还原备份 / 目标存在→核验后接管) / placed→重做接管 / committed→清理
def test_fingerprint_mismatch_blocks_takeover(tmp_path):
    ...  # source_file_id 被新文件复用 (指纹不等) → 拒绝接管, 按 source_path+指纹兜底; 全失败 → 保留 journal+last_error
def test_target_verification_blocks_cleanup(tmp_path):
    ...  # placed: 目标被等大小替换 → 不接管; committed: 目标被删/截断 → 不删源
def test_takeover_already_done_recovery(tmp_path):
    ...  # 源行 path==target_path 且指纹全等 → 按已完成接管进清理 (后置复验)
def test_cross_fs_source_delete_needs_target_valid(tmp_path):
    ...  # mode=move: 目标核验通过才删源; mode=copy 不删源
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_overwrite_recovery.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

按规格 3.4.2 恢复矩阵：指纹三字段全等（无 NULL 分支）；目标核验用同一指纹（提交点后目标内容即源内容）；接管已完成的后置复验；恢复失败一律保留 journal 写 `last_error`。启动扫描挂在 `AppRuntime` 初始化，另在整理同一目标前就地恢复。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/organize src/amane/app/runtime.py tests/organize
git commit -m "feat(organize): 覆写中断恢复扫描"
```

### 任务 27：MediaFile.link_path

**文件：**
- 修改：`src/amane/db/models.py`、`src/amane/api/models/media.py`、`src/amane/handlers/file.py`
- 新建迁移
- 测试：`tests/handlers/test_file_ops.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_link_path_persisted_after_organize(tmp_path):
    ...  # 配置链接模板整理后 MediaFile.link_path == 链接路径; 无链接模板 → None

def test_media_file_response_has_link_path():
    ...  # MediaFileResponse.link_path 存在
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/handlers/test_file_ops.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现与迁移**

`MediaFile.link_path: str | None`；`create_video_link` 返回 PLACED 时落库；`MediaFileResponse` 手加一行。链接文件已不存在时读取侧按无链接处理，不改写该列。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/handlers/ tests/api/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/db src/amane/api/models/media.py src/amane/handlers tests/handlers src/amane/db/migrations/versions
git commit -m "feat(db): 媒体文件持久化链接路径"
```

### 任务 28：resolve_sidecar_paths 拆分

**文件：**
- 修改：`src/amane/organize/path_templates.py`
- 测试：`tests/organize/test_path_templates.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_sidecar_only_renders_selected_artifacts(tmp_path):
    ...  # artifacts=("nfo",): thumb 模板越界抛错也不阻断; 其余字段 None; 视频与链接模板不渲染

def test_sidecar_context_matches_resolve_paths(tmp_path):
    ...  # 含 raw_* 的附属模板: resolve_paths 与 resolve_sidecar_paths(全集) 输出一致 (source_path 传入)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/organize/test_path_templates.py -x -q`
预期：FAIL（函数不存在）

- [ ] **步骤 3：实现**

签名照抄规格 3.4.4（`consume_path/video_path/source_path/ext/file_info/safe_dirs/actor_genders/artifacts`），并带任务 13 引入的 `naming: NamingConfig | None = None`（与 `artifacts` 正交：naming 决定用哪套模板，artifacts 决定渲染哪些产物）；`resolve_paths` 改为调用它（传全集），签名与返回类型不变。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/organize/ tests/handlers/ -q`
预期：PASS（既有用例**不得改断言**——等价性由它们锁住）

- [ ] **步骤 5：Commit**

```bash
git add src/amane/organize/path_templates.py tests/organize
git commit -m "refactor(organize): 附属产物按需渲染入口"
```

### 任务 29：NFO 单独重写端点

**文件：**
- 修改：`src/amane/api/routes/metadata.py`
- 修改：`docs/user/media-server.md`（重写端点与配对规则）
- 测试：`tests/api/test_metadata_nfo_rewrite.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
def test_rewrite_after_template_change_stays_paired(tmp_path, client):
    ...  # 只改 video_template / link_template / nfo_template 三种情况, 重写都以已落盘路径为基准
    # 库内视频 + 库外链接: NFO 落在 link_path 旁且 stem 相同

def test_rewrite_rejects_stem_mismatch(tmp_path, client):
    ...  # stem 不一致 → 422 + 实际与期望两个 stem

def test_rewrite_no_local_file_409(client):
    ...  # 409「无本地文件，无法重写 NFO」

def test_rewrite_multi_file_partial_failure(client):
    ...  # N 个文件独立处理, 失败清单随响应返回, 成功者不回滚
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/api/test_metadata_nfo_rewrite.py -x -q`
预期：FAIL

- [ ] **步骤 3：实现**

`PUT /api/metadata/{id}/nfo`：视频路径取 `MediaFile.path`（缺失 409）；消费路径取 `link_path`（存在且文件在盘）否则视频路径；调 `resolve_sidecar_paths(artifacts=("nfo",), source_path=MediaFile.path)`；校验 `nfo.parent == 消费.parent` 且 `nfo.stem == 消费.stem`，任一不满足 422；覆盖写。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/api/ -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/amane/api tests/api docs/user/media-server.md
git commit -m "feat(api): NFO 单独重写以消费路径为基准"
```

### 任务 30：原文字段前端

**文件：**
- 再生成：`web/src/client/`（`just generate` —— 穷尽映射依赖生成的 `MetadataField` TS 类型，旧类型不含新成员）
- 修改：`web/src/lib/exhaustive-maps.ts`（`METADATA_FIELDS` 加 `original_title` / `original_plot`）、`web/src/lib/media/metadata-fields.ts`（`LOCKABLE_FIELDS` / `FIELD_LABEL_KEY`）、`web/src/components/metadata/merge-dialog.tsx`（`TEXT_FIELDS`）、`web/src/routes/meta.$metadataId.tsx`（展示原标题/原文简介/官网链接）、`web/src/i18n/locales/{zh-CN,en}/metadata.json`
- 验证：`cd web && pnpm check`

- [ ] **步骤 1：刷新生成的 TS client**

运行：`just generate`
预期：`web/src/client/types.gen.ts` 的 `MetadataField` 联合类型含 `original_title` / `original_plot`。生成文件随本任务一并提交。

- [ ] **步骤 2：改穷尽清单（此刻 `tsc -b` 报 i18n key 缺失）**

在三处穷尽清单加两个新成员；`website` 不进 `MetadataField`（派生列），只加详情页展示。

- [ ] **步骤 3：运行 `pnpm check` 验证失败**

运行：`cd web && pnpm check`
预期：FAIL（i18n key 缺失）

- [ ] **步骤 4：补 i18n 与详情页展示**

`detail.fields.original_title` / `original_plot`（zh-CN：原标题 / 原文简介）；详情页只读展示 `website` 链接。

- [ ] **步骤 5：运行 `pnpm check` 验证通过**

运行：`cd web && pnpm check`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add web/src
git commit -m "feat(web): 原文双轨字段展示与锁定"
```

### 任务 31：覆盖编辑、识别词页与试算面板

**文件：**
- 新建：`web/src/routes/parse-rules.tsx`（识别词管理）
- 修改：库详情页新增「配置覆盖」Tab（按 section 稀疏编辑 + 继承开关）、模板编辑器旁「试算」面板（调任务 22 端点；切换语法时未自定义的模板替换为对应默认）
- 验证：`cd web && pnpm check`

- [ ] **步骤 1：编写组件与 API 调用**

TS client 已在任务 30 步骤 1 由 `just generate` 刷新（含全部新端点类型），本任务直接使用。

- [ ] **步骤 2：实现三块 UI**

覆盖 Tab 按 section 分块编辑稀疏 payload（空对象表示显式取空，无 null 输入）；识别词页表格式管理（全局/库级作用域筛选）；试算面板实时显示渲染结果与 422 错误文本。

- [ ] **步骤 3：运行 `pnpm check` 验证通过**

运行：`cd web && pnpm check`
预期：PASS

- [ ] **步骤 4：Commit**

```bash
git add web/src
git commit -m "feat(web): 覆盖编辑 识别词管理与试算面板"
```

### 任务 32：文档同步与打包验证

**文件：**
- 修改：`docs/dev/config.md`（若任务 14 未覆盖全部合成语义）、`docs/dev/data-model.md`（原文双轨来源与按字段语言、`website` 派生）、`docs/dev/organize.md`（模板双引擎、语法差异、覆写协议与 journal）、`docs/user/organize.md`（用户可见命名/整理行为）、`docs/dev/index.md`（登记 `organize.md` / `parsing.md`）
- 验证：`just check`；`google-re2` wheel 在桌面打包（PyInstaller）与 Docker 镜像中可用

- [ ] **步骤 1：核对规格 §3.5 表逐项落点，补齐遗漏段落**

先精简再加：每处并入既有段落，文档只持平或变短除非新增跨模块契约。

- [ ] **步骤 2：验证二进制依赖进包**

`google-re2` 是 C 扩展：PyInstaller 打包后启动一次确认 `import re2` 成功；Docker 镜像构建后同样验证。

- [ ] **步骤 3：全量检查**

运行：`just check`
预期：PASS（lint + format + typecheck + web check + test 全绿）

- [ ] **步骤 3：Commit**

```bash
git add docs
git commit -m "docs: 同步 MDC P0+P1 落地后的知识库"
```

---

## 自检记录

- **规格覆盖度**：规格 §3.1（10/11/12/13/14 任务）、§3.2（17–22 任务）、§3.3（2–6 任务）、§3.4（8/9/23–29 任务）、§3.5（9/14/15/29/32 任务折入）、§4 错误处理（各任务测试行 + §4 表逐项有对应断言）、§5 测试策略（各任务步骤 1 的测试名与规格 §5 表逐行对应）、§6 顺序（任务编号即其 24 步的展开）。规格 §7 待确认第 8 项（`copy_resources` 默认三处不一致）不在实现范围，任务 10 按规格取「全集含 trailer」默认。
- **类型一致性**：`NfoOptions` 在任务 8 定义、任务 9/13 引用同名；`ConfigResolver.resolve` 签名在任务 11 定义、任务 13/14 引用；`ParseRuleSet`/`apply_rules` 任务 15 定义、任务 16 引用；`resolve_sidecar_paths` 任务 28 定义（`naming` 参数承接任务 13、`artifacts` 参数供任务 29 使用）、任务 29 引用；`organize_journal` 列名在任务 24/26 一致。
- **审查重点**：五条分别钉在任务 24（并发覆写）、15（RE2 线性引擎）、12（迁移幂等）、14（未知键）、29（部分失败）。
- **比例**：计划 ≈ 规格 78% 篇幅；代码块只有测试骨架断言与规格定死的字面量（默认模板、字段清单、端点路径），无函数体。
