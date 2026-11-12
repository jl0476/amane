# 设计规格：配置覆盖层与命名整理（MDC 迁移 P0 + P1）

> 日期: 2026-11-12
> 状态: 待审阅（第 2 版，含 4 项决策与数据字段扩展）
> 范围: MDC 能力迁移的第一阶段（配置基础层 + 命名整理 + 原文双轨字段）
> 上游总纲: `.research/mdc/migration-roadmap.md`

## 0. 已定决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | Jinja2 依赖策略 | **主依赖**（非 optional extra） |
| 2 | 覆盖表 vs `Library` 行优先级 | **新覆盖表优先** |
| 3 | 识别词表设计 | **独立表** `parse_rules`（不并入 `FacetRule`） |
| 4 | NFO 的 `originaltitle`/`originalplot`/`website` | **给 `Metadata` 加字段**，`original_plot` 取**原文语义**（源站原文，日文），`website` 加**单值列** |
| 5 | 命名引擎 | 自研语法为默认 + Jinja2 兼容层 |
| 6 | 人脸识别裁剪 | OpenCV（P2，本阶段只预留接口） |

## 1. 目标

把 MDC 的「逐条目配置覆盖」与「命名整理」能力以原生 Python 重新实现进 Amane，
不搬运 MDC 代码，不复用其容器。

**成功标准**（可验证）：

1. 一个媒体库能配置自己的命名模板与整理策略，覆盖全局默认而不影响其它库。
2. 用户能为「国产 / 欧美 / 素人 / 无码」自定义番号前缀与路径关键词识别规则。
3. 命名模板支持条件块、filter、变量降级链；自研语法与 Jinja2 两套并存且可切换。
4. 整理支持 5 种模式（含原地），冲突时可选择跳过或覆写。
5. NFO 的每个字段可独立开关，不再固定。
6. 命名模板可试算：给定一个番号或一条媒体文件，输出最终路径与文件名，不落盘。
7. **元数据保留原文双轨**：`original_title` / `original_plot` 存源站原文（日文），
   `title` / `plot` 存翻译后（中文），两者并存不互相覆盖。
8. `website` 存来源站详情页 URL 的主值，`source_urls` 保留全量。
9. 全程兼容现有 Amane 数据：既有库的落盘结果不因升级而改变。

**非目标**：

- 不做图片处理链（P2）、水印系统（P3）、插件 API v2（P4）。
- 不引入 MDC 的任何代码或前端资源。
- 不做下载器集成。
- 不改���刮削来源的选取逻辑（只补字段取值，不改路由与优先级）。

## 2. 现状与约束

### 2.1 现有配置层次

Amane 现有三层配置，**语义是「复制」而非「继承」**：

| 层 | 位置 | 语义 |
|---|---|---|
| 全局 | `src/amane/config/manager.py::HotSettings` | TOML 持久化，`exclude_none=True` 落盘 |
| 库级 | `src/amane/db/models.py::Library` | 字段直接复制全局值 |
| 任务级 | `src/amane/handlers/models.py::LibraryBase._apply_library` | payload 字段为 `None` 时沿用库设置 |

`_deep_merge`（`manager.py:872`）只合并两层，更深层由 patch 整体替换。

### 2.2 现有命名能力

- 模板引擎 `src/amane/organize/template.py::Parser` / `TemplateEngine` / `PathEngine` / `StrmEngine`
- 语法：`{name}`、`{name|k=v}` 值映射、`{name|=缺省}`、`[...]` 可选组、`[[...]]` 双层组
- 31 个占位符（`TemplateVariables`），`PLACEHOLDER_MAP_KEYS` 定义值映射白名单
- 落盘路径解析 `src/amane/organize/path_templates.py::resolve_paths` → `ResolvedPaths`
- 视频扩展名始终取源文件（`organize/file.py::video_dest`）
- 路径逃逸防护 `PathEngine.resolve`（相对模板须在 `base_path` 下，绝对模板须在 `base_path` 或 `safe_dirs` 下）
- 冲突判定 `organize/file.py::target_state` → `TargetState`（FREE/SAME/OCCUPIED/UNKNOWN）

### 2.3 现有 NFO

`src/amane/media/nfo.py::write_nfo` —— Kodi `<movie>` XML，字段全部硬编码。
`docs/user/media-server.md:78` 明确写「NFO 内容固定, 不提供自定义模板」。

### 2.4 约束

- `src/amane/parsing/` 是**纯函数包，无 I/O 与配置依赖**（`docs/dev/architecture.md` 模块边界）。
  用户自定义识别词不能靠读配置实现。
- 插件是进程内纯 Python、无沙箱、不能自带第三方依赖（`docs/dev/plugins.md`）。
- 配置改动要么热生效（走 `AppRuntime._rebuild`），要么标注需重启。

## 3. 设计

### 3.1 配置覆盖层（P0）

#### 3.1.1 数据模型

引入两个 DB 表，**存稀疏 JSON，不复用 Pydantic 模型表达 null**：

```
config_overrides
  id            PK
  scope_type    TEXT   -- 'library' | 'watch_dir'
  scope_id      INTEGER -- libraries.id 或 watch_dirs.id
  section       TEXT   -- 'common' | 'naming' | 'download' | 'nfo' | 'watermark' | 'parser' | 'metadata' | 'provider'
  payload       TEXT   -- JSON, 稀疏；只含显式设置的键
  created_at, updated_at
  UNIQUE(scope_type, scope_id, section)
```

`payload` 是**稀疏**的：缺失键 = 继承全局；显式值 = 覆盖。
这样避开了 `exclude_none=True` 落盘与「null 表示继承」的语义冲突 ——
null 不再出现，稀疏本身表达覆盖。

**section 的划分**沿用 MDC 的 8 个，但只落地本阶段需要的：
`common`（整理与过滤）、`naming`（命名模板）、`nfo`（NFO 字段开关）、
`download`（下载内容与裁剪策略，P2 用）、`watermark`（P3 用）、
`parser`（识别词）、`provider`（源配置）、`metadata`（刮削后处理）。

本阶段实现 `common` / `naming` / `nfo` / `parser` 四节，其余预留。

#### 3.1.2 生效配置解析

新增 `src/amane/config/overrides.py`：

```python
class ConfigResolver:
    """把全局配置与条目覆盖合成生效配置。"""

    def resolve(
        self,
        hot: HotSettings,
        section: str,
        scope: Scope | None = None,
        payload: dict | None = None,
    ) -> Any:
        """返回该 section 的生效模型。"""
```

合成规则：

1. 取 `hot` 中该 section 的模型实例。
2. 若有 `scope`，取其覆盖 payload；对 payload 中**显式出现的键**逐个覆盖到模型上。
3. 若有 `payload`（任务级），同上，但优先级高于 scope。
4. 嵌套结构：payload 的值若是 dict，与全局值**逐键深合并**；
   非 dict 值整体替换。

调用点：

| 位置 | 用途 |
|---|---|
| `handlers/file.py::OrganizeHandler` | 整理时取 `common` / `naming` / `nfo` |
| `handlers/file.py::execute_file_operations` | 落盘路径与 NFO 生成 |
| `handlers/refresh.py::RefreshHandler` | 扫描时的 `common` 过滤规则 |
| `api/routes/libraries.py` | 库详情返回「当前生效配置」 |

#### 3.1.3 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/libraries/{id}/overrides` | 返回该库的全部覆盖（按 section） |
| PUT | `/api/libraries/{id}/overrides/{section}` | 整体替换某 section 的覆盖 payload |
| DELETE | `/api/libraries/{id}/overrides/{section}` | 清空某 section（回到继承） |
| GET | `/api/libraries/{id}/effective-config` | 返回全局 + 覆盖的合成结果（前端预览用） |

写操作必须校验：section 必须在白名单内；payload 必须能被对应 section 模型校验通过
（用 `HotSettings` 的子模型 `model_validate`）。

#### 3.1.4 与现有 Library 字段的关系

`Library` 表已有的模板字段（`video_template` / `thumb_template` / … / `write_nfo`）
与新的覆盖表**并存**，优先级为：

```
任务 payload  >  config_overrides 表  >  library 行字段  >  全局 HotSettings
```

**覆盖表优先**（用户决策）。理由：覆盖表是显式的、用户主动设置的意图，
其表达力高于 `Library` 行的复制式字段。

**但必须保证既有库行为不变**。`Library` 行的模板字段有**非空默认值**
（`video_template` 默认 `VIDEO_TEMPLATE_DEFAULT`，非空；`write_nfo` 默认 `True`），
无法用「非空即生效」区分「用户显式设过」与「继承来的默认值」。

因此引入**「显式设置」标记**：

- `Library` 表新增一列 `template_overrides: list[str]`，记录哪些模板字段是**用户显式设置**的。
- 新建库时该列为空 —— 全部模板字段视为继承（覆盖表 → 全局）。
- 用户在库表单里改动某个模板字段时，把该字段名加入此列。
- 生效时：`Library` 行的某个模板字段，**只有在其名字出现在 `template_overrides` 中时**才生效；
  否则落到覆盖表 / 全局。

这样既有库（`template_overrides` 为空）行为不变 —— 它们的模板字段虽然非空，
但会被视为「未显式设置」从而落到全局默认。

**代价**：既有库的库级模板会突然失效，改为使用全局默认。
因为既有库的模板值本就等于创建时的全局默认，实际落盘结果不变。
但若用户曾改过某个库的模板（改动写在 `Library` 行、未记入新列），
升级后该改动会失效。

**缓解**：加 Alembic 数据迁移 —— 迁移时把 `Library` 行中**与全局默认不同**的模板字段
写入 `template_overrides`。这样保留了用户的历史改动。

**例外**：`move_mode`（整理模式）、`link_mode`、`subtitle_extensions`、
`copy_resources`、`min_file_size`、`blacklist_patterns` 等**非模板类**字段
不参与这套机制 —— 它们本来就是显式的库级设置，语义清晰，保留现状。

#### 3.1.5 用户自定义识别词（P0）

新增 DB 表：

```
parse_rules
  id           PK
  kind         TEXT   -- 'number_keyword' | 'path_keyword'
  content_type TEXT   -- 'censored' | 'uncensored' | 'amateur' | 'chinese' | 'western'
  pattern      TEXT   -- 字面关键词（大小写不敏感）或 r: 前缀的正则
  enabled      BOOLEAN
  created_at, updated_at
  UNIQUE(kind, content_type, pattern)
```

**交互规则**（对齐 MDC）：

1. 番号关键词与路径关键词**都命中**时，番号优先。
2. 路径关键词匹配**完整路径**（含文件名）。
3. 命中后**覆盖**内置的类型判定（即把该文件判为指定 `content_type`）。
4. 未命中任何自定义规则时，走现有 `parsing/file_info.py` 逻辑。

**实现约束**：`parsing/` 是纯函数包。因此：

- 新增 `src/amane/parsing/rules.py`，定义 `ParseRuleSet`（冻结 dataclass，
  由 DB 行构造）与 `apply_rules(info: FileInfo, rules: ParseRuleSet) -> FileInfo`。
- `parse_file_info` **签名不变**（保持向后兼容），新增可选参数
  `rules: ParseRuleSet | None = None`。
- `parsing/` 仍然不读配置、不做 I/O —— 规则由调用方（handler / API）从 DB 读出后传入。
- `rules.py` 的匹配逻辑必须是纯函数，便于表测试。

**规则缓存**：`ParseRuleSet` 按 DB 行的 `updated_at` 版本号缓存于
`Runtime`（随 `AppRuntime._rebuild` 重建）。识别词变更后触发 rebuild。

#### 3.1.6 前端

- 库详情页新增「配置覆盖」Tab：按 section 分块编辑，编辑的是**稀疏覆盖**而非全量。
- 每块顶部有「继承全局」开关：关闭时该 section 整体回到继承。
- 识别词规则管理页：新增 `/parse-rules` 路由，或并入设置页的 `scraping` section。
  建议独立路由（规则是多行记录，不是配置字段）。

### 3.2 命名模板引擎（P1）

#### 3.2.1 双引擎并存

新增 `src/amane/organize/template_jinja.py`，提供 Jinja2 实现。
`src/amane/organize/template.py` 的自研实现保留为默认。

```python
class TemplateBackend(Protocol):
    """渲染命名模板。"""

    def render(self, source: str, variables: Mapping[str, Any]) -> str: ...
    def validate(self, source: str) -> None: ...
    def describe(self) -> TemplateSyntax:  # 语法元信息，供前端高亮
        ...
```

实现：

- `NativeTemplateBackend` —— 包装现有 `TemplateEngine`，并**扩展语法**（见 3.2.2）。
- `JinjaTemplateBackend` —— 包装 Jinja2。

**Jinja2 的安全约束**（Amane 渲染的是路径，必须防注入）：

- 使用 `SandboxedEnvironment`（`jinja2.sandbox.SandboxedEnvironment`）。
- 禁用 `attr` / `getitem` 对下划线开头的属性访问（SandboxedEnvironment 默认行为）。
- 输出经 `PathEngine.clean` 的路径段规范化（去段尾 `.`/空格、丢空段）。
- 渲染结果仍须过 `PathEngine.resolve` 的逃逸防护。

**Jinja2 是主依赖**（用户决策）：加进 `pyproject.toml` 的 `[project].dependencies`。
理由：体积小（~1MB），且用户在模板里用了 Jinja2 语法后缺依赖会导致任务失败而非降级；
主依赖可避免「已保存的 Jinja2 模板在换机或升级后突然不能用」。
装在桌面版与 Docker 版也无额外负担。

#### 3.2.2 自研语法扩展

在现有语法上补三项能力（对齐 MDC 的 Jinja2 能力，但保持自研引擎的确定性）：

| 能力 | 语法 | 语义 |
|---|---|---|
| 条件块 | `{% if expr %}…{% else %}…{% endif %}` | expr 为变量名或变量名列表（真值判定：非空且非「未知」） |
| filter | `{name \| filter \| filter}` | 见下表 |
| 降级链 | `{a \| default(b \| default("未知"))}` | 从左到右取第一个「有值」的 |

**filter 集合**（对齐 MDC 的常用 filter）：

| filter | 语义 |
|---|---|
| `default(x)` | 无值时用 `x`（`x` 可为嵌套 filter 链或字面量） |
| `upper` / `lower` | 大小写转换 |
| `truncate(n)` | 截断到 n 字符（**按 UTF-8 字节**计，与现有 `_clip_field` 一致） |
| `split("-")` | 拆分，返回列表；只能与 `first` / `last` 组合 |
| `first` / `last` | 取列表首/末项 |
| `replace(a, b)` | 字符串替换 |
| `trim` | 去首尾空格 |

**「有值」的判定**（统一定义，全引擎一致）：

```
None / 空字符串 / 空列表 / 字符串 "未知" / 字符串 "None"  →  无值
其它 → 有值
```

MDC 的 Jinja2 语义是「缺失时为空字符串」，Amane 现有语义是「缺失时显示『未知』」。
两者不同，因此**两套引擎的「无值」语义必须分别定义**：

- 自研引擎：无值 = 上述定义（沿用 Amane 现状）
- Jinja2 引擎：无值 = 空字符串 / None（Jinja2 原生语义，不特殊处理「未知」）

`default` filter 在两套引擎下都用各自的无值定义。

**表达式语法限制**：自研引擎不做任意表达式求值。
`{% if %}` 的条件只能是「变量名」或「变量名列表」，不含运算符、不含函数调用。
理由：路径模板由用户在 Web UI 里编辑，需要**可静态分析**（前端高亮、校验、试算），
且要杜绝求值注入。

#### 3.2.3 模板来源与语法标识

模板需要记录用的是哪套语法。方案：

- 新增 `Library` 列 `template_syntax: str`（默认 `"native"`），取值 `native` | `jinja`。
- 覆盖表的 `naming` section 里对应字段同理。
- 前端模板编辑器提供语法切换，切换时**尝试用目标引擎解析**，
  解析失败则拒绝并提示（不静默改写模板）。

`/api/libraries/path-template-schema` 响应新增 `syntaxes` 字段，
列出可用语法与各自的占位符/能力。

#### 3.2.4 命名试算（P1 必备）

新增端点：

```
POST /api/libraries/naming/preview
```

请求：

```json
{
  "library_id": 1,
  "syntax": "native",
  "number": "ADN-426",
  "source_path": "/media/115/ADN-426-U.mp4",
  "content_type": "censored",
  "title": "…", "actors": ["…"], "studio": "…", "release": "2022-10-03"
}
```

响应：解析结果（番号、类型、马赛克、中字、分辨率）+ 渲染后的各产物路径
（video / thumb / poster / fanart / nfo / trailer / subtitle）。

**不落盘**，纯计算。要求：

- 试算走**与真实落盘完全相同**的代码路径（`resolve_paths` + `TemplateContext`），
  避免「试算通过但实际落盘不同」。
- 支持 `library_id` 缺省（用全局默认模板试算）。
- 支持「用某条真实 `MediaFile` 试算」：`{"media_file_id": 123}`，
  从 DB 取真实文件与已刮削的元数据。

前端：模板编辑器旁加「试算」面板，实时显示渲染结果与错误。

### 3.3 元数据原文双轨字段

MDC 的 NFO 有 `OriginalTitle`（日文原标题）与 `OriginalPlot`。
Amane 现有 `Metadata` 只有 `title` / `plot`（单一轨，翻译后就覆盖了原文）。

**调研结论**：原文数据在多数源天然可得，只是当前被压平或丢弃。

#### 3.3.1 字段语义

| 字段 | 语义 | 来源 |
|---|---|---|
| `title` | 翻译后（中文） | 现有链路：源站值 → LLM 翻译 |
| `original_title` | **源站原文**（通常日文） | 新增 |
| `plot` | 翻译后（中文） | 现有链路 |
| `original_plot` | **源站原文**（通常日文） | 新增 |
| `website` | 来源站详情页 URL（主值） | 新增单值列 |

**与 MDC 的命名差异**：MDC 的 `OriginalPlot` 实际是**中文剧情**（等于 Amane 的 `plot`），
命名与直觉相反。Amane 的 `original_plot` 明确定义为**源站原文**，
以免继承 MDC 的歧义。

#### 3.3.2 数据可得性（调研实测）

| 源 | 原标题 | 原文简介 | 说明 |
|---|---|---|---|
| **DMM** | ✅ GraphQL `ppvContent.title` 实测**纯日文** | ✅ `ppvContent.description` 实测日文 | 无多语言维度，描述与 `description(format: HTML)` 同文本 |
| **MGStage** | ✅ `h1` 日文 | ✅ `dl#introduction` 日文 | 日文站 |
| **official**（厂牌官网） | ✅ `//title/text()` | ✅ `p-workPage__text` | 日文官网 |
| **wp_works**（faleno/dahlia） | ✅ ACF `作品名` | — | 日文官网 |
| **avsox** | ✅ `title_ja` **已在模型里** | ✅ `description_ja` **已在模型里** | 被 `_first` 压平 |
| **r18dev** | ✅ `title_ja` **已在模型里** | ✅ `comment_ja` **已在模型里** | mapper 二选一 |
| javlibrary / jav321 / xcity / giga / prestige | ✅ 站点即日文 | 部分 | — |
| javdb | ⚠️ 需 `?locale=ja`（当前固定 `locale=zh`） | ❌ 不解析 plot | — |
| javbus | ⚠️ 需 `Accept-Language: ja`（当前固定 `zh-CN`） | ❌ 不解析 plot | — |
| iqqtv | ⚠️ 需 `/jp` 前缀（`_LANG_PREFIX` 已备好） | ⚠️ 同 | — |
| getchu / kin8 / fc2* / airav / freejavbt | ❌ | ❌ | 无原标题概念 |

**结论**：`original_title` 对**有码片**（DMM 路由首位）几乎必然可得；
`original_plot` 对 DMM / MGStage / official 可得，对 Javdb / Javbus / Javlibrary 不可得（不解析 plot）。

#### 3.3.3 顺带修复的两个既有缺陷

调研发现两处会阻碍双轨实现的问题：

1. **avsox 未声明 `SourceTrait.MULTI_LANGUAGE`** — `src/amane/crawlers/sites/avsox.py::AvsoxCrawler.profile`
   只返回 `name` / `base_url`，缺 `traits`。因此 `aggregate/engine.py::_resolve_lang`
   不会给它传 `language`，`avsox.py::_lang` 恒返回 `"cn"`，
   `title_ja` / `description_ja` **永远取不到**。这是死代码。
   修复：补 `traits=frozenset({SourceTrait.MULTI_LANGUAGE})`。

2. **avsox 的 `_first` 顺序把中文排在日文前** — `avsox.py:161-167`
   `plot=_first(movie.description, movie.description_cn, movie.description_ja, ...)`。
   即使拿到日文轨也会先取中文。修复：改为按 `language` 精确取，并保留 `_ja` 轨。

**这两处修复是双轨实现的前置条件**，必须先做。

#### 3.3.4 实现方案

**爬虫层**：

- `MediaMetadata` 新增 `original_title: str | None = None`、
  `original_plot: str | None = None`。
  **必须有默认值** —— 否则存量 `raw` 快照反序列化会抛 `TypeError`，
  导致整站快照作废（`aggregate/engine.py:514` 的 `MediaMetadata(**cached)`）。
- 各源在返回时同时给出原文与译文侧的字段：
  - DMM / MGStage / official / wp_works / javlibrary / jav321 / xcity / giga / prestige：
    `original_title = title`（这些源的 title 本身就是原文）。
  - avsox：`original_title = movie.title_ja`，`original_plot = movie.description_ja`；
    `title` / `plot` 仍按 `language` 取对应语言。
  - r18dev：`original_title = v.title_ja`，`original_plot = v.comment_ja`；
    `title` / `plot` 保持现有按 language 的二选一。
  - Javdb / Javbus / iqqtv：**本阶段不改**（需改 locale/请求头，会影响既有抓取行为）。
    它们不提供原标题，`original_title` 留空。

**翻译层**：

- `handlers/scrape.py::_translate_metadata` 当前就地覆盖 `title` / `plot`。
  改为：翻译前先记录原文到 `original_title` / `original_plot`
  （若原文字段为空而 `title`/`plot` 非空，则**在翻译前**把当时的值存入原文字段）。

  这一步很关键：**即使某个源不显式提供原文字段，
  只要它返回的是原文（未被翻译过的值），翻译前的快照就是原文。**
  翻译发生在聚合之后（`handlers/scrape.py:147-148` → `_translate_metadata`），
  此时 `title` / `plot` 仍是源站原文，翻译前的快照即为原文。

  但要注意：字段链上第一个非空的源可能**已经是中文站**（如 airav）。
  此时「翻译前的 title」是中文，存进 `original_title` 就错了。
  因此**必须由源显式声明**，不能靠翻译前快照兜底。

  方案：新增 `MediaMetadata.source_language: str | None = None`，
  由源声明自身语言（如 DMM 声明 `"ja"`）。翻译层据此判断：
  - `source_language` 是非中文语言（如 `ja` / `en`）→ 翻译前的 `title`/`plot` 即原文，
    存入 `original_title` / `original_plot`。
  - `source_language` 是中文或未声明 → 不写原文字段（该源不提供原文）。

  已有 `crawlers/models.py::FetchOptions.language` 与
  `crawlers/site_roles.py::MULTI_LANGUAGE_SITES` 可复用，但那是**请求语言**而非**响应语言**。
  新增 `source_language` 字段更直接。

**聚合层**：

- `enums.py::MetadataField` 新增 `ORIGINAL_TITLE` / `ORIGINAL_PLOT` / `WEBSITE`
  三个枚举值（**值必须等于 `MediaMetadata` 属性名**，
  `tests/crawlers/test_enum_consistency.py:37-40` 硬断言）。
- `aggregate/engine.py::SCALAR_FIELDS` 加入这三个字段，
  否则不会参与聚合（`_fill_scalar` 走通用 `setattr` 分支，无需额外改动）。
- `aggregate/engine.py::_normalize_source_text` 与
  `db/repos/metadata.py::_normalize_text_fields` 需为 `original_plot` 加长文本归一
  （当前只归一 `plot`）。
- `field_sources` 会自动记录新字段的来源（`_fill_scalar`）。

**DB 层**：

- `Metadata` 加四列：`original_title` / `original_plot` / `website` /
  以及 `source_language` 是否落库需定 —— **建议落库**（便于详情页展示「该条目来自哪个语言的源」，
  且重新刮削时需要）。
- `db/repo_types.py::MetadataFields` TypedDict 加键（**必改**，静态检查会报错）。
- `db/repos/metadata.py::update_metadata` **显式逐字段赋值**
  （项目禁 `setattr`，`docs/dev/data-model.md:84`）。
- Alembic migration：用 `uv run alembic revision --autogenerate -m "..."` 生成，
  SQLite 加可空列用 `op.batch_alter_table`。

**API 层**：

- `api/models/metadata.py::MetadataResponse` **手写**，必须手动加四行。
- `PartialMetadata` 由 `create_partial_model(Metadata, ...)` **自动派生**，无需改。

**前端层**（三处 `assertExhaustive` 编译期强制，漏改会编译失败 —— 这是好事）：

- `web/src/lib/exhaustive-maps.ts::METADATA_FIELDS`
- `web/src/lib/media/metadata-fields.ts::LOCKABLE_FIELDS` + `FIELD_LABEL_KEY`
- `web/src/components/metadata/merge-dialog.tsx::TEXT_FIELDS`
- `web/src/routes/meta.$metadataId.tsx` 展示原标题 / 原文简介 / 官网链接
- i18n：`web/src/i18n/locales/{zh-CN,en}/metadata.json`

**NFO 收益**：

`media/nfo.py::write_nfo` 当前把 `<title>` 的同值写进 `<originaltitle>` ——
这是**既有缺陷**。加 `original_title` 后，`<originaltitle>` 写真实原标题，
`<title>` 写中文标题。同时 `<plot>` / `<outline>` 分离为翻译后 / 原文。

#### 3.3.5 `website` 单值列

- `Metadata` 加 `website: str | None`。
- 取值：字段链上第一个非空源的 `source_url`。
  `aggregate/engine.py` 现有 `source_urls` 填充逻辑是「先到先得」
  （`state.result.source_urls[ck] = data.source_url`），
  `website` 取同一顺序的**第一个**值。
- `source_urls` 保留全量不变。
- 落库时 `website` 与 `source_urls` 同时写。

#### 3.3.6 影响面清单

以 `title` 为参照，`original_title` 的完整链路需改动：

| 段 | 位置 |
|---|---|
| 爬虫取值 | `crawlers/sites/dmm.py::DmmCrawler._scrape_digital` 等各源 `_scrape` |
| 爬虫模型 | `crawlers/models.py::MediaMetadata` |
| 站点快照 | `aggregate/engine.py::aggregate` |
| 聚合取值 | `aggregate/engine.py::_resolve_scalars` → `_fill_scalar` |
| 翻译前原文留存 | `handlers/scrape.py::_translate_metadata` |
| 落库 | `handlers/scrape.py::ScrapeHandler.handle` → `db/repos/metadata.py::upsert_metadata` |
| DB 列 | `db/models.py::Metadata` |
| repo 更新 | `db/repos/metadata.py::update_metadata` |
| API 响应 | `api/models/metadata.py::MetadataResponse`（手写） |
| API PATCH | `api/models/metadata.py::PartialMetadata`（自动派生） |
| 枚举 | `enums.py::MetadataField` |
| 字段链 | `aggregate/engine.py::SCALAR_FIELDS` |
| 长文本归一 | `aggregate/engine.py::_normalize_source_text` + `db/repos/metadata.py::_normalize_text_fields` |
| TypedDict | `db/repo_types.py::MetadataFields` |
| NFO | `media/nfo.py::write_nfo` |
| 前端 | `meta.$metadataId.tsx` + 三处 `assertExhaustive` + i18n |

**注意**：`MetadataField` 新增值后，以下两处**不会自动生效**，需手动加：

- `config/manager.py::LANG_METADATA_FIELD_SET` — 新字段若要参与多语言刮削，
  必须同时加进此集合（`_resolve_lang` 的两个条件是 AND）。
  本阶段**不加** —— 原文字段不走多语言展开。
- `config/manager.py::ScrapingConfig.field_priority` / `field_blacklist` 的 key 集合
  （`x-frozen-keys`）—— 新字段若要可配字段优先级，需加入 `_complete_frozen_dict` 的键集。
  本阶段**加入**，让用户能配「原标题优先取 DMM 还是 MGStage」（MDC 有此配置）。

### 3.4 整理（P1）

#### 3.4.1 整理模式

`src/amane/enums.py::MoveMode` 增加 `INPLACE = "inplace"`。

语义：**源文件不动，在源文件所在目录内生成整理结果**（图片、NFO、字幕）。
MDC 的「原地整理」是「在源文件夹内生成整理结果，适合已整理过的媒体库」。

实现要点：

- `organize/file.py::execute_organize` 的 `match mode:` 增加 `inplace` 分支 —— 不移动视频。
- 视频的目标路径 = 源路径（不改名）。
- 图片/NFO 的落盘位置：按模板渲染，但**基准目录**是视频所在目录。
- `handlers/file.py::OrganizeHandler` 的 `collect_prune` 条件不变（inplace 不腾空目录）。

与现有 `MoveMode` 的兼容性：新增枚举值不影响既有库（既有库存的是旧值）。
迁移：无需 Alembic revision（枚举以字符串存储），但需确认 `Library.move_mode`
列的校验路径会接受新值。

#### 3.4.2 覆写策略

`common` section 新增：

```
overwrite_video: bool = False   # 目标已有视频时覆写
overwrite_image: bool = False    # 目标已有图片时覆写
```

默认 `False` —— **保持现有行为**（冲突即跳过）。

覆写时的安全约束：

- 覆写视频 = 删除目标后移动/链接源。删除前必须确认目标是**视频文件**且在本库内。
- 覆写图片 = 覆盖写。
- 覆写**永不**跨越库边界（`safe_dirs` 检查照常生效）。
- 覆写决策记入任务结果（新增计数 `overwritten`），并在日志中留痕。

#### 3.4.3 NFO 字段开关

新增配置模型 `NfoOptions`（独立 `nfo` section）：

```
enabled: bool = True
sorttitle: bool = True
originaltitle: bool = True
outline: bool = True
originalplot: bool = False
plot: bool = True
actor: bool = True
director: bool = True
release: bool = True
releasedate: bool = True
premiered: bool = True
year: bool = True
runtime: bool = True
votes: bool = False
score: bool = True
criticrating: bool = True
country: bool = False
mpaa: bool = True
mpaa_value: str = "JP-18+"
customrating: bool = False
series: bool = True
set: bool = True
tag: bool = True
genre: bool = True
studio: bool = True
maker: bool = True
publisher: bool = True
label: bool = True
poster: bool = True
cover: bool = True
trailer: bool = True
website: bool = True
num: bool = True
external_ids: bool = True
add_part_to_title: bool = False
add_translated_by: bool = False
tagline_format: str | None = None
set_by_actress: bool = False
set_by_serial_name: bool = False
set_by_series: bool = False
additional_tags: list[str] = []   # 附加标签内容
```

`write_nfo(metadata, nfo_path)` 签名改为接受 `options: NfoOptions`。

**关于 `originaltitle` / `originalplot`（有真实原文后语义变化）**：

现有实现把 `<title>` 的同值写进 `<originaltitle>`，把 `plot` 的同值写进 `<outline>`
（`media/nfo.py:61-65` 与 `:76-80`）。这是**既有缺陷**。

加了 `original_title` / `original_plot` 后：

| 元素 | 现有行为 | 新行为 | 默认开关 |
|---|---|---|---|
| `<title>` | 中文标题 | 中文标题（不变） | `True` |
| `<originaltitle>` | 中文标题（同值） | **真实原标题**（`metadata.original_title`）；为空时回退写中文标题 | `True` |
| `<plot>` | 剧情 | 剧情（译文，不变） | `True` |
| `<outline>` | 剧情（同值） | **原文简介**（`metadata.original_plot`）；为空时回退写译文 | `True` |
| `<website>` | 不写 | `metadata.website`（来源站详情页 URL） | `True` |

**默认值保持输出兼容**：`original_title` / `original_plot` 为空的条目
（存量数据尚未重新刮削）写出的 XML 与现状一致。

**注意 `outline` 语义反转的风险**：Kodi / Emby 里 `<outline>` 通常是「详细剧情」，
`<plot>` 是「一句话简介」。本设计让 `<outline>` 承载**原文**简介，
若原文很长会改变 Emby 的展示长度。
**需在 `docs/user/media-server.md` 说明这一点**，并让 `originalplot` 开关可关
（关闭时 `<outline>` 回退写译文，与现状一致）。

**默认值必须与现有 `write_nfo` 的实际输出逐字段一致。**
现有实现的写入规则（`src/amane/media/nfo.py`，已核对）：

| 现有行为 | 对应开关默认值 |
|---|---|
| `plot` 与 `outline` 同写 `metadata.plot`，二者同时出现 | `plot=True` `outline=True` |
| 无 `originalplot` | `originalplot=False` |
| `premiered` / `releasedate` 同写 `metadata.release`；`year` 由 release 的前 4 位派生 | `premiered=True` `releasedate=True` `year=True` |
| 无独立 `release` 字段 | `release=False` |
| `num` 恒写 | `num=True` |
| `title` / `originaltitle` / `sorttitle` 三者同值 | 三者均 `True` |
| `mpaa` 恒为 `JP-18+` | `mpaa=True`，`mpaa_value="JP-18+"` |
| `rating` 与 `criticrating`（score×10）同时写 | `score=True` `criticrating=True` |
| 无 `votes` | `votes=False` |
| 无 `country` / `customrating` | 均 `False` |
| `series` 与 `set/name` 同写 `metadata.series` | `series=True` `set=True` |
| `studio` 与 `maker` 同写 `metadata.studio` | 两者均 `True` |
| `publisher` 与 `label` 同写 `metadata.publisher` | 两者均 `True` |
| `tag` 与 `genre` 同写 `metadata.tags` | 两者均 `True` |
| `poster` / `cover` / `trailer` 按 URL 非空写 | 三者均 `True` |
| `director` 按 `metadata.directors` 写 | `director=True` |
| `{site}id` 按 `metadata.external_ids` 写 | `external_ids=True` |
| 无 `website` | `website=False` |
| 无 `tagline` | `tagline_format=None` |
| 无 `set` 的其它来源 | `set_by_*` 三者均 `False` |

**改动后必须用现有 NFO 测试用例验证输出逐字节不变**（默认值下的回归）。

`additional_tags` 支持的附加内容（对齐 MDC）：
`番号前缀` / `演员` / `分辨率` / `中文字幕` / `有码/无码` / `系列` / `片商` / `发行商`。

#### 3.4.4 NFO 单独重写

新增端点：

```
PUT /api/metadata/{id}/nfo
```

用当前生效配置重写该条目的 NFO 文件。
需要定位 NFO 文件路径 —— 现有 `organize` 落盘时未记录 NFO 路径。
方案：`MediaFile` 增加 `nfo_path` 列，或从 `resolve_paths` 重算。
建议**重算**（`resolve_paths` 是确定性的），避免新增列。
但若模板被改过，重算得到的是**新**路径 —— 这正是「单独重写」的语义。

### 3.5 章节划分与文档

| 内容 | 落点 |
|---|---|
| 配置覆盖的合成语义（四层优先级、稀疏含义、显式设置标记） | `docs/dev/config.md` |
| 模板引擎双实现与语法差异 | `docs/dev/organize.md`（新建） |
| 识别词规则的匹配优先级 | `docs/dev/parsing.md`（新建） |
| 原文双轨字段的来源与翻译前留存规则 | `docs/dev/data-model.md` |
| 用户可见的命名与 NFO 行为 | `docs/user/organize.md` |
| `<outline>` 承载原文简介的语义变化 | `docs/user/media-server.md` |

`docs/dev/index.md` 需登记新建的两个文档。

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| 覆盖 payload 校验失败 | API 返回 422 + Pydantic 错误详情；不落库 |
| 模板语法错误（保存时） | API 返回 422 + 错误位置；不落库 |
| 模板渲染失败（落盘时） | 该文件整理失败，任务记 `failed`，错误信息含模板名与位置；不影响其它文件 |
| Jinja2 渲染产生越界路径 | `PathEngine.resolve` 抛 `ValueError` → 同上，失败而非逃逸 |
| Jinja2 模板访问被禁属性 | `SandboxedEnvironment` 抛 `SecurityError` → 渲染失败，错误信息明确 |
| 覆写时目标不在库内 | 拒绝覆写，记冲突 |
| 自定义识别词正则非法 | 保存时校验（`re.compile`），非法返回 422 |
| 识别词命中冲突（同 kind 同 type 多条） | 按 `created_at` 升序，首条命中生效（与 MDC「按顺序匹配，首条命中生效」一致） |
| 存量 `raw` 快照缺新字段 | `MediaMetadata` 新字段**必须有默认值**（`None`），否则 `MediaMetadata(**cached)` 抛 `TypeError` 会作废整站快照（`aggregate/engine.py:514`） |
| 原文字段与译文字段冲突 | 翻译层只在 `source_language` 为非中文时写原文字段；中文源不写，避免把中文译文当原文 |
| `library.template_overrides` 与实际模板字段不一致 | 以 `template_overrides` 名单为准；名单外的模板字段视为继承 |

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| `ConfigResolver.resolve` | 表测试：全局/覆盖/任务三层组合、嵌套 dict 深合并、非 dict 替换、空覆盖 |
| `template_overrides` 生效判定 | 表测试：名单内字段生效、名单外落到覆盖表/全局、既有库（名单空）行为不变 |
| `ParseRuleSet` 匹配 | 表测试：番号优先于路径、路径含目录、大小写、正则 `r:` 前缀、非法正则拒绝、首条命中 |
| `TemplateEngine` 扩展 | 表测试：条件块真/假、filter 链、嵌套 default、无值判定、空组、未知 filter 报错 |
| `JinjaTemplateBackend` | 表测试：与自研引擎的等价用例、`{{ }}` 与 `{% %}`、Sandbox 拒绝属性访问、undefined 变量 |
| 两引擎一致性 | 表测试：同一组变量，两个引擎对**共有语义**产出一致（无值语义差异除外） |
| `resolve_paths` + inplace | 表测试：inplace 时视频路径 = 源路径、图片落在源目录 |
| 覆写策略 | 表测试：默认跳过不覆盖、开启后覆写、目标非视频拒绝覆写、跨库拒绝 |
| `write_nfo` + NfoOptions | 表测试：每个开关的开/关对输出 XML 的影响、默认值与旧行为一致 |
| 原文字段留存 | 表测试：`source_language="ja"` 时原文写入、`"zh_cn"` 时不写、`None` 时不写；翻译后 `title`/`plot` 不受影响 |
| `original_title` / `original_plot` 聚合 | 表测试：字段链取首个非空、`field_sources` 记录来源 |
| `website` 取值 | 表测试：取字段链首个非空 `source_url`、`source_urls` 全量保留 |
| avsox 多语言修复 | 表测试：声明 `MULTI_LANGUAGE` 后 `_lang` 生效、`title_ja`/`description_ja` 可取到、`_first` 顺序修正 |
| `MediaMetadata` 新增字段 | 回归：存量 `raw` 快照仍能反序列化（不抛 `TypeError`） |
| API 端点 | 覆盖 CRUD、422 路径、删除后回到继承 |

**回归重点**：

1. 既有库的落盘结果不变 —— 用现有测试套件验证，
   并补一条「老库（`template_overrides` 为空）落到全局默认且落盘结果不变」的用例。
2. 存量 `raw` 快照兼容 —— 补一条用旧格式 `raw` 反序列化的用例。
3. NFO 默认输出逐字节不变 —— 用现有 NFO 测试验证（`<originaltitle>` / `<outline>` 的回退路径）。

## 6. 实施顺序（供实现计划参考）

按「先改现有行为、后加新能力」的顺序，每步都可独立验证：

1. **avsox 两处修复** + `MediaMetadata` 加 `original_title` / `original_plot` /
   `source_language` 字段（含默认值）
2. **`Metadata` 加四列** + `MetadataField` 加三枚举值 + `SCALAR_FIELDS` +
   `MetadataFields` TypedDict + `update_metadata` 显式赋值 + Alembic revision
3. **各源填原文字段**（DMM / MGStage / official / wp_works / avsox / r18dev 等）
4. **翻译层原文留存**（`handlers/scrape.py::_translate_metadata` 按 `source_language` 判定）
5. **`website` 取值与落库**
6. **`NfoOptions` 模型 + `write_nfo` 改造**（默认值对齐现有输出 + 原文/译文分离）
7. **`ConfigResolver` + `config_overrides` 表 + `Library.template_overrides` 列 + Alembic**
8. **`ParseRuleSet` + `parse_rules` 表 + `parse_file_info` 扩展参数**
9. **模板引擎扩展**（自研语法）+ `TemplateBackend` 抽象
10. **`JinjaTemplateBackend` + Jinja2 主依赖 + 语法标识**
11. **`MoveMode.INPLACE` + 落盘分支**
12. **覆写策略**
13. **试算端点 + 前端试算面板**
14. **覆盖 API + 前端覆盖编辑 UI**
15. **识别词管理 API + 前端**
16. **原文字段的前端展示**（详情页 + i18n）

## 7. 待确认

| # | 问题 | 状态 |
|---|---|---|
| 1 | Jinja2 依赖策略 | **已定案**：主依赖 |
| 2 | NFO 默认字段集 | **已定案**：以现有 `write_nfo` 实际输出为准（见 3.4.3 的逐字段对照表） |
| 3 | 覆盖表与 `Library` 行的优先级 | **已定案**：覆盖表优先；引入 `Library.template_overrides` 显式设置标记以保持既有库行为不变 |
| 4 | `parse_rules` 表设计 | **已定案**：独立表 |
| 5 | `original_plot` 语义 | **已定案**：源站原文（日文） |
| 6 | `website` 设计 | **已定案**：单值列 + `source_urls` 保留全量 |

### 实现时需注意的既有缺陷（本阶段顺带修复）

1. **NFO 的 `<originaltitle>` 现写的是中文标题同值**，不是原标题 ——
   加了 `original_title` 后改为写真实原标题，为空时回退。
2. **NFO 的 `<outline>` 现写的是剧情同值**，不是另一份简介 ——
   加了 `original_plot` 后改为写原文简介，为空时回退。
3. **avsox 未声明 `MULTI_LANGUAGE`**，导致 `title_ja` / `description_ja` 是死代码。
4. **avsox 的 `_first` 把 `description_cn` 排在 `description_ja` 之前**。

### 已知限制（明确不做）

1. **Javdb / Javbus / iqqtv 不提供原标题** —— 需改 locale 或请求头，
   会影响既有抓取行为，本阶段不动。这些源的 `original_title` 留空。
2. **Javdb / Javbus / Javlibrary / Jav321 / freejavbt 不解析 plot** ——
   这些源不提供原文简介。
3. **`original_plot` 无数据来源的条目** —— 存量数据未重新刮削前为空，
   NFO 回退写译文，与现状一致。
4. **r18dev 无 `source_url`** —— 离线镜像无网页，其 `website` 为空。
