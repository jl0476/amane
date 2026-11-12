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

## 0.1 评审意见处理记录（第 2 版）

第 1 版经独立评审提出 4 项 P1、6 项 P2。逐项核实后**全部成立**，已按下表修订：

| 级别 | 问题 | 处理 | 落点 |
|---|---|---|---|
| P1 | 视频覆写无失败恢复协议 | 改为**备份 + 原子替换 + 回滚**协议，四种整理模式分别规定；补磁盘不足/权限失败/中断恢复 | 3.4.2 |
| P1 | 覆写后残留旧索引元数据 | 定义覆写后的索引**接管**规则（字段级表），独立于 `commit_organized_media_file` | 3.4.2 |
| P1 | 历史库迁移不保语义 | 改为**迁移时保留全部历史生效值**（不再「与全局默认比较」）；新建库才用继承语义 | 3.1.4 |
| P1 | 单个 `source_language` 判断不了聚合字段语言 | 改为**按字段**记录语言（`field_languages`），原文在**聚合阶段**按各自字段链确定 | 3.3.4 |
| P2 | 依赖未定义的全局模型 | 补齐 4 个 section 模型的完整字段定义 + 与 `Library` 行的合成关系表 | 3.1.2 |
| P2 | 数据传递链不完整 | 补 `AggregatedMetadata` 三属性；`WEBSITE` 不进 `SCALAR_FIELDS`，改为从主源 `source_url` 派生 | 3.3.4 |
| P2 | 语法切换只验「解析成功」 | 改为**渲染探针**（查占位符残留）+ 两套默认模板 + 继承/回退模板全参与验证 | 3.2.3 |
| P2 | Jinja filter 语义不符 | `default` / `truncate` **自建兼容实现**，不用原生；逐项列出引擎差异 | 3.2.2 |
| P2 | NFO 默认值与行为冲突 | 统一为**唯一的输出规则表**；`<website>` 默认 `False`；`<originaltitle>` 默认回退；Kodi `plot`/`outline` 语义写反而**已更正** | 3.4.3 |
| P2 | 单独重写 NFO 可能无法匹配视频 | 改为**以实际视频路径为基准**、只重算附属文件；跨目录返回 422、无本地文件返回 409 | 3.4.4 |

另修正一处评审未提及的**规格自身错误**：Kodi 官方语义中
`<plot>` 是详细剧情、`<outline>` 是简短摘要 —— 第 1 版写反了。
本版按正确语义设计，但**默认不改** `<outline>` 的行为（`outline_summary` 可选开关），
以保证存量条目 NFO 输出逐字节不变。

相关修订已同步纳入 **§1 成功标准**（新增数据安全与兼容性标准）与 **§5 测试策略**。

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

**数据安全标准**（审核 P1-1 / P1-2 引入）：

10. **覆写是事务性的**：任一步失败（磁盘不足、权限、进程中断）后，
    磁盘上仍有可用的视频；备份可自动恢复。
11. **覆写后的索引归属正确**：目标视频被源替换后，
    `metadata_id` / `number` / `oshash` 与文件相位字段均按新内容重算，
    不残留旧条目的关联。
12. **既有库升级后行为不变**：迁移保留全部历史生效值；
    升级后再修改全局默认，既有库的落盘结果仍不变。

**兼容性标准**（审核 P2 引入）：

13. **语法切换必须做语义验证**：不能只验「解析成功」
    （Jinja2 会把自研占位符当普通文本，解析成功但输出字面量）。
    切换时验证全部生效模板，含继承与回退模板。
14. **两套引擎的 filter 语义一致**：`default` / `truncate` 用自建兼容实现，
    不依赖 Jinja2 原生行为。
15. **NFO 默认输出逐字节不变**：存量条目在默认配置下写出的 XML 与升级前一致。

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

#### 3.1.2 新增配置 section 模型

现状核实：`config/manager.py::HotSettings` 只有 13 个 section
（`scraping` / `media_server` / `actor_scraping` / `agent` / `network` / `sr` /
`semantic` / `watermark` / `llm` / `r18` / `watcher` / `worker` / `logging` / `plugins`），
**没有** `common` / `naming` / `nfo` / `parser`。

本阶段新增 4 个 section 模型（全部加进 `HotSettings`，
`model_config = {"extra": "forbid"}` 下未知字段会被拒绝）：

```python
class OrganizeCommonConfig(BaseModel):
    """整理目录与文件处理."""
    target_folder: str | None = None
    target_folder_metadata: str | None = None
    delete_metadata_folder_when_failed: bool = False
    target_overwrite_video: bool = False
    target_overwrite_pic: bool = False
    delete_empty_parent_after_move: bool = False
    file_size_filter: int = 0              # 字节; 0 = 关闭
    file_ext_filter: list[str] = []
    file_name_blacklist: list[str] = []
    file_sanitize_list: list[str] = []
    uncensored_hack_keywords: list[str] = []
    protect_ext_whitelist: bool = False
    extra_ext_whitelist: list[str] = []
    delete_by_size: bool = False
    delete_by_ext: bool = False
    delete_by_name: bool = False


class NamingConfig(BaseModel):
    """命名模板与语法."""
    syntax: Literal["native", "jinja"] = "native"
    video_template: str = VIDEO_TEMPLATE_DEFAULT
    link_template: str | None = None
    link_mode: LinkMode = LinkMode.STRM
    strm_content_template: str | None = None
    thumb_template: str | None = None
    poster_template: str | None = None
    fanart_template: str | None = None
    extrafanart_template: str | None = None
    extrafanart_name_template: str | None = None
    nfo_template: str | None = None
    trailer_template: str | None = None
    subtitle_template: str | None = None
    subtitle_extensions: list[str] = DEFAULT_SUBTITLE_EXTENSIONS
    max_folder_length: int = 0
    max_actor_count: int = 3
    category_mapping: dict[str, str] = {}
    category_rules: list[CategoryRule] = []


class NfoConfig(BaseModel):
    """NFO 字段开关. 字段清单见 3.4.3."""
    ...  # NfoOptions 的全部字段


class ParserConfig(BaseModel):
    """番号识别的用户自定义规则."""
    number_keywords: dict[ContentType, list[str]] = {}
    path_keywords: dict[ContentType, list[str]] = {}
```

**与 `Library` 行字段的合成关系**（审核 P2-5 要求说明）：

| section | 与 `Library` 行的关系 |
|---|---|
| `naming` | `Library` 行的模板字段是 `naming` 的**部分字段**，按 3.1.4 的 `template_overrides` 名单参与合成 |
| `common` | `Library` 行的 `move_mode` / `min_file_size` / `blacklist_patterns` / `trailer_pattern` **恒定生效**，不被 `common` 覆盖（它们不是模板类，是显式库级设置）。`common` 里的同名概念（`file_size_filter` 等）是**全局默认**，库行未设时用全局 |
| `nfo` | `Library.write_nfo` 是 `nfo.enabled` 的库级对应项，按 `template_overrides` 参与合成 |
| `parser` | **库级独立** —— 不同库可以有不同识别规则；`Library` 行无对应字段，规则只存 `config_overrides` 表 |

**注意**：`naming` 里的 `video_template` 等字段与 `Library` 行同名。
`ConfigResolver` 的合成输入必须包含 `Library` 行的相关字段，
否则合成结果不完整 —— 详见 3.1.4 的四层顺序。

#### 3.1.3 生效配置解析

新增 `src/amane/config/overrides.py`：

```python
class ConfigResolver:
    """把全局配置与条目覆盖合成生效配置."""

    def resolve(
        self,
        hot: HotSettings,
        section: str,
        scope: Scope | None = None,
        library: Library | None = None,
        payload: dict | None = None,
    ) -> Any:
        """返回该 section 的生效模型."""
```

合成规则：

1. 取 `hot` 中该 section 的模型实例（全局基线）。
2. 若有 `library`：把该库在 `template_overrides` 名单内的**模板类**字段
   覆盖到基线上（见 3.1.4 的字段分类表）。
3. 若有 `scope`：取其覆盖 payload；对 payload 中**显式出现的键**逐个覆盖。
4. 若有 `payload`（任务级），同上，优先级最高。
5. 嵌套结构：payload 的值若是 dict，与当前值**逐键深合并**；
   非 dict 值整体替换。

**字段分类表**（决定 `Library` 行哪些字段参与合成）：

| 类别 | 字段 | 是否参与合成 |
|---|---|---|
| 模板类 | `video_template` / `link_template` / `link_mode` / `strm_content_template` / `thumb_template` / `poster_template` / `fanart_template` / `extrafanart_template` / `extrafanart_name_template` / `nfo_template` / `trailer_template` / `subtitle_template` / `subtitle_extensions` / `write_nfo` / `copy_resources` | ✅ 仅当在 `template_overrides` 中 |
| 非模板类 | `move_mode` / `min_file_size` / `blacklist_patterns` / `trailer_pattern` | ❌ 恒定生效 |

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

**但必须保证既有库行为不变**。关键事实（已核实）：
`db/repos/libraries.py::create_library` 的模板参数默认值是**模块常量**
（`video_template: str = VIDEO_TEMPLATE_DEFAULT`），**不是**「复制当前全局配置」。
也就是说库行里存的是「建库那一刻的代码默认值」，
既无法区分「用户显式设过」与「继承来的默认值」，
也无法通过「与当前全局默认比较」还原用户意图 ——
用户当年显式设成 `VIDEO_TEMPLATE_DEFAULT` 的库，会被误判为「未设置」。

因此引入**「显式设置」标记**，且**迁移时保留全部历史生效值**：

- `Library` 表新增一列 `template_overrides: list[str]`，记录哪些模板字段是**显式生效**的。
- **Alembic 数据迁移**：把每个既有库的**全部**模板类字段
  （`video_template` / `link_template` / `link_mode` / `strm_content_template` /
  `thumb_template` / `poster_template` / `fanart_template` / `extrafanart_template` /
  `extrafanart_name_template` / `nfo_template` / `trailer_template` / `subtitle_template` /
  `subtitle_extensions` / `write_nfo` / `copy_resources`）
  **一律写入 `template_overrides`**。
  理由：既有库的行值就是它当前的实际生效值，全部保留即可保证行为**逐字节不变**。
  这比「与全局默认比较」更保守也更正确 —— 不猜用户意图，只保留现状。
- **新建库**时该列为**空** —— 全部模板字段视为继承（覆盖表 → 全局）。
  这是新语义，与既有库不同：新建库跟随全局默认，改全局设置时新库受影响。
- 用户在库表单里改动某个模板字段时，把该字段名加入此列。
- 生效时：`Library` 行的某个模板字段，**只有在其名字出现在 `template_overrides` 中**时
  才参与合成；否则落到覆盖表 / 全局。

合成顺序（覆盖表优先，但库行在其未被标记时让位）：

```
1. 全局 HotSettings
2. config_overrides 表（该 scope 的 section）
3. Library 行字段 —— 仅当字段名在 template_overrides 中；否则跳过
4. 任务 payload
```

**这样既满足「覆盖表优先」的决策，又保证既有库升级后行为不变**：
既有库的模板字段全在 `template_overrides` 中 → 库行参与合成 → 与升级前一致；
新建库的该列为空 → 覆盖表优先于全局 → 符合决策意图。

**验收要求**：迁移后，既有库的落盘结果必须与迁移前**逐字节一致**。
测试必须覆盖「升级后再修改全局默认，既有库的落盘结果不变」这一场景。

**例外**：`move_mode`（整理模式）、`min_file_size`、`blacklist_patterns`、
`trailer_pattern` 等**非模板类**字段不参与这套机制 ——
它们本来就是显式的库级设置，语义清晰，保留现状（恒定生效，不被覆盖表覆盖）。

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

**「有值」的判定**（自研引擎，沿用 Amane 现状）：

```
None / 空字符串 / 空列表 / 字符串 "未知" / 字符串 "None"  →  无值
其它 → 有值
```

**Jinja2 引擎的语义与自研引擎不同，必须逐项说明**（审核 P2-8）：

| 能力 | 自研引擎 | Jinja2 原生 | 处理 |
|---|---|---|---|
| 变量缺失 | 无值（按上述定义） | `Undefined` | 自定义 filter 统一处理 |
| `default(x)` | 无值时用 `x`（无值含空串、空列表、「未知」） | **只对 undefined 生效**，空字符串会原样返回 | **不用原生 `default`**，自建 `default` filter，语义对齐自研引擎 |
| `truncate(n)` | 截断到 n **UTF-8 字节**，无省略号 | 截断到 n **字符**，带 `...` 省略号，有 `leeway` 容差 | **不用原生 `truncate`**，自建 `truncate` filter，按字节截断且不加省略号 |
| `upper` / `lower` | 大小写转换 | 同 | 一致，直接用原生 |
| `trim` | 去首尾空格 | 同 | 一致，直接用原生 |
| `replace(a, b)` | 字符串替换 | 同 | 一致，直接用原生 |
| `split` / `first` / `last` | 自研实现 | Jinja2 无 `split` filter | 自建 `split`，配合 `first`/`last`（Jinja2 的 `first`/`last` 是取序列首末，可直接用） |

结论：**自研引擎与 Jinja2 引擎共用同一套「兼容 filter」**，
通过在 `SandboxedEnvironment` 上注册自定义 filter 实现。
这样「两引擎一致性」的承诺才成立 —— 一致性测试才有意义。

**不一致之处仅一处**：Jinja2 引擎下变量缺失时是空字符串（Undefined），
自研引擎下是「无值」（含「未知」字符串的判定）。
这与 MDC 的 Jinja2 行为一致，属**有意差异**，一致性测试排除此项。

**表达式语法限制**：自研引擎不做任意表达式求值。
`{% if %}` 的条件只能是「变量名」或「变量名列表」，不含运算符、不含函数调用。
理由：路径模板由用户在 Web UI 里编辑，需要**可静态分析**（前端高亮、校验、试算），
且要杜绝求值注入。

Jinja2 引擎有完整表达式能力，但受 `SandboxedEnvironment` 限制
（禁属性访问、禁导入、禁任意调用）。

#### 3.2.3 模板来源与语法标识

模板需要记录用的是哪套语法：

- 新增 `Library` 列 `template_syntax: str`（默认 `"native"`），取值 `native` | `jinja`。
- 覆盖表的 `naming` section 里对应字段同理。
- 前端模板编辑器提供语法切换。

**切换校验不能只看「解析成功」**（审核 P2-7）：

Jinja2 会把自研语法的 `{number}` 当**普通文本**解析 —— 解析**成功**，
但渲染结果是字面量 `{number}`。因此「能解析」完全不能证明模板有效。

切换时必须做**语义验证**：

1. 用目标引擎解析全部模板（video / link / strm_content / 8 个附属模板 /
   继承链上所有生效模板）。
2. 对每个模板做**渲染探针**：用一组**已知的样例变量**渲染，
   检查输出中**不含未替换的占位符残留**。
   - 自研语法：残留 `{` + 标识符 + `}` 形态。
   - Jinja2：残留 `{{` 或 `{%` 形态（也应检查字面量 `{number}` 这类自研占位符残留）。
3. 任一模板未通过 → **拒绝切换**，返回 422 + 具体是哪个模板、哪处残留。

**两套默认模板**（审核 P2-7 要求）：

现有默认模板全部用自研占位符（`{studio}/{number}/{number}[-CD{cd?}][-{sub?}].{ext}`）。
Jinja2 引擎需要**自己的一套默认模板**（同结构，语法不同）：

```
自研默认（现有，保持不变）：
  {studio}/{number}/{number}[-CD{cd?}][-{sub?}].{ext}

Jinja2 默认（新增）：
  {{ studio }}/{{ number }}/{{ number }}{% if cd %}-CD{{ cd }}{% endif %}{% if sub %}-{{ sub }}{% endif %}.{{ ext }}
```

两套默认值都由 `path-template-schema` 端点按 syntax 分别下发，
前端切换语法时自动替换为对应默认（若用户未自定义过该模板）。

**继承与回退模板也要验证**（审核 P2-7）：
`template_overrides` 名单内的库级模板、覆盖表里的模板、
以及最终落到全局默认的模板，**全部**参与语法验证 ——
不能只验证当前库这一层的模板。

`/api/libraries/path-template-schema` 响应新增 `syntaxes` 字段，
列出可用语法、各自的默认模板、占位符与能力。

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

**语言判定必须按字段，不能全局一个值**：

`aggregate/engine.py::_resolve_scalars` 是**按字段独立**沿链取值的 ——
标题可能来自 DMM（日文），剧情可能来自 airav（中文）。
若用一个全局 `source_language`，会出现「标题日文、剧情中文」时
把中文剧情写进 `original_plot` 的错误。

因此**按字段记录来源语言**：

- `MediaMetadata` 新增 `field_languages: dict[str, str] | None = None`
  （**逐字段**的语言声明，如 `{"title": "ja", "plot": "en"}`）。
  爬虫用它在同一个源内表达「标题是日文、剧情是英文」这类混合情况。
- 聚合层为每个字段记录它实际取自哪个源（`field_sources` 已有），
  再据此取该源对该字段的语言声明，写入 `AggregatedMetadata.field_languages`。
- **不引入源级的全局语言标记** —— 它无法表达按字段的语言，
  在混合来源下会误判（审核 P1-4 的核心问题）。
  源若整体只有一种语言，也照样逐字段声明。

**原文在聚合阶段确定，而非翻译阶段**：

新增两个**独立于 `title`/`plot` 的字段**参与聚合：
`original_title` / `original_plot`。它们各自有自己的字段链
（由 `field_priority` / `content_routes` 编译得出）。

各源在返回时同时给出两侧的值：

- DMM / MGStage / official / wp_works / javlibrary / jav321 / xcity / giga / prestige：
  `original_title = title`（这些源的 title 本身就是原文）。
- avsox：`original_title = movie.title_ja`，`original_plot = movie.description_ja`；
  `title` / `plot` 仍按请求 `language` 取对应语言。
- r18dev：`original_title = v.title_ja`，`original_plot = v.comment_ja`。
- Javdb / Javbus / iqqtv：**本阶段不改**（需改 locale/请求头，会影响既有抓取行为）。
  它们不提供原标题，`original_title` 留空。

这样**标题的原文与剧情的原文各自由各自的字段链决定**，
不存在「一个源同时供两个字段」的语言歧义。

**翻译层**：

- `handlers/scrape.py::_translate_metadata` 当前就地覆盖 `title` / `plot`。
  改为：**只翻译 `title` / `plot`，不碰 `original_title` / `original_plot`**。
- 翻译前**不再**用「翻译前的快照」兜底填原文字段 ——
  那个做法在混合来源下会出错（审核 P1-4 的核心问题）。
  原文字段为空就留空。
- 翻译**失败**时：`title` / `plot` 保持源站原值不变（现有行为），
  `original_*` 独立存在 —— 两者都是原文，不冲突。

**未声明语言的源**：不写原文字段。宁可留空，不可猜。

**字段语言声明的落库**：`Metadata` 需要能回答「这条的原文标题来自哪个源、什么语言」。
落库字段：

- `original_title` / `original_plot`（文本）
- `website`（URL）
- `field_languages: dict[str, str]`（字段 → 语言，如 `{"original_title": "ja", "original_plot": "ja"}`）

**不落全局 `source_language`** —— 审核指出它无法表达按字段的语言。
改为落 `field_languages` 字典，与 `field_sources` 平行
（`field_sources` 记「来自哪个站」，`field_languages` 记「什么语言」）。

已有 `crawlers/models.py::FetchOptions.language` 与
`crawlers/site_roles.py::MULTI_LANGUAGE_SITES` 是**请求语言**，不是响应语言，
不能替代本设计。新增 `field_languages` 表达**响应语言**。

**聚合层**：

- `enums.py::MetadataField` 新增 `ORIGINAL_TITLE` / `ORIGINAL_PLOT` / `WEBSITE`
  三个枚举值（**值必须等于 `MediaMetadata` 属性名**，
  `tests/crawlers/test_enum_consistency.py:37-40` 硬断言）。
- `aggregate/engine.py::SCALAR_FIELDS` 加入 `ORIGINAL_TITLE` / `ORIGINAL_PLOT`。
  `WEBSITE` **不进 `SCALAR_FIELDS`** —— 它没有同名的爬虫字段（见下方 P2-6 的修正）。
- `aggregate/models.py::AggregatedMetadata` 加三个属性
  （**审核 P2-6 指出此项被遗漏**）：
  `original_title: str | None` / `original_plot: str | None` / `website: str | None`。
- `aggregate/engine.py::_normalize_source_text` 与
  `db/repos/metadata.py::_normalize_text_fields` 需为 `original_plot` 加长文本归一
  （当前只归一 `plot`）。
- `field_sources` 会自动记录新字段的来源（`_fill_scalar`）。
- `field_languages` 随 `_fill_scalar` 一并记录对应字段的语言。

**`WEBSITE` 的取值方式（修正 P2-6）**：

爬虫侧**没有** `website` 字段，只有 `source_url`（各站的详情页 URL）。
因此 `WEBSITE` 不走通用标量聚合，而是**专门派生**：

- `MetadataField.WEBSITE` **不加入 `SCALAR_FIELDS`**，
  也不要求 `MediaMetadata` 有同名属性（否则会破坏
  `tests/crawlers/test_enum_consistency.py` 的断言）。
- 取值规则：沿 `title` 的字段链（即主源）取第一个非空 `source_url`。
  即「主源的详情页 URL」。
  实现：在 `_resolve_scalars` 之后增加一步 ——
  取 `state.result.field_sources[TITLE]` 对应的 `state.fetched` 条目，
  从其 `source_url` 派生 `website`。
- `source_urls` 全量字典保持不变。

**DB 层**：

- `Metadata` 加四列：`original_title` / `original_plot` / `website` /
  `field_languages`（JSON 字典）。
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

##### 覆写的事务性协议

覆写**不能**是「先删目标、再放源」。必须保证任一步失败时磁盘上仍有可用视频。

统一采用**备份 + 原子替换**：

```
1. 确认目标存在、是视频文件、在库内、且不同于源
2. 把目标重命名为同目录下的备份名：<目标名>.amane-overwrite-<任务id>
   （同目录 → 同一文件系统 → rename 是原子的）
3. 按整理模式落位源文件：
   - move    : os.replace(源, 目标)
   - copy    : 复制到临时名 <目标名>.amane-tmp-<任务id>，再 os.replace 到目标
   - hardlink: os.link(源, 临时名)，再 os.replace 到目标
   - symlink : 在临时名建符号链接，再 os.replace 到目标
4. 成功后删除备份
5. 任一步失败：回滚 —— 删除已落位的临时/目标，恢复备份到原目标名
```

约束：

- 备份名与临时名带任务 id，**跨任务不冲突**，便于事后清理。
- 备份与临时文件必须在**同一目录**（rename 原子性要求，且不跨文件系统）。
- **磁盘不足**（copy 模式）发生在写临时文件阶段 → 目标未被触碰，备份直接还原。
- **权限失败** 发生在 rename/replace 阶段 → 同上。
- **进程中断** 的恢复：备份文件残留在目录中。下次整理同一目标时，
  若发现 `<目标名>.amane-overwrite-*` 备份存在且目标缺失，视为上次中断，
  **恢复备份**而非视为冲突跳过。恢复动作记入日志。
- 覆写**永不**跨越库边界（`safe_dirs` 检查照常生效）。
- 覆写决策记入任务结果（新增计数 `overwritten`），并在日志中留痕。
- 覆写**不涉及**字幕与图片的独立备份 —— 图片是覆盖写（内容可重新下载），
  字幕是同名覆盖，风险低于视频。

##### 覆写后的索引归属

现有 `handlers/file.py::commit_organized_media_file` 在目标路径已有索引时，
只**补充空字段**（`metadata_id` / `oshash` / `number`），再删除源索引。
这在「跳过覆写」语义下是对的（目标是别的条目，不该被源改写）。

但在「覆写」语义下，**目标视频已被源替换**，目标索引必须整体归属给源条目：

| 字段 | 覆写时的处理 |
|---|---|
| `path` | 保持目标路径 |
| `metadata_id` | **覆盖**为源的 `metadata_id`（不是「仅当为空」） |
| `number` | **覆盖**为源的 `number` |
| `oshash` | **重算**（目标内容已变，源 oshash 是旧内容） |
| `status` | 取源的状态 |
| `size` / `duration` / `codec` | **重算**或置空待刷新 |
| `mosaic` / `definition` / `has_subtitle_in_name` | 按新文件路径重新派生 |
| `library_id` | 不变（目标在本库内） |

因此覆写路径**不能复用**现有的 `commit_organized_media_file` 分支，
需要一条独立的「接管目标索引」路径：
删掉目标旧索引行 → 把源索引行的 path 改为目标路径 → 按上表更新字段 → 落库。

**不同条目覆写同一路径**的规则：
以**源条目**为准，目标旧条目的索引被替换（源成为该路径的唯一所有者）。
目标旧条目若还被其它路径引用，其 `metadata_id` 关系不受影响（元数据是按番号存的，
不随文件走）。但目标旧条目的这条 `MediaFile` 行会消失 ——
需确认它没有关联的字幕索引（`has_external_subtitle` 是派生属性，随行删除）。

#### 3.4.3 NFO 字段开关

新增配置模型 `NfoOptions`（独立 `nfo` section）：

```
enabled: bool = True
# 标题类
title: bool = True
originaltitle: bool = True
sorttitle: bool = True
# 剧情类
plot: bool = True
outline: bool = True
outline_summary: bool = False   # True 时 <outline> 取摘要, False 时与 <plot> 同值
originalplot: bool = False
# 人物类
actor: bool = True
director: bool = True
# 日期类
premiered: bool = True
releasedate: bool = True
year: bool = True
# 其它
num: bool = True
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

##### Kodi 字段语义（修正审核指出的颠倒）

Kodi 官方 NFO 规范里：

- `<plot>` —— **详细剧情**，可多行、可很长。
- `<outline>` —— **简短摘要**，一行。

**原规格把两者写反了**（写成「`<outline>` 通常是详细剧情，`<plot>` 是一句话简介」）。
按正确语义修正设计：

| 元素 | Kodi 语义 | 内容 |
|---|---|---|
| `<plot>` | 详细剧情 | `metadata.plot`（译文） |
| `<outline>` | 简短摘要 | `metadata.plot` 的**首句/前 N 字**（译文），非原文 |
| `<originaltitle>` | 原标题 | `metadata.original_title` |

**因此原文简介不进 `<outline>`** —— `<outline>` 是摘要位，塞长篇日文原文是错的。

原文简介改放 `<originalplot>`（**Kodi 非标准字段**，
Emby/Jellyfin 会忽略，但保留信息不丢）：

- `originalplot` 开关默认 `False` —— **默认不写**，避免给非标准字段引入意外行为。
- 开启时写 `metadata.original_plot`（原文简介），为空则不写该元素。

**这也解开了「默认输出逐字节不变」与「默认采用真实原文」的冲突**：
默认配置下 `<originaltitle>` 仍写**中文标题同值**（与现状一致），
只有显式开启 `originaltitle` 才写真实原标题。

##### 唯一的输出规则表

`write_nfo` 的输出由**这张表**唯一决定（不再有其它描述）：

| 元素 | 开关 | 取值 | 为空时 |
|---|---|---|---|
| `<title>` | `title` | `_title_with_number(number, title)` | 不写该元素 |
| `<originaltitle>` | `originaltitle` | `original_title`；**默认回退**到 `<title>` 的值 | 回退写 `<title>` 值 |
| `<sorttitle>` | `sorttitle` | 同 `<title>` | 不写 |
| `<plot>` | `plot` | `plot` | 不写 |
| `<outline>` | `outline` | `plot` 的**同值**（默认）；`outline_summary=True` 时取摘要 | 不写 |
| `<originalplot>` | `originalplot` | `original_plot` | **不写该元素** |
| `<actor>` | `actor` | 每个演员一组 `<name>`/`<type>` | 不写 |
| `<director>` | `director` | `directors` | 不写 |
| `<premiered>` | `premiered` | `release` | 不写 |
| `<releasedate>` | `releasedate` | `release` | 不写 |
| `<year>` | `year` | `release` 的前 4 位 | 不写 |
| `<num>` | `num` | `number` | 不写 |
| `<runtime>` | `runtime` | `runtime` | 不写 |
| `<rating>` | `score` | `score` | 不写 |
| `<criticrating>` | `criticrating` | `int(score * 10)` | 不写 |
| `<votes>` | `votes` | 点赞数 | 不写（**当前无数据来源**） |
| `<country>` | `country` | 国家码 | 不写（**当前无数据来源**） |
| `<mpaa>` | `mpaa` | `mpaa_value` | 不写 |
| `<customrating>` | `customrating` | 自定义分级 | 不写（**当前无数据来源**） |
| `<series>` | `series` | `series` | 不写 |
| `<set><name>` | `set` | `series`（或按 `set_by_*` 改用演员/番号前缀/系列） | 不写 |
| `<tag>` | `tag` | 每个标签一个 | 不写 |
| `<genre>` | `genre` | 每个标签一个 | 不写 |
| `<studio>` | `studio` | `studio` | 不写 |
| `<maker>` | `maker` | `studio`（同值） | 不写 |
| `<publisher>` | `publisher` | `publisher` | 不写 |
| `<label>` | `label` | `publisher`（同值） | 不写 |
| `<poster>` | `poster` | `poster_url` | 不写 |
| `<cover>` | `cover` | `thumb_url` | 不写 |
| `<trailer>` | `trailer` | `trailer_url` | 不写 |
| `<website>` | `website` | `website` | 不写 |
| `<{site}id>` | `external_ids` | `external_ids` 各项 | 不写 |

**`<outline>` 的摘要规则**（仅 `outline_summary=True` 时生效）：
取 `plot` 的第一个句号/问号/叹号/换行之前的内容；
若无这些分隔符，取前 120 字符（按 UTF-8 字节，不切断多字节字符）。
摘要为空时 `<outline>` 不写。

默认 `outline_summary=False` → `<outline>` 与 `<plot>` 同值，
与现状**逐字节一致**。

**默认值必须与现有 `write_nfo` 的实际输出逐字段一致。**
现有实现的写入规则（`src/amane/media/nfo.py`，已核对）：

| 现有行为 | 对应开关默认值 |
|---|---|
| `plot` 与 `outline` 同写 `metadata.plot`，二者同时出现 | `plot=True` `outline=True` |
| 无 `originalplot` | `originalplot=False` |
| `premiered` / `releasedate` 同写 `metadata.release`；`year` 由 release 的前 4 位派生 | `premiered=True` `releasedate=True` `year=True` |
| **无独立 `<release>` 元素**（只写 `releasedate`） | 模型中**不设** `release` 开关 |
| `num` 恒写 | `num=True` |
| `title` / `originaltitle` / `sorttitle` 三者同值 | 三者均 `True`，且 `originaltitle` 默认回退到 `<title>` 值 |
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
| **无 `website`** | `website=False`（**不是 `True`**） |
| 无 `tagline` | `tagline_format=None` |
| 无 `set` 的其它来源 | `set_by_*` 三者均 `False` |

**两处「默认值不变」与「默认输出逐字节不变」的冲突已消解**：

1. `<website>`：模型默认 `False` —— 存量条目 `website` 为空，即使默认 `True` 也不写，
   但语义上「默认不写」更贴近现状，故取 `False`。
2. `<originaltitle>`：默认走**回退**写 `<title>` 同值，与现状一致。
   真实原标题只在**显式开启**且数据存在时写出。

**改动后必须用现有 NFO 测试用例验证输出逐字节不变**（默认值下的回归）。
新增原文字段后，需**分别验收**：
- 存量数据（`original_title` / `original_plot` 为空）→ 输出与升级前逐字节一致。
- 含原文字段的新数据 → 原文字段正确出现在 `<originaltitle>` / `<originalplot>`。

`additional_tags` 支持的附加内容（对齐 MDC）：
`番号前缀` / `演员` / `分辨率` / `中文字幕` / `有码/无码` / `系列` / `片商` / `发行商`。

#### 3.4.4 NFO 单独重写

新增端点：

```
PUT /api/metadata/{id}/nfo
```

用当前生效配置重写该条目的 NFO 文件。

**不能按当前完整模板重算 NFO 路径**（审核 P2-10）：
视频模板改过之后，重算得到的 NFO 路径可能落到另一个目录或另一个文件名，
而视频**不会**跟着移动 —— 结果是一个 Kodi/Emby 匹配不上的孤立 NFO
（Kodi 按「与视频同目录同名.nfo」的规则配对）。

正确做法：**以实际视频（或链接）路径为基准构造上下文，只重算附属文件**。

```
1. 定位该条目的实际视频位置：
   - 优先用 MediaFile.path（库内已落盘的真实路径）
   - 其次用该条目关联的 STRM / 符号链接路径
   - 都没有 → 该条目无本地文件 → 返回 409（不可重写）
2. 以该视频路径为基准目录与文件名，
   只渲染附属文件模板（nfo / thumb / poster / fanart / extrafanart / trailer / subtitle），
   **不动视频模板**
3. 若计算出的 NFO 路径与视频不在同一目录 → 拒绝写入并返回 422
   （Kodi 要求 NFO 与视频同目录同名；跨目录的 NFO 无法被匹配）
4. 写 NFO。若目标 NFO 已存在 → 覆盖（这是「重写」的语义）
```

**行为边界**：

| 场景 | 行为 |
|---|---|
| 条目有多个 `MediaFile`（同一番号的多个文件） | 每个文件各自重写一份 NFO（按各自的视频路径） |
| 条目跨多个库 | 每库内各自的视频路径分别处理；库外文件不处理 |
| 条目无本地文件（只有元数据） | 返回 409，提示「无本地文件，无法重写 NFO」 |
| 视频在库外（STRM 指向库外） | 以 STRM 路径所在目录为基准；NFO 写在链接旁边 |
| 视频模板与 NFO 模板的基准目录不一致 | 返回 422，明确报错，不静默写到别处 |
| NFO 模板产出跨目录路径 | 返回 422（同上） |

**验收**：模板修改后重写 NFO，NFO 仍与视频同目录同名，Kodi 可正常匹配。

### 3.5 章节划分与文档

| 内容 | 落点 |
|---|---|
| 配置覆盖的合成语义（四层优先级、稀疏含义、显式设置标记） | `docs/dev/config.md` |
| 模板引擎双实现与语法差异 | `docs/dev/organize.md`（新建） |
| 识别词规则的匹配优先级 | `docs/dev/parsing.md`（新建） |
| 原文双轨字段的来源与按字段语言判定 | `docs/dev/data-model.md` |
| 用户可见的命名与 NFO 行为 | `docs/user/organize.md` |
| NFO 字段语义（`plot` / `outline` / `originaltitle` / `originalplot`） | `docs/user/media-server.md` |
| 覆写的备份/回滚协议与索引接管 | `docs/dev/organize.md` |

`docs/dev/index.md` 需登记新建的两个文档。

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| 覆盖 payload 校验失败 | API 返回 422 + Pydantic 错误详情；不落库 |
| 模板语法错误（保存时） | API 返回 422 + 错误位置；不落库 |
| 模板语法切换后有占位符残留 | API 返回 422 + **哪个模板、哪处残留**；拒绝切换（不能只验「解析成功」） |
| 模板渲染失败（落盘时） | 该文件整理失败，任务记 `failed`，错误信息含模板名与位置；不影响其它文件 |
| Jinja2 渲染产生越界路径 | `PathEngine.resolve` 抛 `ValueError` → 同上，失败而非逃逸 |
| Jinja2 模板访问被禁属性 | `SandboxedEnvironment` 抛 `SecurityError` → 渲染失败，错误信息明确 |
| 覆写时目标不在库内 | 拒绝覆写，记冲突 |
| **覆写中途失败**（磁盘不足 / 权限 / rename 失败） | 回滚：删除临时文件、恢复备份到原目标名；目标视频保持可用；任务记 `failed` + 回滚原因 |
| **覆写进程中断** | 备份残留（`<目标名>.amane-overwrite-<任务id>`）。下次整理同一目标时**自动恢复备份**，不视为冲突跳过；记日志 |
| 自定义识别词正则非法 | 保存时校验（`re.compile`），非法返回 422 |
| 识别词命中冲突（同 kind 同 type 多条） | 按 `created_at` 升序，首条命中生效（与 MDC「按顺序匹配，首条命中生效」一致） |
| 存量 `raw` 快照缺新字段 | `MediaMetadata` 新字段**必须有默认值**（`None`），否则 `MediaMetadata(**cached)` 抛 `TypeError` 会作废整站快照（`aggregate/engine.py:514`） |
| **原文字段与译文字段来源不同** | 原文在聚合阶段按**各自字段链**确定（`original_title` / `original_plot` 独立成链），不靠单一语言标记推断；翻译只改 `title`/`plot` |
| 翻译失败 | `title`/`plot` 保持源站原值；`original_*` 独立存在，两者都是原文，不冲突 |
| `library.template_overrides` 与实际模板字段不一致 | 以 `template_overrides` 名单为准；名单外的模板字段视为继承 |
| **单独重写 NFO 时条目无本地文件** | 返回 409，提示「无本地文件，无法重写 NFO」 |
| **单独重写 NFO 时算出的路径跨目录** | 返回 422，明确报错，不静默写到别处（Kodi 要求 NFO 与视频同目录同名） |

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| `ConfigResolver.resolve` | 表测试：全局/覆盖/库行/任务四层组合、嵌套 dict 深合并、非 dict 替换、空覆盖 |
| `template_overrides` 生效判定 | 表测试：名单内字段生效、名单外落到覆盖表/全局 |
| **历史库迁移**（P1-3） | 迁移后既有库的**全部模板类字段**进 `template_overrides`；升级后再改全局默认，既有库落盘结果**不变**；新建库 `template_overrides` 为空 |
| `ParseRuleSet` 匹配 | 表测试：番号优先于路径、路径含目录、大小写、正则 `r:` 前缀、非法正则拒绝、首条命中 |
| `TemplateEngine` 扩展 | 表测试：条件块真/假、filter 链、嵌套 default、无值判定、空组、未知 filter 报错 |
| `JinjaTemplateBackend` | 表测试：`{{ }}` 与 `{% %}`、Sandbox 拒绝属性访问、undefined 变量 |
| **兼容 filter**（P2-8） | 表测试：`default` 对空串/空列表/「未知」均生效（**不是原生只管 undefined**）；`truncate` 按 UTF-8 字节截断且**无省略号**（**不是原生行为**）；`split`/`first`/`last` 组合 |
| 两引擎一致性 | 表测试：同一组变量，两个引擎对**共有语义**产出一致（**仅排除「变量缺失」这一项有意差异**） |
| **语法切换验证**（P2-7） | 表测试：自研模板切到 Jinja2 被拒（含残留检测）；Jinja2 模板切到自研被拒；**继承与回退模板也参与验证**；两套默认模板各自可用 |
| `resolve_paths` + inplace | 表测试：inplace 时视频路径 = 源路径、图片落在源目录 |
| **覆写事务性**（P1-1） | 表测试：move/copy/hardlink/symlink 四模式各一条；**磁盘不足**时目标完好；**权限失败**时目标完好；**中断后**备份残留 → 下次整理自动恢复 |
| **覆写索引归属**（P1-2） | 表测试：覆写后 `metadata_id`/`number` 被**覆盖**（非仅补空）、`oshash` 重算、文件相位字段按新路径派生；**不同条目覆写同一路径**时源成为唯一所有者 |
| `write_nfo` + NfoOptions | 表测试：每个开关的开/关对输出 XML 的影响、默认值与旧行为一致 |
| **NFO 字段语义**（P2-9） | 表测试：`<plot>` = 详细剧情、`<outline>` 默认与 `<plot>` 同值、`outline_summary=True` 时取摘要、`<originaltitle>` 默认回退写 `<title>` 值、`<originalplot>` 默认不写 |
| **原文双轨聚合**（P1-4） | 表测试：**标题来自日文源、剧情来自中文源**时，原文分别取各自字段链；未知语言不写原文；**翻译失败**时 `title`/`plot` 保持原值 |
| `original_title` / `original_plot` 聚合 | 表测试：各自字段链取首个非空、`field_sources` 记录来源 |
| `website` 取值 | 表测试：取主源（title 的来源）的 `source_url`、`source_urls` 全量保留；**`WEBSITE` 不在 `SCALAR_FIELDS`** |
| avsox 多语言修复 | 表测试：声明 `MULTI_LANGUAGE` 后 `_lang` 生效、`title_ja`/`description_ja` 可取到、`_first` 顺序修正 |
| `MediaMetadata` 新增字段 | 回归：存量 `raw` 快照仍能反序列化（不抛 `TypeError`） |
| **单独重写 NFO**（P2-10） | 表测试：模板改过后重写，NFO 仍与视频同目录同名；多文件条目各写一份；跨库分别处理；**无本地文件返回 409**；**跨目录路径返回 422** |
| API 端点 | 覆盖 CRUD、422 路径、删除后回到继承 |

**回归重点**：

1. **既有库的落盘结果不变** —— 用现有测试套件验证；
   补一条「历史库迁移后 `template_overrides` 含全部模板字段、
   升级后再改全局默认、既有库落盘结果仍不变」的用例。
2. **存量 `raw` 快照兼容** —— 补一条用旧格式 `raw` 反序列化的用例。
3. **NFO 默认输出逐字节不变** —— 用现有 NFO 测试验证。
   注意：现状是 `<outline>` 与 `<plot>` 同写完整剧情，
   故默认 `outline_summary=False`（`<outline>` 与 `<plot>` 同值）保证逐字节不变；
   摘要行为作为**可选开关**，开启才是行为变更。
   `<originaltitle>` 默认走回退写 `<title>` 值，与现状一致。
   另需**分别验收**：存量数据（`original_*` 为空）输出不变；
   含原文字段的新数据，原文正确出现在 `<originaltitle>` / `<originalplot>`。

## 6. 实施顺序（供实现计划参考）

按「先改现有行为、后加新能力」的顺序，每步都可独立验证：

### 第 1 组：原文双轨与数据层

1. **avsox 两处修复**（声明 `MULTI_LANGUAGE` + `_first` 顺序）
2. **`MediaMetadata` 加 `original_title` / `original_plot` / `field_languages`**（含默认值）
3. **`Metadata` 加四列**（`original_title` / `original_plot` / `website` / `field_languages`）
   + `MetadataField` 加三枚举值 + `SCALAR_FIELDS` 加两个原文字段
   + `AggregatedMetadata` 加三属性 + `MetadataFields` TypedDict
   + `update_metadata` 显式赋值 + Alembic revision
4. **各源填原文字段**（DMM / MGStage / official / wp_works / avsox / r18dev 等）
5. **`website` 派生取值**（主源的 `source_url`，不进 `SCALAR_FIELDS`）
6. **翻译层只改 `title`/`plot`**（不碰 `original_*`）
7. **`NfoOptions` 模型 + `write_nfo` 改造**（默认值对齐现有输出 + 原文/译文分离
   + `outline_summary` 可选开关）

### 第 2 组：配置基础层

8. **4 个新 section 模型**（`common` / `naming` / `nfo` / `parser`）加进 `HotSettings`
9. **`ConfigResolver` + `config_overrides` 表** + `Library.template_overrides` 列
   + **历史库数据迁移**（既有库全部模板字段进名单）
10. **`ParseRuleSet` + `parse_rules` 表** + `parse_file_info` 扩展参数

### 第 3 组：模板引擎与整理

11. **模板引擎扩展**（自研语法：条件块 / filter / 降级链）+ `TemplateBackend` 抽象
12. **`JinjaTemplateBackend` + Jinja2 主依赖 + 兼容 filter**（`default` / `truncate` 自建）
13. **语法标识 + 两套默认模板 + 切换时的语义验证**
14. **`MoveMode.INPLACE` + 落盘分支**
15. **覆写的备份 / 原子替换 / 回滚协议**
16. **覆写的索引接管**（独立于 `commit_organized_media_file`）
17. **试算端点 + 前端试算面板**
18. **覆盖 API + 前端覆盖编辑 UI**
19. **识别词管理 API + 前端**
20. **原文字段的前端展示**（详情页 + i18n）
21. **单独重写 NFO 端点**（以实际视频路径为基准）

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
   加了 `original_title` 后可写真实原标题（需显式开启 `originaltitle` 的原文模式，
   默认仍回退写 `<title>` 值以保证逐字节不变）。
2. **NFO 的 `<outline>` 现写的是完整剧情**，不符合 Kodi 的「摘要」语义 ——
   本阶段**默认不改**（保持逐字节不变），摘要行为作为 `outline_summary` 可选开关。
3. **avsox 未声明 `MULTI_LANGUAGE`**，导致 `title_ja` / `description_ja` 是死代码。
4. **avsox 的 `_first` 把 `description_cn` 排在 `description_ja` 之前**。

### 已知限制（明确不做）

1. **Javdb / Javbus / iqqtv 不提供原标题** —— 需改 locale 或请求头，
   会影响既有抓取行为，本阶段不动。这些源的 `original_title` 留空。
2. **Javdb / Javbus / Javlibrary / Jav321 / freejavbt 不解析 plot** ——
   这些源不提供原文简介。
3. **`original_plot` 无数据来源的条目** —— 存量数据未重新刮削前为空，
   `<originalplot>` 不写（默认本就关闭）。
4. **r18dev 无 `source_url`** —— 离线镜像无网页，其 `website` 为空。
5. **`<originalplot>` 是 Kodi 非标准字段** —— Emby / Jellyfin 会忽略它，
   仅作信息留存。需要媒体服务器读取原文简介的，仍应依赖 `original_plot` 的
   其它消费方（如 Amane 自己的 API / 前端）。
6. **`votes` / `country` / `customrating` 三个 NFO 开关暂无数据来源** ——
   本阶段保留开关但不产出内容，等数据模型补齐后再实现。
