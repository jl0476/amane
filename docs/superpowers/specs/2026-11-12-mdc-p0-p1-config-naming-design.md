# 设计规格：配置覆盖层与命名整理（MDC 迁移 P0 + P1）

> 日期: 2026-11-12
> 状态: 待审阅
> 范围: MDC 能力迁移的第一阶段（配置基础层 + 命名与整理）
> 上游总纲: `.research/mdc/migration-roadmap.md`

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
7. 全程兼容现有 Amane 数据：既有库的落盘结果不因升级而改变。

**非目标**：

- 不做图片处理链（P2）、水印系统（P3）、插件 API v2（P4）。
- 不引入 MDC 的任何代码或前端资源。
- 不做下载器集成。
- 不改变刮削来源与聚合逻辑。

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
**保留不动**，作为「库级覆盖」的第二来源。优先级：

```
任务 payload  >  library 行字段  >  config_overrides 表  >  全局 HotSettings
```

理由：`Library` 行字段是已有的、已在用的、库创建时就确定的；
覆盖表是新增的、更细粒度的。行字段优先可避免升级后行为突变
（老库的 `video_template` 非空时不会被覆盖表改变）。

**例外**：`Library` 行的模板字段为空（`None`）时，才落到覆盖表/全局。
这保持了「既有库的落盘结果不因升级而改变」。

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

**Jinja2 是可选依赖**：`pyproject.toml` 加 `jinja2` 到主依赖还是 optional extra？
建议**主依赖**（体积小 ~1MB，且一旦用户在模板里用了 Jinja2 语法，缺依赖会导致任务失败而非降级）。
若加为 extra，则前端需在未安装时禁用 Jinja2 选项。

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

### 3.3 整理（P1）

#### 3.3.1 整理模式

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

#### 3.3.2 覆写策略

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

#### 3.3.3 NFO 字段开关

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
website: bool = False
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

#### 3.3.4 NFO 单独重写

新增端点：

```
PUT /api/metadata/{id}/nfo
```

用当前生效配置重写该条目的 NFO 文件。
需要定位 NFO 文件路径 —— 现有 `organize` 落盘时未记录 NFO 路径。
方案：`MediaFile` 增加 `nfo_path` 列，或从 `resolve_paths` 重算。
建议**重算**（`resolve_paths` 是确定性的），避免新增列。
但若模板被改过，重算得到的是**新**路径 —— 这正是「单独重写」的语义。

### 3.4 章节划分与文档

| 内容 | 落点 |
|---|---|
| 配置覆盖的合成语义（三层优先级、稀疏含义） | `docs/dev/config.md` |
| 模板引擎双实现与语法差异 | `docs/dev/organize.md`（新建） |
| 识别词规则的匹配优先级 | `docs/dev/parsing.md`（新建） |
| 用户可见的命名与 NFO 行为 | `docs/user/organize.md` |

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

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| `ConfigResolver.resolve` | 表测试：全局/覆盖/任务三层组合、嵌套 dict 深合并、非 dict 替换、空覆盖 |
| `ParseRuleSet` 匹配 | 表测试：番号优先于路径、路径含目录、大小写、正则 `r:` 前缀、非法正则拒绝、首条命中 |
| `TemplateEngine` 扩展 | 表测试：条件块真/假、filter 链、嵌套 default、无值判定、空组、未知 filter 报错 |
| `JinjaTemplateBackend` | 表测试：与自研引擎的等价用例、`{{ }}` 与 `{% %}`、Sandbox 拒绝属性访问、undefined 变量 |
| 两引擎一致性 | 表测试：同一组变量，两个引擎对**共有语义**产出一致（无值语义差异除外） |
| `resolve_paths` + inplace | 表测试：inplace 时视频路径 = 源路径、图片落在源目录 |
| 覆写策略 | 表测试：默认跳过不覆盖、开启后覆写、目标非视频拒绝覆写、跨库拒绝 |
| `write_nfo` + NfoOptions | 表测试：每个开关的开/关对输出 XML 的影响、默认值与旧行为一致 |
| API 端点 | 覆盖 CRUD、422 路径、删除后回到继承 |

**回归重点**：既有库的落盘结果不变 —— 用现有测试套件验证，
并补一条「老库（模板字段非空）不受覆盖表影响」的用例。

## 6. 实施顺序（供实现计划参考）

1. `NfoOptions` 模型 + `write_nfo` 改造 + 默认值对齐（**先做，因为它改的是现有行为**）
2. `ConfigResolver` + `config_overrides` 表 + Alembic revision
3. `ParseRuleSet` + `parse_rules` 表 + `parse_file_info` 扩展参数
4. 模板引擎扩展（自研语法）+ `TemplateBackend` 抽象
5. `JinjaTemplateBackend` + Jinja2 依赖 + 语法标识
6. `MoveMode.INPLACE` + 落盘分支
7. 覆写策略
8. 试算端点 + 前端试算面板
9. 覆盖 API + 前端覆盖编辑 UI
10. 识别词管理 API + 前端

## 7. 待确认

| # | 问题 | 状态 |
|---|---|---|
| 1 | Jinja2 作为主依赖还是 optional extra？ | 建议主依赖，待用户确认 |
| 2 | NFO 默认字段集 | **已定案**：以现有 `write_nfo` 实际输出为准（见 3.3.3 的逐字段对照表） |
| 3 | 覆盖表与 `Library` 行的优先级 | 本文档定为「Library 行优先」，需确认 |
| 4 | `parse_rules` 是否并入 `FacetRule` 表而非独立表？ | 本文档定为独立表（语义不同），需确认 |
| 5 | 「原始 plot」字段的来源 | Amane 的 `Metadata` 只有 `plot` 一个简介字段，无原文简介。`originalplot` 开关默认 `False` 且**无数据来源**；若要实现，需先在数据模型加 `original_plot` 字段（本次范围外）。同理 `originaltitle` 现有 NFO 里写的是 `title` 的同值，不是真正的原标题 —— 见下方「已知不一致」 |

### 已知不一致（实现时需处理或明确不做）

1. **NFO 的 `originaltitle` 现写的是中文标题同值**，不是原标题。
   Amane 数据模型无原标题字段（`.research/mdc/architecture.md` 的字段对照已记录此事）。
   本阶段**不新增数据模型字段**，故 `originaltitle` 开关仅控制该元素的写出与否，
   写出的值仍是 `title`。
2. **`originalplot` 无数据来源**，同 `plot`。
3. **`website` 无数据来源** —— Amane 的 `Metadata` 无 `source_urls` 到单 URL 的便捷字段
   （有 `source_urls: dict`）。本阶段 `website` 开关保留但不生效，或从 `source_urls`
   取第一个值实现。**需在实现时二选一并写入 `docs/dev/`**。
