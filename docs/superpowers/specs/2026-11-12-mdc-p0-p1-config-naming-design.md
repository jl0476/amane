# 设计规格：配置覆盖层与命名整理（MDC 迁移 P0 + P1）

> 日期: 2026-11-12
> 状态: 待审阅（第 9 版，按第 8 轮联合复审修订 4 项 P2）
> 范围: MDC 能力迁移的第一阶段（配置基础层 + 命名整理 + 原文双轨字段）
> 上游总纲: `.research/mdc/migration-roadmap.md`

## 0. 已定决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | Jinja2 依赖策略 | **主依赖**（非 optional extra） |
| 2 | 覆盖表 vs `Library` 行优先级 | **覆盖表优先**（`template_overrides` 名单外的库行字段让位） |
| 3 | 识别词表设计 | **独立表** `parse_rules`，表内自带作用域列（不并入 `FacetRule`，也不进 `config_overrides`） |
| 4 | NFO 的 `originaltitle`/`originalplot`/`website` | `Metadata` 加 `original_title` / `original_plot` / `website` 三列，`original_plot` 取**源站原文**（日文） |
| 5 | 命名引擎 | 自研语法为默认 + Jinja2 兼容层 |
| 6 | 人脸识别裁剪 | OpenCV（P2，本阶段只预留接口） |
| 7 | 自研语法的 filter 分隔符 | **`:`**（`|` 已被值映射占用，见 3.2.2 的语法冲突分析） |
| 8 | Jinja2 引擎的变量名 | **去掉 `?` 后缀**（`cd?` → `cd` 等）。`?` 在 Jinja2 里是非法标识符字符，见 3.2.3 的映射表 |
| 9 | 关键词识别规则的路径输入 | `apply_rules` 显式接收原始路径；`parse_file_info` 内部保留路径，按需传给规则 |
| 10 | 识别词的匹配顺序 | **扁平序列**：合成后的规则按「番号关键词全部 → 路径关键词全部」，各段内按 `created_at` 升序，首条命中生效。桶只用于合成，不用于匹配 |
| 11 | 恢复时的源行身份 | `MediaFile.id` **不是**稳定身份（表无 AUTOINCREMENT，rowid 会复用）；身份判定用**三字段内容指纹（path + oshash + size），全部必填、全等才算命中**。指纹在覆写开始前现场补算，journal 中不存在空指纹；证据不足即放弃接管或放弃删源。**接管与删源前还必须核验目标副本**：提交点后目标内容即源内容，目标现场 oshash 必须与 journal 指纹一致，缺失或不一致时保留源与 journal |
| 12 | 识别词匹配引擎 | **RE2**（`google-re2` 主依赖）。RE2 线性时间从引擎层消除灾难回溯，语法子集与嵌套量词拒绝都无法给出可证明边界（`(a|aa)+$` 无嵌套量词仍指数回溯）；RE2 不支持的构造编译即拒 → 422 |

## 0.1 评审意见处理记录

第 1 版经独立评审提出 4 项 P1、6 项 P2；第 2 版经复审提出 3 项 P1、7 项 P2；
第 3 版经复审提出 1 项 P1、7 项 P2；第 4 版经复审提出 1 项 P1、4 项 P2；
第 5 版经复审提出 1 项 P1、2 项 P2；第 6 版经复审提出 1 项 P1；
第 7 版与实现计划联合审核提出 2 项 P1、6 项 P2。七轮意见全部成立。
前几轮的处理已并入正文；第 3、4 轮的处理明细见第 4、5 版存档，第 5 轮明细见第 6 版存档，
第 6 轮明细见第 7 版存档，本版修订第 7 轮项目：

| 级别 | 问题 | 处理 | 落点 |
|---|---|---|---|
| P1 | 计划遗漏刮削结果落库接线 | `ScrapeHandler.handle` 的 `upsert_metadata` 调用**显式补四参数**（`original_title` / `original_plot` / `website` / `field_languages`），并增加「执行刮削任务后从 DB 与 API 读回」的集成测试 | 3.3.4 |
| P1 | 生效命名配置未传入真实落盘流程 | 新增**生效配置传递接口**：任务入口解析一次 `NamingConfig`，向下经 `apply_file_operations` 传给 `resolve_paths`；`resolve_paths` / `resolve_sidecar_paths` 接受生效命名，不回读库行 | 3.1.3 |
| P2 | 正常流程接管后才核验目标 | 阶段 C 重排：目标核验先行，接管+committed 同事务随后，删源前再核验一次 | 3.4.2 |
| P2 | Jinja 后端缺运行时分派 | 分派点覆盖视频、附属产物、STRM 正文、剧照文件名五处真实渲染入口；验收为「保存 Jinja 模板 → 整理 → 检查实际产物」 | 3.2.1 |
| P2 | 覆写模块未接入现有冲突分支 | `execute_file_operations` 的 `OCCUPIED` 预检按 `common.overwrite_video` 分派跳过或覆写协议；`overwrite_image` 传入图片写入 | 3.4.2 |
| P2 | 识别词缺生产入口注入 | 明确五个生产入口（扫描 / 整理落盘 / 任务请求解析 / 清理清单 / 调度）各自注入 `rules=` 与缓存提供者 | 3.1.6 |
| P2 | 256 字符上限不防 ReDoS | 初版采用「拒绝嵌套量词」受限语法；第 8 轮复审指出 `(a|aa)+$` 无嵌套量词仍指数回溯，**改用 RE2 线性时间引擎**（`google-re2` 主依赖），从引擎层保证安全 | 3.1.6 |
| P2 | 前端客户端生成顺序错误 | `just generate` 提前到前端任务第一步（穷尽映射依赖生成的 `MetadataField` 类型） | 实现计划 |

第 9 版修订第 8 轮（联合复审后续）的 4 项 P2：

| 级别 | 问题 | 处理 | 落点 |
|---|---|---|---|
| P2 | 嵌套量词拒绝仍放过 `(a|aa)+$`（无嵌套量词，指数回溯） | 匹配引擎改为 **RE2**（`google-re2` 主依赖，已实测 3.14/Windows wheel 可用、长近似输入线性完成、反向引用与环视编译即拒）；撤销「不存在运行期回溯风险」的说法，改为「引擎保证线性时间」 | 3.1.6 |
| P2 | 字幕落盘遗漏生效配置与模板后端 | `resolve_subtitle_path` 渲染 `subtitle_template`，纳入分派表与 `naming` 传递链；精确区分「字幕配对不渲染」与「字幕落盘渲染」；验收断言磁盘实际字幕路径 | 3.2.1、3.1.3 |
| P2 | 双库扫描验收与 `content_type` 归属冲突 | `MediaFile` 无 `content_type`（类型在全局唯一的 `Metadata`，首建时定）；验收改为断言解析结果 `FileInfo.content_type` 与后继 SCRAPE 任务载荷，并验证既有条目类型稳定 | 3.1.6 |
| P2 | 计划指定的 `re._parser.sre_parse.parse` 入口不存在（3.14 实测只有 `re._parser.parse`） | 随 RE2 方案消解：不再解析 Python `re` 语法树，RE2 编译即校验 | 3.1.6 |

## 1. 目标

把 MDC 的「逐条目配置覆盖」与「命名整理」能力以原生 Python 重新实现进 Amane，
不搬运 MDC 代码，不复用其容器。

**成功标准**（可验证）：

1. 一个媒体库能配置自己的命名模板与整理策略，覆盖全局默认而不影响其它库。
2. 用户能为「国产 / 欧美 / 素人 / 无码」自定义番号前缀与路径关键词识别规则，且不同媒体库可采用不同规则。
3. 命名模板支持条件块、filter、变量降级链；自研语法与 Jinja2 两套并存且可切换。
4. 整理支持 5 种模式（含原地），冲突时可选择跳过或覆写。
5. NFO 的每个字段可独立开关，不再固定。
6. 命名模板可试算：给定一个番号或一条媒体文件，输出最终路径与文件名，不落盘。
7. **元数据保留原文双轨**：`original_title` / `original_plot` 存源站原文（日文），
   `title` / `plot` 存翻译后（中文），两者并存不互相覆盖。
8. `website` 存主来源详情页 URL 的单值，`source_urls` 保留全量。
9. 全程兼容现有 Amane 数据：既有库的落盘结果不因升级而改变。

**数据安全标准**：

10. **覆写是事务性的**：任一步失败（磁盘不足、权限、进程中断）后，
    磁盘上仍有可用的视频；备份可自动恢复。
11. **覆写后的索引归属正确**：目标视频被源替换后，
    `metadata_id` / `number` / `oshash` 与文件相位字段均按新内容重算，
    不残留旧条目的关联。
12. **文件替换与索引接管之间可恢复**：替换完成、索引未接管的中间状态，
    在应用下次启动或下次整理同一目标时自动补齐，不产生「新视频 + 旧条目」的持久错配。
13. **既有库升级后行为不变**：迁移保留全部历史生效值；
    升级后再修改全局默认，既有库的落盘结果仍不变；
    升级后为既有库设置覆盖，覆盖**仍能生效**。

**兼容性标准**：

14. **语法切换必须做语义验证**：不能只验「解析成功」
    （Jinja2 会把自研占位符当普通文本，解析成功但输出字面量）。
    切换时验证全部生效模板，含继承与回退模板。
15. **两套引擎的 filter 语义一致**：`default` / `truncate` 用自建兼容实现，
    不依赖 Jinja2 原生行为。
16. **NFO 默认输出不变**：存量条目在默认配置下写出的 XML 内容与升级前一致
    （同一行尾约定下逐字节比对）。
17. **枚举契约不破坏**：新增字段不使 `MetadataField` 与爬虫模型的一致性测试失败。

**非目标**：

- 不做图片处理链（P2）、水印系统（P3）、插件 API v2（P4）。
- 不引入 MDC 的任何代码或前端资源。
- 不做下载器集成。
- 不改动刮削来源的选取逻辑（只补字段取值，不改路由与优先级）。
- 不引入 MDC 的 `max_folder_length` / `max_actor_count` / 文件过滤开关等本阶段无实现的功能字段。

## 2. 现状与约束

### 2.1 现有配置层次

Amane 现有三层配置，**语义是「复制」而非「继承」**：

| 层 | 位置 | 语义 |
|---|---|---|
| 全局 | `src/amane/config/manager.py::HotSettings` | TOML 持久化，`model_dump(mode="json", exclude_none=True, exclude_defaults=True)` |
| 库级 | `src/amane/db/models.py::Library` | 字段来自**建库那一刻的代码默认值**，不来自当时的全局配置 |
| 任务级 | `src/amane/handlers/models.py::LibraryBase` | `payload` 字段为 `None` 时由各子类的 `_apply_library` 沿用库设置 |

`HotSettings` 有 **14 个** section 字段：`scraping` / `media_server` / `actor_scraping` / `agent` /
`network` / `sr` / `semantic` / `watermark` / `llm` / `r18` / `watcher` / `worker` / `logging` / `plugins`，
`model_config = {"extra": "forbid"}`。其中 `plugins` 的类型是 `dict[str, PluginConfig]`，不是配置模型。

`_deep_merge`（`manager.py::_deep_merge`）只合并两层：顶层 key 之间若两侧都是 dict，
做一次浅层展开；第三层及更深的 dict、以及 list 与标量，一律由 patch 整体替换。

### 2.2 现有命名能力

- 模板引擎 `src/amane/organize/template.py::Parser` / `TemplateEngine` / `PathEngine` / `StrmEngine`
- 语法：`{name}`、`{name|k=v}` 值映射、`{name|=缺省}`、`[...]` 可选组、`[[...]]` 双层组
- **30 个占位符**（`TemplateVariables`）；另有运行时别名 `dir`（`TemplateContext.from_metadata` 注入，
  不在 schema、不在 `PLACEHOLDERS`）
- `PLACEHOLDER_MAP_KEYS` 只对 4 个占位符约束值映射白名单：`content_type` / `mosaic?` / `def?` / `sub?`
- 落盘路径解析 `src/amane/organize/path_templates.py::resolve_paths` → `ResolvedPaths`
- 可选模板的默认值由 `resolve_paths` 在渲染时回退到模块常量（`THUMB_TEMPLATE_DEFAULT` 等）
- 视频扩展名始终取源文件（`organize/file.py::video_dest`）
- 路径逃逸防护 `PathEngine.resolve`（相对模板须在 `base_path` 下，绝对模板须在 `base_path` 或 `safe_dirs` 下）
- 冲突判定 `organize/file.py::target_state` → `TargetState`（FREE/SAME/OCCUPIED/UNKNOWN）
- 落盘 `organize/file.py::execute_organize` 的 `match mode:` 只有 4 个分支（move/copy/hardlink/symlink），
  **无兜底分支** —— 新增枚举值而不新增分支会静默落空并返回 PLACED

### 2.3 现有 NFO

`src/amane/media/nfo.py::write_nfo(metadata, nfo_path) -> bool` —— Kodi `<movie>` XML，
用 `StringIO` 手工拼接，元素顺序与缩进硬编码；写入用 `aiofiles.open(..., encoding="UTF-8")`
且未指定 `newline`，Windows 上产生 CRLF。
`docs/user/media-server.md` 明确写「NFO 内容固定, 不提供自定义模板」。

### 2.4 约束

- `src/amane/parsing/` 是**纯函数包，无 I/O 与配置依赖**（`docs/dev/architecture.md` 模块边界）。
  用户自定义识别词不能靠读配置实现。
- 插件是进程内纯 Python、无沙箱、不能自带第三方依赖（`docs/dev/plugins.md`）。
- 配置改动要么热生效（走 `AppRuntime._rebuild`），要么标注需重启。

## 3. 设计

### 3.1 配置覆盖层（P0）

#### 3.1.1 数据模型

引入一张表，**存稀疏 JSON，不用 Pydantic 模型表达 null**：

```
config_overrides
  id            PK
  scope_type    TEXT   -- 'library' | 'watch_dir'
  scope_id      INTEGER -- libraries.id 或 watch_dirs.id
  section       TEXT   -- 见下表
  payload       TEXT   -- JSON, 稀疏; 只含显式设置的键
  created_at, updated_at
  UNIQUE(scope_type, scope_id, section)
```

`payload` 是**稀疏**的：键缺失 = 继承；键存在 = 覆盖。

**禁止 JSON null**。`null` 有两种可能的含义（「继承」与「显式关闭」），
无法同时表达，因此 `payload` 中**不允许出现 null 值**，写入时校验拒绝（422）。

对于本身可为空、且「空」有意义（而不是继承）的字段，用**显式替代表示**：

| 字段 | 想要的语义 | payload 中的写法 |
|---|---|---|
| `link_template` | 不创建链接 | `{}`（空对象） |
| `strm_content_template` | 用默认正文（视频绝对路径） | `{}` |
| `thumb_template` / `poster_template` / `fanart_template` / `extrafanart_template` / `extrafanart_name_template` / `nfo_template` / `trailer_template` / `subtitle_template` | 采用模块内建默认 | `{}` |

即：这些字段在 payload 中的类型是 `str | dict`（`dict` 只允许空对象），
`{}` 在构造 section 模型时映射为 `None`。这样「键缺失」与「显式取空」可区分，
且不引入 null。

与 Pydantic 的落盘对齐：`HotSettings` 用 `exclude_none=True` 写 TOML，
空值同样不出现；`{}` 经校验后变成 `None`，读取时按字段自身的 `None` 语义解释。
两条路径的「空」语义一致。

section 沿用 MDC 的划分，但**只有已实现的三节可写入**：

| section | 状态 | 内容 |
|---|---|---|
| `common` | 本阶段实现 | 覆写开关 |
| `naming` | 本阶段实现 | 命名模板、语法、附属产物 |
| `nfo` | 本阶段实现 | NFO 字段开关 |
| `download` / `watermark` / `metadata` / `provider` | 预留 | 写入返回 422 |

`parser` **不是** section：识别规则是多行记录，存 `parse_rules` 表（3.1.6）。

`scope_type` 本阶段只接受 `library`；`watch_dir` 预留，写入返回 422。

#### 3.1.2 新增配置 section 模型

现状：`HotSettings` 没有 `common` / `naming` / `nfo`，也没有任何同义 section。
新增 3 个模型并加进 `HotSettings`（`extra="forbid"` 下未知字段会被拒绝）。

```python
class OrganizeCommonConfig(BaseModel):
    """整理时的覆写策略. 只放本阶段实现的字段."""

    model_config = {"extra": "forbid"}

    overwrite_video: bool = False
    """目标已有视频时覆写源文件; 默认 False 保持现状 (冲突即跳过)."""

    overwrite_image: bool = True
    """目标已有图片产物时是否覆写.

    默认 True —— 现有 `handlers/file.py::_place_library_images` 直接 copy2 写入,
    不判断目标上是否已有产物, 即现状就是覆盖写. 取 True 才能保持既有行为;
    显式设 False 才转为「目标已存在则跳过」.
    """


class NamingConfig(BaseModel):
    """命名模板与语法. 字段与 Library 行的模板类字段一一对应."""

    model_config = {"extra": "forbid"}

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
    subtitle_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_SUBTITLE_EXTENSIONS))
    write_nfo: bool = True
    copy_resources: list[DownloadableResource] = Field(default_factory=lambda: list(DownloadableResource))


class NfoConfig(BaseModel):
    """NFO 字段开关. 字段清单与默认值见 3.4.3."""

    model_config = {"extra": "forbid"}
    ...  # NfoOptions 的全部字段
```

`parse_rules` 的编译产物 `ParseRuleSet` 是冻结 dataclass，不是配置模型，也不挂进 `HotSettings`。

**关键语义**：模板类字段的 `None` 是**显式值**（「采用模块内建默认」或「不创建链接」），
不是「继承」。原因见 3.1.5 的证据：`create_library` 的 8 个可选模板参数默认就是 `None`，
`resolve_paths` 靠 `library.xxx_template or XXX_TEMPLATE_DEFAULT` 回退。
若把 `None` 当作继承标记，`link_template=None`（不建链接）会变成「继承全局的链接模板」，
行为被反转。

在 **payload 与 TOML 中**，这种「显式取空」写作空对象 `{}`（3.1.1）；
只有模型层用 `None` 承载。模型校验器把 `{}` 转成 `None`，
拒绝其他任何 dict，也拒绝 null。

`copy_resources` 的默认值取 `list(DownloadableResource)`（含 trailer），
与 `create_library` 与 `LibraryCreateRequest` 的实际默认一致。
`db/models.py::Library.copy_resources` 的模型默认是「除 trailer 外全部」，三处不一致，
本阶段不改动任何一处的既有行为，登记为待确认（§7）。

`NfoConfig` 不设 `enabled` 总开关 —— 总开关是 `naming.write_nfo`，避免同一个事实有两个字段。

**与 `Library` 行的合成关系**：

| section | 与 `Library` 行的关系 |
|---|---|
| `naming` | `Library` 行的 15 个模板类字段是 `naming` 的**同名子集**，按 3.1.5 的 `template_overrides` 名单参与合成 |
| `common` | `Library` 行的 `move_mode` / `min_file_size` / `blacklist_patterns` / `trailer_pattern` **恒定生效**，不被 `common` 覆盖。`common` 中**不设**同义字段，避免出现「两个字段表达同一策略」 |
| `nfo` | `Library.write_nfo` 是 `naming.write_nfo` 的库级对应项，按名单参与合成；`nfo` section 本身无库行对应字段 |
| `parse_rules` | 库级独立，见 3.1.6；`Library` 行无对应字段 |

#### 3.1.3 生效配置解析

新增 `src/amane/config/overrides.py`：

```python
class ConfigResolver:
    """把全局配置、库行与覆盖合成生效配置."""

    def resolve[S: BaseModel](
        self,
        section: type[S],
        *,
        scope: Scope | None = None,
        library: Library | None = None,
        payload: dict[str, Any] | None = None,
    ) -> S:
        """返回该 section 的生效模型."""
```

合成顺序**唯一**，四层由低到高：

```
1. 全局 HotSettings 中该 section 的模型
2. Library 行字段 —— 仅当字段名在 template_overrides 中；否则整层跳过
3. config_overrides 表（该 scope 的该 section）
4. 任务 payload —— 仅限用户显式给出的字段
```

逐层用「显式出现的键」覆盖上一层；dict 值逐键深合并，非 dict（含 list）整体替换。

**第 4 层必须只包含用户显式给出的字段**，这是本节唯一的实现改造点。

现状：`api/support/task_resolve.py::resolve_submission` 在**入队前**就调用
`payload.resolve(repo)`，而 `handlers/models.py::OrganizePayload._apply_library` 会把
`library.write_nfo` 与 `library.copy_resources` 填进 payload。之后
`handlers/file.py::OrganizeHandler.handle` 又按「payload 非 None 即优先」取用。
若沿用这条链路，库值会被当成用户显式值，成为最高优先层，**压掉第 3 层的覆盖表**。

**不能用 `model_fields_set` 承担这件事**：任务 payload 以
`payload.model_dump(mode="json")` 存入 `tasks.payload`，执行时经
`TemplateTaskHandler.parse_payload` 的 `model_validate(raw)` 还原。
实测该往返后 `model_fields_set` 变成**全部字段**（`model_dump` 会补出 `None` 键），
「用户没给」与「用户给了 null」的区分在跨进程处丢失。

因此改为**显式的持久化字段**：

```python
class OrganizePayload(LibraryBase):
    ...
    explicit_fields: list[str] = Field(default_factory=list)
    """用户显式给出的模板字段名. 入队前由 Submission 记录; 库值回填不计入.

    必须落库 —— ``model_fields_set`` 经 JSON 往返后不再可靠.
    """
```

- `OrganizeSubmission`（`api/models/tasks.py`）在 `_apply_library` **之前**记录
  用户在请求体中实际给出的字段名，写入 `explicit_fields`。
  记录规则：取 `model_fields_set` 与这两个字段（`write_nfo` / `copy_resources`）的交集，
  再**排除值为 `None` 的字段** —— 这两个字段的 `null` 本就定义为「未指定」
  （字段类型是 `bool | None` / `list | None`），null 不进显式集。
  空列表 `[]` 是显式值（判断是 `is None` 而非真值测试），表示复制零个资源。
  手动整理路径（`POST /api/tasks`）由此得到空列表或用户给出的子集。
- 自动整理路径（`handlers/scrape.py` 的 `_auto_organize` 构造
  `OrganizePayload(library_id=..., media_file_ids=[...])`）不设该字段，
  落在默认空列表 —— 与手动整理不带模板字段时完全一致。
- `_apply_library` 的既有行为不变（它仍为执行提供可用的库值）；
  resolver 的第 4 层**只读 `explicit_fields` 中列出的字段**，其余字段的库值
  按第 2 层（`template_overrides`）规则处理，不进第 4 层。

**历史任务的兼容解析**：升级前入队的任务没有 `explicit_fields` 键，
但升级前的行为是「payload 中非 `None` 的字段优先」。因此解析规则为：

```
"explicit_fields" not in raw  →  显式集 = {write_nfo, copy_resources} ∩ {payload 中非 None 的字段}
"explicit_fields" in raw      →  显式集 = raw["explicit_fields"]
```

缺失键时按旧语义重建，升级前入队的旧任务行为不变；
`parse_payload` 在 `model_validate` 之后做这一步（对实例补写 `explicit_fields`）。

**同一覆盖结果的手动与自动两条路径**：两条路径都不带模板字段，
`explicit_fields` 都为空，第 4 层为空，生效配置完全相同。验收需覆盖这一点。

**字段分类表**（决定 `Library` 行哪些字段参与第 2 层）：

| 类别 | 字段 | 是否参与合成 |
|---|---|---|
| 模板类 | `video_template` / `link_template` / `link_mode` / `strm_content_template` / `thumb_template` / `poster_template` / `fanart_template` / `extrafanart_template` / `extrafanart_name_template` / `nfo_template` / `trailer_template` / `subtitle_template` / `subtitle_extensions` / `write_nfo` / `copy_resources` | ✅ 仅当在 `template_overrides` 中 |
| 非模板类 | `move_mode` / `min_file_size` / `blacklist_patterns` / `trailer_pattern` | ❌ 恒定生效，不经 resolver |

`copy_resources` 归入模板类：它与 `write_nfo` 同属「产出哪些附属文件」，与路径模板共同决定落盘产物。

调用点：

| 位置 | 用途 |
|---|---|
| `handlers/file.py::OrganizeHandler` | 任务入口解析一次 `common` / `naming` / `nfo`，向下传递 |
| `handlers/file.py::apply_file_operations` | 接收生效配置并传入渲染与落盘 |
| `handlers/file.py::execute_file_operations` | 落盘路径与 NFO 生成 |
| `handlers/scrape.py::ScrapeHandler.handle` | 落库时补原文字段（见 3.3.4） |
| `api/routes/libraries.py` | 库详情返回「当前生效配置」 |

**生效配置的传递接口**（联合审核 P1 修订）：

`apply_file_operations` 现状是自取 `repo.get_library` 后把**库行**直接传给
`resolve_paths`——若只在任务入口做合成，下层渲染仍会按库行模板算路径，
覆盖配置对真实落盘无效。因此：

- `OrganizeHandler` 在任务入口解析一次生效的 `NamingConfig` / `OrganizeCommonConfig` /
  `NfoConfig`，作为参数传入 `apply_file_operations`。
- `resolve_paths` 与 `resolve_sidecar_paths` 新增可选参数
  `naming: NamingConfig | None = None`：非 None 时模板字段（`video_template` 及
  全部附属模板、`link_template` / `link_mode` / `strm_content_template`）取自该对象；
  None 时维持现状按 `Library` 行取值（既有调用与测试不回归）。
- `resolve_subtitle_path` 同样新增 `naming` 参数（字幕落盘路径渲染
  `subtitle_template`，经 `place_subtitles` 传递），分派规则见 3.2.1。
- 下层函数**不得**回读 `Library` 行覆盖已解析的结果；`NfoOptions` 与
  `overwrite_video` / `overwrite_image` / `copy_resources` 同路传递。
- 验收以**真实落盘路径**为准：写 `naming` 覆盖后执行整理，磁盘产物的实际路径
  必须按覆盖后的模板生成（含字幕），不得只比较 resolver 返回值。

#### 3.1.4 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/libraries/{id}/overrides` | 返回该库的全部覆盖（按 section） |
| PUT | `/api/libraries/{id}/overrides/{section}` | 整体替换某 section 的覆盖 payload |
| DELETE | `/api/libraries/{id}/overrides/{section}` | 清空某 section（回到继承） |
| GET | `/api/libraries/{id}/effective-config` | 返回合成结果（前端预览用） |

写操作必须校验：section 在已实现白名单内；payload 能被对应 section 模型 `model_validate` 通过。
校验失败返回 422 且不落库。

#### 3.1.5 与现有 Library 字段的关系

`Library` 表已有的模板字段与新的覆盖表**并存**，合成顺序即 3.1.3 的四层。

**覆盖表优先**（用户决策）。理由：覆盖表是显式、用户主动设置的意图，
其表达力高于 `Library` 行的复制式字段。

**必须保证既有库行为不变**。关键事实（已核验）：

- `db/repos/libraries.py::create_library` 中**只有 `video_template` 的默认值是模块常量**
  `VIDEO_TEMPLATE_DEFAULT`；其余 8 个可选模板默认 `None`，由 `resolve_paths` 回退到
  `THUMB_TEMPLATE_DEFAULT` 等常量。
- 因此库行里存的既不是「当前全局值」，也不都能直接当路径用；
  它只是「建库那一刻的输入」，`None` 是其中一个合法输入。
- 既无法区分「用户显式设过」与「默认值」，也无法通过「与当前全局默认比较」还原用户意图 ——
  用户当年显式设成 `VIDEO_TEMPLATE_DEFAULT` 的库会被误判为「未设置」。

因此引入**「显式设置」标记**，且**迁移时保留全部历史生效值**：

- `Library` 表新增一列 `template_overrides: list[str]`，记录哪些模板字段参与第 2 层合成。
- **Alembic 数据迁移**：把每个既有库的**全部 15 个模板类字段**写入 `template_overrides`。
  理由：库行值（含 `None`）就是它当前的生效输入；全部保留即可保证行为不变。
  这比「与全局默认比较」更保守也更正确 —— 不猜用户意图，只保留现状。
- **新建库**时该列为**空** —— 全部模板字段视为继承（第 2 层整层跳过）。
  这是新语义：新建库跟随全局，改全局设置时新库受影响。
- **建库与改库的请求中显式给出某个模板字段** → 把该字段名加入此列。
  包括 `POST /api/libraries` 的建库请求：否则用户建库时自定义的模板会被静默忽略。
  判定「显式给出」用请求模型的 `model_fields_set`：请求模型由 FastAPI 直接从
  请求体 JSON 构造，不经 `model_dump` 往返，`model_fields_set` 如实反映请求体里的键。
  （任务 payload 不适用同一手段，原因见 3.1.3。）
- 生效时：`Library` 行的某个模板字段，只有名字出现在 `template_overrides` 中才参与合成。
  该列只增不减：字段一旦被标记，后续清空其值写作「采用内建默认」，
  回到继承需要显式 API 操作（从名单移除）。

**验收要求**：

1. 迁移后，既有库的落盘结果与迁移前一致（逐字节）。
2. 升级后再修改全局 `naming` 默认，既有库的落盘结果不变。
3. **升级后为既有库写 `naming` 覆盖，覆盖生效** —— 覆盖表在第 3 层，
   高于名单内的库行，这是本顺序与「覆盖表优先」决策一致的地方。

**例外**：`move_mode` / `min_file_size` / `blacklist_patterns` / `trailer_pattern`
不参与这套机制 —— 它们本来就是显式的库级设置，恒定生效，不被覆盖表覆盖。

#### 3.1.6 用户自定义识别词（P0）

新增 DB 表。**规则只存在这里**，不进 `config_overrides`：

```
parse_rules
  id           PK
  scope_type   TEXT    -- 'global' | 'library'
  scope_id     INTEGER -- 全局记 0, 库级记 libraries.id
  kind         TEXT    -- 'number_keyword' | 'path_keyword'
  content_type TEXT    -- 'censored' | 'uncensored' | 'amateur' | 'chinese' | 'western'
  pattern      TEXT    -- 字面关键词（大小写不敏感）或 r: 前缀的正则
  enabled      BOOLEAN
  created_at, updated_at
  UNIQUE(scope_type, scope_id, kind, content_type, pattern)
```

`scope_id` 用 `0` 表示全局而不是 NULL：SQLite 的 UNIQUE 认为 NULL 互不相等，
用 NULL 会让「同一全局规则重复插入」不被拦截。

**合成规则**（库级覆盖全局，按桶整体替换）：

- 按 `(kind, content_type)` 分桶。
- 某桶在库级有启用规则 → 只用库级的；否则用全局的。

不采用「库级与全局合并后按时间排序」：那会让一条更早的全局规则压过库级规则，
与「库级独立」的目标冲突。

**匹配顺序是扁平的**（第 4 轮修订，替代「桶内首条命中」）：桶只决定**合成**时用哪批行，
不决定匹配次序。合成后的启用规则排成单一序列：

```
number_keyword 全部（按 created_at 升序） → path_keyword 全部（按 created_at 升序）
```

逐条尝试，**首条命中即生效**，该文件的 `content_type` 置为命中规则的 `content_type`。

- 同一 `pattern` 允许出现在多个 `content_type` 桶（唯一约束不禁止），
  谁先命中由 `created_at` 决定 —— 这与 MDC 的「按顺序匹配，首条命中生效」一致，
  也让用户用插入顺序表达优先级，而不是在保存时武断拒绝。
- 正则重叠同理：两条正则都能命中同一文件时，`created_at` 早者生效。
- `content_type` 不同的两条规则之间没有隐式优先级；唯一显式的优先级是
  「番号关键词先于路径关键词」（见下）。

**匹配规则**（对齐 MDC）：

1. 番号关键词段先于路径关键词段：两段都命中时番号胜出。
2. 路径关键词匹配**完整路径**（含文件名）。
3. 命中后**覆盖**内置的类型判定（把该文件判为指定 `content_type`）。
4. 未命中任何规则时，走现有 `parsing/file_info.py` 逻辑。

**路径输入的处理**：`FileInfo` **不含原始路径**（其字段是解析结果），
所以规则匹配不能在 `parse_file_info` 返回之后做，必须在它内部、路径还在手上的时候做。

- `rules.py::apply_rules` 的签名带上路径：
  `apply_rules(path: str | Path | None, info: FileInfo, rules: ParseRuleSet) -> FileInfo`。
- **仅按番号解析（`text=` 分支）时跳过路径规则**：该分支没有路径，
  且现有语义就是「只执行番号规则」。此时只应用番号关键词规则。
- 匹配用调用方传入的**原始路径字符串**，不是 `path_key` 归一后的形式 ——
  归一（NFC / normcase）会改变大小写，而路径关键词是用户按实际目录名写的。
  大小写不敏感由匹配实现自己按 `casefold` 处理。
- `parse_file_info` 在 `path` 分支内构造 `FileInfo` 之后、返回之前调用 `apply_rules`；
  `text` 分支传入 `path=None`。

**实现约束**：`parsing/` 是纯函数包。因此：

- 新增 `src/amane/parsing/rules.py`，定义 `ParseRuleSet`（冻结 dataclass，由 DB 行构造）
  与上述 `apply_rules`。`ParseRuleSet` 内部持有**已合成、已排序**的规则序列
  （番号关键词段 → 路径关键词段，各段按 `created_at` 升序）——
  桶合成与排序在构造时完成，`apply_rules` 只做匹配，不再接触作用域概念。
- `parse_file_info(path=None, *, text=None, has_external_subtitle=False, escape_strings=None)`
  签名不变，新增仅关键字参数 `rules: ParseRuleSet | None = None`。
- `parsing/` 仍然不读配置、不做 I/O —— 规则由调用方从 DB 读出后传入。
- `rules.py` 的匹配逻辑必须是纯函数，便于表测试。

**匹配引擎采用 RE2（ReDoS 防线，第 8 轮修订改定）**：长度上限（256 字符）与
`re.compile` 只能拦截非法表达式；语法树拒绝嵌套量词也拦不住 `(a|aa)+$` ——
交替分支可互换匹配的重复组没有 `REPEAT` 嵌套 `REPEAT` 的形态，却是经典的指数回溯。
语法子集无法给出可证明的安全边界，因此**匹配引擎换用 RE2**
（`google-re2`，加入 `[project].dependencies`）：

- RE2 的匹配时间与输入长度成线性关系，从引擎层面消除灾难回溯，
  不再依赖对 pattern 形状的枚举式拒绝；`Python re` 不再参与识别词匹配。
- RE2 不支持的构造（反向引用 `\1`、环视 `(?<=…)` / `(?!…)` 等）
  在编译期直接报错 → 保存返回 422。已实测：本机 Python 3.14 + Windows wheel
  可安装；`(a|aa)+$` 对长近似匹配输入瞬时完成；反向引用与 lookbehind 编译即拒。
- 长度上限 256 保留，定位是防滥用而非防回溯。
- `google-re2` 是二进制扩展，桌面打包与 Docker 镜像需包含对应 wheel
  （PyPI 覆盖 Windows / macOS / Linux 主流平台，实现时在打包任务中验证）。

**生产入口注入**（联合审核 P2 修订）：`rules=` 必须接入全部真实解析入口，
由 `Runtime` 持有 `ParseRuleSet` 提供者（按库取，含全局合成），调用方注入：

| 入口 | 位置 | 注入 |
|---|---|---|
| 扫描 | `handlers/refresh.py::RefreshHandler`（`scan_inventory` 逐文件解析） | 该库 `ParseRuleSet` |
| 整理落盘 | `handlers/file.py::apply_file_operations` | 该库 `ParseRuleSet` |
| 任务请求解析 | `api/models/tasks.py`（手动整理的番号预解析） | 该库 `ParseRuleSet` |
| 清理清单 | `library/cleanup/footprint.py` | 该库 `ParseRuleSet` |
| 调度 | `scheduler/service.py` 的文件发现解析 | 对应库 `ParseRuleSet` |

不注入的位置：字幕配对（`organize/subtitles.py`，`text=` 分支本就跳过路径规则）、
Alembic 数据迁移（一次性重投影，保持历史口径）。验收以真实扫描流程为准：
两个库对同一番号文件采用不同规则，得到不同 `content_type`。

**缓存与失效**：

- `ParseRuleSet` 按库缓存在 `Runtime`，版本键取该库相关行（含全局）的
  `(count, max(updated_at))`。
- 规则 CRUD 后重建该缓存。规则是 DB 行，不随 `AppRuntime._rebuild`
  （那由配置文件变化触发）；两者都要能触发失效。

#### 3.1.7 前端

- 库详情页新增「配置覆盖」Tab：按 section 分块编辑稀疏覆盖，另有一处「继承全局」开关，
  关闭时删除该 section 的覆盖行。
- 识别词规则管理页：新增 `/parse-rules` 路由。规则是多行记录，且带作用域，
  不并入设置页的 `scraping` section。

### 3.2 命名模板引擎（P1）

#### 3.2.1 双引擎并存

新增 `src/amane/organize/template_jinja.py`，提供 Jinja2 实现。
`src/amane/organize/template.py` 的自研实现保留为默认。

```python
class TemplateBackend(Protocol):
    """渲染命名模板."""

    def render(self, source: str, variables: Mapping[str, str]) -> str: ...
    def validate(self, source: str) -> None: ...
    def describe(self) -> TemplateSyntax: ...
```

实现：

- `NativeTemplateBackend` —— 包装现有 `TemplateEngine`，并**扩展语法**（见 3.2.2）。
- `JinjaTemplateBackend` —— 包装 Jinja2。

**Jinja2 的安全约束**（Amane 渲染的是路径，必须防注入）：

- 使用 `SandboxedEnvironment`（`jinja2.sandbox.SandboxedEnvironment`）。
- 禁用 `attr` / `getitem` 对下划线开头属性的访问（SandboxedEnvironment 默认行为）。
- 输出经 `PathEngine.clean` 的路径段规范化（去段尾 `.`/空格、丢空段）。
- 渲染结果仍须过 `PathEngine.resolve` 的逃逸防护。

**Jinja2 是主依赖**（用户决策）：加进 `pyproject.toml` 的 `[project].dependencies`。
理由：体积小，且用户在模板里用了 Jinja2 语法后缺依赖会导致任务失败而非降级；
主依赖可避免「已保存的 Jinja2 模板在换机或升级后突然不能用」。

**运行时分派**（联合审核 P2 修订）：`naming.syntax` 必须接入全部真实渲染入口，
由统一的分派函数按生效 `NamingConfig.syntax` 选 backend，调用点覆盖：

| 渲染入口 | 位置 |
|---|---|
| 视频 / 链接 / 附属产物 | `organize/path_templates.py::resolve_paths`（及 `resolve_sidecar_paths`） |
| **字幕落盘路径** | `organize/path_templates.py::resolve_subtitle_path`（`library.subtitle_template`，经 `organize/subtitles.py::place_subtitles` 调用） |
| STRM 正文 | `organize/strm_content.py::render_strm_content` |
| 剧照文件名 | `organize/path_templates.py::render_extrafanart_names` |
| 路径试算 | `organize/path_templates.py::render_path_template` |

**字幕的精确边界**（第 8 轮修订补正）：字幕**配对**（`organize/subtitles.py` 里识别
哪个字幕文件属于哪个视频，`parse_file_info(text=…)`）不渲染模板，确实不在分派范围；
但字幕**落盘路径**（`place_subtitles → resolve_subtitle_path`）渲染
`subtitle_template` 且当前直接用原生 `PathEngine`——该入口必须分派，
并从生效 `NamingConfig` 取 `subtitle_template`，否则字幕覆盖配置与 Jinja 模板
都不生效。验收断言**磁盘上的实际字幕路径**。

验收走完整链路：保存 Jinja 模板 → 执行整理 → 检查磁盘实际产物路径与文件名
（含字幕）；任何入口漏接，该入口下 Jinja 模板会把 `{{ }}` 当字面量落盘。

#### 3.2.2 自研语法扩展

**`|` 不能用作 filter 分隔符**。现有 `_parse_placeholder` 用 `body.partition("|")`
按**首个** `|` 切分，右侧整体交给 `_parse_placeholder_mapping`：

- `{name|upper}` 现在抛「路径模板里的占位符映射无效」（右侧无 `=`）。
- `{title|truncate=10}` 现在是**合法**的值映射（`title` 不在 `PLACEHOLDER_MAP_KEYS`，
  任意 key 接受），渲染时静默无效果。
- `{name|k=v|upper}` 会把 `v|upper` 整体当作映射值，filter 不生效。

若把 filter 叠在 `|` 上，第 2 条会变成不兼容的语义变更，第 3 条则永远无法生效。
因此 filter 用 **`:`** 分隔，`|` 继续只做值映射。

补三项能力：

| 能力 | 语法 | 语义 |
|---|---|---|
| 条件块 | `{% if a,b %}…{% else %}…{% endif %}` | 条件只能是变量名或逗号分隔的变量名列表；**全部有值**时为真 |
| filter | `{title:upper:truncate(20)}` | 从左到右依次应用 |
| 降级链 | `{a:default(b:default("未知"))}` | `default` 接受表达式参数，从内向外求值 |

语法形状：

```
expr    ::= var (":" filter)*
filter  ::= name | name "(" args ")"
args    ::= arg ("," arg)*
arg     ::= quoted-string | expr
```

- 字符串字面量必须加双引号，`replace("a,b","c")` 才能含逗号。
- 未加引号的参数不能含 `,` 与 `)`。
- 条件块不提供 `elif`，也不支持运算符与函数调用 —— 模板由用户在 Web UI 编辑，
  必须可静态分析（前端高亮、校验、试算），且要杜绝求值注入。

**filter 集合**：

| filter | 语义 |
|---|---|
| `default(x)` | 无值时用 `x`（`x` 可为嵌套表达式或字面量） |
| `upper` / `lower` | 大小写转换 |
| `truncate(n)` | 截断到 n 字符（**按 UTF-8 字节**计，与 `_clip_field` 一致） |
| `split("-")` | 拆分，返回列表；只能与 `first` / `last` 组合 |
| `first` / `last` | 取列表首/末项 |
| `replace(a, b)` | 字符串替换 |
| `trim` | 去首尾空格 |

**「有值」的判定**（沿用 Amane 现状）：

```
None / 空字符串 / 空列表 / 字符串 "Unknown" / 字符串 "None"  →  无值
其它 → 有值
```

注意 `_build_variables` 把缺失的元数据字段填成字面量 `"Unknown"`，
所以 `{studio|=未分类}` 不会把 `Unknown` 变成默认值，需要写 `{studio|Unknown=未分类}`
或改用 `{studio:default("未分类")}`。`default` 按上面的「有值」判定工作，覆盖 `Unknown`。

**Jinja2 引擎的语义差异**：

| 能力 | 自研引擎 | Jinja2 原生 | 处理 |
|---|---|---|---|
| 变量缺失 | 无值（按上述定义） | `Undefined` | 自定义 filter 统一处理 |
| `default(x)` | 无值时用 `x`（含空串、空列表、`Unknown`） | **只对 undefined 生效**，空字符串会原样返回 | **不用原生 `default`**，自建同名 filter |
| `truncate(n)` | 截断到 n **UTF-8 字节**，无省略号 | 截断到 n **字符**，带 `...`，有 `leeway` | **不用原生 `truncate`**，自建同名 filter |
| `upper` / `lower` | 大小写转换 | 同 | 直接用原生 |
| `trim` | 去首尾空格 | 同 | 直接用原生 |
| `replace(a, b)` | 字符串替换 | 同 | 直接用原生 |
| `split` / `first` / `last` | 自研实现 | Jinja2 无 `split` | 自建 `split`，`first`/`last` 用原生 |

结论：**两套引擎共用同一套「兼容 filter」**，通过在 `SandboxedEnvironment`
上注册同名 filter 实现，一致性测试才有意义。

**不一致之处仅一处**：Jinja2 引擎下变量缺失得到空字符串（`Undefined`），
自研引擎下按「有值」判定为无值。与 MDC 的 Jinja2 行为一致，属**有意差异**，
一致性测试排除此项。

**保存时校验占位符名**：现有 `Parser` 解析阶段不校验占位符名，
未知名字在渲染时输出 `Unknown`。引入 filter 语法后，拼写错误会静默产出 `Unknown`，
因此 `validate` 必须拒绝不在 `PLACEHOLDERS ∪ {"dir"}` 内的占位符名与未定义的 filter。

- 该约束只作用于**保存与切换**路径；已落盘的渲染行为不变。
- 既有模板若含未知占位符，下次保存时会被拒绝并指出位置。

**引入 `:` 与 `{%` 的兼容边界**：现有模板中若出现 `{...:...}` 或 `{%`，
今天会渲染成 `Unknown`（占位符名未知），引入新语法后语义改变。
这类模板本就不产生有效路径，保存时的占位符校验会一并拒绝。

#### 3.2.3 模板来源与语法标识

语法是**每库一份**的配置，只存在 `naming.syntax` 一处，不另设 `Library.template_syntax` 列：
两处存同一事实会产生不一致。

**切换校验不能只看「解析成功」**：

Jinja2 会把自研语法的 `{number}` 当**普通文本**解析 —— 解析**成功**，
但渲染结果是字面量 `{number}`。因此「能解析」不能证明模板有效。

切换时必须做**语义验证**：

1. 用目标引擎解析全部生效模板：`video` / `link` / `strm_content` / 8 个附属模板，
   以及继承链上所有生效模板（库行名单内的、覆盖表里的、最终落到全局默认的）。
2. 对每个模板做**渲染探针**：用一组**覆盖全部 30 个占位符**的样例变量渲染，
   检查输出中不含未替换的占位符残留。
   - 自研语法：残留 `{` + 标识符 + `}` 形态。
   - Jinja2：残留 `{{` / `{%`，以及自研占位符形态（`{number}` 这类）。
3. 任一模板未通过 → **拒绝切换**，返回 422 + 具体模板与残留位置。

**两套默认模板**：

```
自研默认（现有，保持不变）：
  {studio}/{number}/{number}[-CD{cd?}][-{sub?}].{ext}

Jinja2 默认（新增）：
  {{ studio }}/{{ number }}/{{ number }}{% if cd %}-CD{{ cd }}{% endif %}{% if sub %}-{{ sub }}{% endif %}.{{ ext }}
```

**Jinja2 引擎的变量名必须去掉 `?`**。

`?` 在 Jinja2 里是非法标识符字符：`{{ cd? }}` 与 `{% if cd? %}` 都在**解析阶段**
抛 `TemplateSyntaxError: unexpected char '?'`。因此 4 个带 `?` 的占位符在 Jinja2 下
不是「名字写错」而是**根本无法表达**。

`JinjaTemplateBackend` 在渲染前建立变量映射（4 个重命名，其余同名）：

| 自研占位符 | Jinja2 变量名 | 说明 |
|---|---|---|
| `cd?` | `cd` | 分集号 |
| `sub?` | `sub` | 中字标记，值为 `"C"` 或空 |
| `mosaic?` | `mosaic` | 马赛克性质 |
| `def?` | `definition` | 分辨率（`def` 不是合法 Python 标识符，也不宜作变量名） |
| 其余 26 个 | 同名 | 含 `dir` 别名 |

映射只作用于**变量字典的键**；模板源码里的 `?` 形式在 Jinja 语法下无法出现，
所以无需处理「用户写了 `{{ cd? }}`」的兼容 —— 那会被解析期报错拦下。

**两套默认模板的等价验收**：对同一组变量（含「有分集」「无分集」「有中字」「无中字」
「分集 + 中字」四种组合），两个引擎渲染出的路径必须**完全相同**。
这四组覆盖了 `[-CD{n}]` 与 `[-C]` 两个可选组的全部真值组合，
是多分集/多字幕文件得到不同目标路径的前提。

两套默认值都由 `/api/libraries/path-template-schema` 端点按 syntax 分别下发；
前端切换语法时，若用户未自定义过该模板，自动替换为对应默认。

`path-template-schema` 当前响应为 `video_default` / `optional_defaults` / `placeholders` /
`subtitle_extensions_default`，新增 `syntaxes` 字段，列出可用语法、各自默认模板、
占位符与能力。`placeholders` 需补上 `dir` 别名，并按 syntax 给出对应的 Jinja2 变量名。

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

MDC 的 NFO 有 `OriginalTitle` 与 `OriginalPlot`。
Amane 现有 `Metadata` 只有 `title` / `plot`（单一轨，翻译后就覆盖了原文）。

**调研结论**：原文数据在多数源天然可得，只是当前被压平或丢弃。

#### 3.3.1 字段语义

| 字段 | 语义 | 来源 |
|---|---|---|
| `title` | 翻译后（中文） | 现有链路：源站值 → LLM 翻译 |
| `original_title` | **源站原文**（通常日文） | 新增 |
| `plot` | 翻译后（中文） | 现有链路 |
| `original_plot` | **源站原文**（通常日文） | 新增 |
| `website` | 主来源详情页 URL | 新增单值列 |

**与 MDC 的命名差异**：MDC 的 `OriginalPlot` 实际是**中文剧情**（等于 Amane 的 `plot`）。
Amane 的 `original_plot` 明确定义为**源站原文**，以免继承 MDC 的歧义。

#### 3.3.2 数据可得性（调研实测）

| 源 | 原标题 | 原文简介 | 说明 |
|---|---|---|---|
| **DMM** | ✅ GraphQL `ppvContent.title` 实测**纯日文** | ✅ `ppvContent.description` 实测日文 | 无多语言维度 |
| **MGStage** | ✅ `h1` 日文 | ✅ `dl#introduction` 日文 | 日文站 |
| **official**（厂牌官网） | ✅ `//title/text()` | ✅ `p-workPage__text` | 日文官网 |
| **wp_works**（faleno/dahlia） | ✅ ACF `作品名` | — | 日文官网 |
| **avsox** | ✅ `title_ja` **已在模型里** | ✅ `description_ja` **已在模型里** | 被 `_first` 压平 |
| **r18dev** | ✅ `title_ja` **已在模型里** | ✅ `comment_ja` **已在模型里** | mapper 按请求语言二选一 |
| javlibrary / jav321 / xcity / giga / prestige | ✅ 站点即日文 | 部分 | — |
| javdb | ⚠️ 需 `?locale=ja`（当前固定 `locale=zh`） | ❌ 不解析 plot | — |
| javbus | ⚠️ 需 `Accept-Language: ja`（当前固定 `zh-CN`） | ❌ 不解析 plot | — |
| iqqtv | ⚠️ 需 `/jp` 前缀（`_LANG_PREFIX` 已备好） | ⚠️ 同 | — |
| getchu / kin8 / fc2* / airav / freejavbt / theporndb | ❌ | ❌ | 无原标题概念 |

**结论**：`original_title` 对**有码片**（DMM 路由首位）几乎必然可得；
`original_plot` 对 DMM / MGStage / official 可得，对 Javdb / Javbus / Javlibrary 不可得。

#### 3.3.3 顺带修复的两个既有缺陷

1. **avsox 未声明 `SourceTrait.MULTI_LANGUAGE`** — `crawlers/sites/avsox.py::AvsoxCrawler.profile`
   只返回 `name` / `base_url`，缺 `traits`，因此不在 `MULTI_LANGUAGE_SITES` 中
   （该集合由 `crawlers/site_roles.py` 按已注册站点的 `profile().traits` 推导，
   当前只有 IQQTV 与 R18DEV）。
   引擎因而不会为它生成带语言的节点，`_fetch_one` 传入 `options=None`，
   `avsox.py::_lang` 恒返回 `"cn"`，`title_ja` / `description_ja` 只在
  默认语言字段为空时才作为回退被取到。这是死代码。
   修复：补 `traits=frozenset({SourceTrait.MULTI_LANGUAGE})`。

2. **avsox 的 `_first` 顺序把中文排在日文前** — `avsox.py::_scrape` 的
   `plot=_first(movie.description, movie.description_cn, movie.description_ja, ...)`。
   即使拿到日文轨也会先取中文。
   注意标题的 `_first` 顺序是 `title, title_ja, title_en, title_cn, title_tw`，
   日文排在中文之前，只有简介的链需要修正。
   修复：改为按 `language` 精确取，并保留 `_ja` 轨。

**这两处修复是双轨实现的前置条件**，必须先做。

#### 3.3.4 实现方案

**爬虫层**：

- `crawlers/models.py::MediaMetadata` 新增
  `original_title: str | None = None`、`original_plot: str | None = None`、
  `field_languages: dict[str, str] | None = None`。
  **必须有默认值** —— `aggregate/engine.py::_fetch_one` 用
  `MediaMetadata(**cached)` 反序列化存量 `raw` 快照，缺字段时 Pydantic v2 抛
  `ValidationError`，而该处只 `except TypeError`，兜不住，任务会中断。
- 各源在返回时同时给出两侧的字段与**逐字段的语言声明**：
  - DMM / MGStage / official / wp_works / javlibrary / jav321 / xcity / giga / prestige：
    `original_title = title`，`field_languages = {"original_title": "ja", "original_plot": "ja"}`。
  - avsox：`original_title = movie.title_ja`，`original_plot = movie.description_ja`；
    `title` / `plot` 仍按请求 `language` 取对应语言。
  - r18dev：`original_title = v.title_ja`，`original_plot = v.comment_ja`。
  - Javdb / Javbus / iqqtv：**本阶段不改**（需改 locale/请求头，会影响既有抓取行为），
    不声明 `field_languages`，`original_*` 留空。

**语言判定必须按字段，不能全局一个值**：

`aggregate/engine.py::_resolve_scalars` 按字段独立沿 `field_chains[field]` 取值 ——
标题可能来自 DMM（日文），剧情可能来自 airav（中文）。
若用一个全局 `source_language`，会出现「标题日文、剧情中文」时把中文剧情写进
`original_plot` 的错误。

因此**按字段记录来源语言**：

- 爬虫用 `MediaMetadata.field_languages` 表达「本源的标题是日文、剧情是英文」这类混合情况。
- 聚合层在 `_fill_scalar` 记录字段来源的同时，记录该字段的实际语言：
  取该源的 `field_languages[field]`，写入 `AggregatedMetadata.field_languages`。
- **不引入源级的全局语言标记** —— 它无法表达按字段的语言，混合来源下会误判。
- 源未声明某字段的语言 → 该字段不参与原文聚合（宁可留空，不可猜）。

**原文在聚合阶段确定，而非翻译阶段**：

`original_title` / `original_plot` 是**独立字段**，各自参与聚合、各有自己的字段链
（由 `field_priority` / `content_routes` 编译得出）：

1. `enums.py::MetadataField` 新增 `ORIGINAL_TITLE` / `ORIGINAL_PLOT`，值等于 `MediaMetadata` 属性名
   （`tests/crawlers/test_enum_consistency.py` 断言每个枚举成员都能在
   `MediaMetadata.model_fields` 找到同名键）。
2. `aggregate/engine.py::SCALAR_FIELDS` 加入这两个字段，它们随之进入 `ALL_FIELDS`，
   字段链与 `field_priority` 覆盖自动生效。
3. `aggregate/models.py::AggregatedMetadata` 新增
   `original_title: str | None` / `original_plot: str | None` / `website: str | None` /
   `field_languages: dict[str, str]`。
4. `_normalize_source_text` 与 `db/repos/metadata.py::_normalize_text_fields`
   为 `original_plot` 加长文本归一（当前只归一 `plot`）。
5. `field_sources` 由 `_fill_scalar` 自动记录；`field_languages` 同时记录。

**`website` 的取值方式**：

爬虫侧**没有** `website` 字段，只有 `source_url`（各站详情页 URL，单值）。
因此 `website` **不是**聚合字段，也不进 `MetadataField`：

- **不新增** `MetadataField.WEBSITE`。该枚举被一致性测试逐个校验必须在
  `MediaMetadata` 里有同名字段，而 `MediaMetadata` 不会加 `website`
  （它没有爬虫侧来源，加了就是无意义字段）。是否进入 `SCALAR_FIELDS` 与这条断言无关。
- `website` 只在 `AggregatedMetadata` / `Metadata` / API / NFO 出现，是**派生字段**。

**DB 层**：

- `Metadata` 加四列：`original_title` / `original_plot` / `website` / `field_languages`（JSON 字典）。
- `db/repo_types.py::MetadataFields` TypedDict 加四个键（必改，静态检查会报错）。
  `upsert_metadata` 已按 `MetadataFields` 键循环 `setattr`，新键自动生效；
  `update_metadata` **显式逐字段赋值**（`docs/dev/data-model.md`），必须手加四行。
- **`handlers/scrape.py::ScrapeHandler.handle` 的 `upsert_metadata` 调用显式补四参数**
  （`original_title=result.metadata.original_title` 等四项）—— 该调用是显式逐参数传名，
  新字段不会自动进入；漏接则原文永远不落库。
  验收为集成测试：执行一次刮削任务后，从 DB 与 `GET /api/metadata/{id}` 都能读回四字段。
- Alembic migration 用 `uv run alembic revision --autogenerate -m "..."` 生成，
  SQLite 加可空列用 `op.batch_alter_table`。

**API 层**：

- `api/models/metadata.py::MetadataResponse` **手写**，必须手动加四行。
- `PartialMetadata` 由 `create_partial_model(Metadata, ...)` 自动派生，无需改动。
- `website` 作为只读字段：加入 `create_partial_model` 的 `ignore_fields`
  （它由聚合派生，不接受 PATCH 写入）。

**前端层**（`assertExhaustive` / `exhaustiveTuple` 在漏改时编译失败）：

- `web/src/lib/exhaustive-maps.ts::METADATA_FIELDS`（用 `exhaustiveTuple`）
- `web/src/lib/media/metadata-fields.ts::LOCKABLE_FIELDS` + `FIELD_LABEL_KEY`
- `web/src/components/metadata/merge-dialog.tsx::TEXT_FIELDS`（及 `MEDIA_FIELDS` / `SCORE_FIELD`）
- `web/src/routes/meta.$metadataId.tsx` 展示原标题 / 原文简介 / 官网链接
- i18n：`web/src/i18n/locales/{zh-CN,en}/metadata.json`

注意：原文字段是普通 `MetadataField` 成员，会同时要求加入 `LOCKABLE_FIELDS`
与 `TEXT_FIELDS`；`website` 不是枚举成员，只在详情页展示。

#### 3.3.5 `website` 单值列

**唯一取值规则**：

```
website = source_urls[field_sources[TITLE]]
```

即「标题胜出来源的详情页 URL」。两者 key 空间一致：`field_sources` 记录的是
`cache_key`，`source_urls` 也按 `cache_key` 收集（多语言站为 `site:lang`）。

- 标题来源没有 `source_url`（如 r18dev）→ `website` 为 `None`，**不沿链回退**。
  回退会让「官网链接」指向一个并非标题来源的站，与「主来源」语义不符。
- `source_urls` 保留全量不变。
- 落库时 `website` 与 `source_urls` 同时写。

#### 3.3.6 NFO 收益

`media/nfo.py::write_nfo` 当前把 `<title>` 的同值写进 `<originaltitle>`，
`<outline>` 与 `<plot>` 同值 —— 这是既有缺陷。加 `original_title` 后，
`<originaltitle>` 可写真实原标题，`<outline>` 可取摘要（均为开关，见 3.4.3）。

#### 3.3.7 影响面清单

以 `title` 为参照，`original_title` 的完整链路需改动：

| 段 | 位置 |
|---|---|
| 爬虫取值 | `crawlers/sites/dmm.py::DmmCrawler._scrape_digital` 等各源 `_scrape` |
| 爬虫模型 | `crawlers/models.py::MediaMetadata` |
| 站点快照 | `aggregate/engine.py::aggregate` |
| 聚合取值 | `aggregate/engine.py::_resolve_scalars` → `_fill_scalar` |
| 翻译 | `handlers/scrape.py::_translate_metadata` |
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
| 前端 | `meta.$metadataId.tsx` + 三处穷尽检查 + i18n |

**注意**：`MetadataField` 新增值后，以下两处**不会自动生效**，需手动处理：

- `config/manager.py::LANG_METADATA_FIELD_SET` — 新字段若要参与多语言请求展开，
  必须同时加进此集合（`_resolve_lang` 的两个条件是 AND）。
  本阶段**不加** —— 原文字段不走多语言请求展开。
- `ScrapingConfig.field_priority` / `field_blacklist` — 两者是普通 dict，
  key 为 `MetadataField`，**没有** `x-frozen-keys`（`tests/config/test_site_roles.py`
  显式断言），也不经 `_complete_frozen_dict`；它们走 `_validate_film_field_map`
  与 `_migrate_priority`。因此新枚举成员**不需要**改任何键集，
  用户即可配置「原标题优先取 DMM 还是 MGStage」。

**翻译层**：

- `handlers/scrape.py::_translate_metadata` 当前就地覆盖 `title` / `plot`（仅当译文非空）。
  保持该行为：**只翻译 `title` / `plot`，不碰 `original_title` / `original_plot`**。
- 翻译前**不再**用「翻译前的快照」兜底填原文字段 ——
  那个做法在混合来源下会出错。原文字段为空就留空。
- 翻译**失败**时：`title` / `plot` 保持源站原值不变（现有行为），
  `original_*` 独立存在 —— 两者都是原文，不冲突。

### 3.4 整理（P1）

#### 3.4.1 整理模式

`src/amane/enums.py::MoveMode` 增加 `INPLACE = "inplace"`。

语义：**源文件不动，在源文件所在目录内生成整理结果**（图片、NFO、字幕）。

实现要点：

- `organize/file.py::execute_organize` 的 `match mode:` 增加 `inplace` 分支 —— 不移动视频。
  **必须加分支**：该 `match` 没有兜底，新增枚举成员而漏加分支会静默落空并返回 PLACED。
- 视频的目标路径 = 源路径（不改名），`video_template` 在此模式下**被忽略**。
  因此 `resolve_paths` 需要 inplace 分支：直接用 `source_path` 作为 `video`，
  并跳过「视频模板解析为库根目录」的守卫。
- 图片/NFO 的落盘位置按模板渲染，**基准目录**是视频所在目录（`apply_video(source)` 之后正常渲染）。
- `handlers/file.py::OrganizeHandler` 的 `collect_prune` 条件是
  `prune_empty_dirs and move_mode is MoveMode.MOVE`，inplace 天然不满足，无需修改。

迁移：`MoveMode` 以字符串落库，列上无 CHECK 约束，无需 Alembic revision。

#### 3.4.2 覆写策略

`common` section 两个开关（见 3.1.2）：`overwrite_video` 默认 `False`、`overwrite_image` 默认 `True`
—— **保持现有行为**（视频冲突即跳过；图片现状就是覆盖写，见 `handlers/file.py::_place_library_images`）。

**接入现有冲突分支**（联合审核 P2 修订）：现有
`handlers/file.py::execute_file_operations` 在落盘前预检 `OCCUPIED` 即返回
`CONFLICT`。分派规则：

- `common.overwrite_video=False`（默认）→ 现状不变：`OCCUPIED` 记 `CONFLICT` 跳过。
- `common.overwrite_video=True` → `OCCUPIED` 进入覆写协议（阶段 A 起），
  不再记 `CONFLICT`；阶段 A 的前置检查（目标存在、是视频、在库内、不同于源）
  就是该预检的延伸。
- `common.overwrite_image` 传入图片写入流程：`True`（默认）维持覆盖写；
  `False` 时目标已有图片则跳过并记 `skipped`。

覆写路径与跳过路径共用同一次 `target_state` 预检，分派只发生在预检结果之后。

##### 覆写的事务性协议

覆写**不能**是「先删目标、再放源」。必须保证任一步失败时磁盘上仍有可用视频。

**分区段的判据是磁盘事实，不是 journal 状态**：

**提交点 = 目标路径上已经出现新内容的那一刻**（`os.replace` 返回成功）。
它与「journal 是否已更新为 `placed`」无关 —— journal 是一次数据库写，
可能失败、可能被中断，而磁盘事实已经发生。因此：

- 提交点之前（阶段 A，以及阶段 B 中 replace 未成功）→ 回滚，目标恢复为旧内容。
- 提交点之后（replace 已成功）→ **任何失败都不删除目标**，包括 journal 更新失败。
  源内容的副本此时只在目标路径上（MOVE 模式下源路径已消失），删目标即删数据。

```
阶段 A 预备
  1. 确认目标存在、是视频文件、在库内、且不同于源
  2. 写 journal 行（独立提交, 见下）: phase=prepared, mode, 源路径与内容指纹
     （oshash / size / metadata_id / number —— 此刻源行完好, 是取指纹的唯一时机）
  3. 把目标重命名为同目录备份 <目标名>.amane-overwrite-<任务id>
     （同目录 → 同一文件系统 → rename 原子）

阶段 B 落位
  4. move    : 同文件系统 os.replace(源, 目标)
               跨文件系统(EXDEV) 复制到 <目标名>.amane-tmp-<任务id>, 再 os.replace 到目标
               （跨文件系统时源保留, 交 committed 清理分支删除）
     copy    : 复制到临时名, 再 os.replace 到目标
     hardlink: os.link(源, 临时名), 再 os.replace 到目标
     symlink : 在临时名建符号链接, 再 os.replace 到目标
     >>> os.replace 返回成功即提交点 <<<

阶段 C 接管与清理（提交点之后, 任何失败都不回滚）
  5. 尽力写 journal 行 phase=placed（失败不回滚, 恢复从 prepared 分支走）
  6. 目标核验: 对 target_path 现场 compute_oshash + stat size,
     与 journal 全等才继续; 不通过 → 保留源与备份与 journal,
     写 last_error, 转人工处理, 不接管不删源
  7. 索引接管 + journal 行 phase=committed, 同一数据库事务（见下）
  8. 删除备份
  9. 删源前再核验一次目标（同第 6 步, 覆盖接管与删备份之间的窗口）,
     通过后: mode = move 且源路径仍存在 → 对源磁盘文件现场重算 oshash + size,
     与 journal 全等才删除源
  10. 删除 journal 行
```

回滚（**仅**提交点之前）：

```
删除临时文件（若存在）
把备份恢复为原目标名
删除 journal 行
任务记 failed + 回滚原因
```

约束：

- **journal 更新失败不得删除目标**（第 4 轮 P1 的修订基础）。
  replace 成功后若相位推进写失败（DB 锁、磁盘满、进程中断），
  保留 `prepared` 记录、**不删目标**，任务记 `failed` 并把原因写入结果。
  恢复扫描按 `prepared` 处理（见下）。
- 阶段 C 的每一步失败都只记日志与告警，不回滚。包括：
  - 索引接管失败 → journal 仍是 `prepared`，恢复扫描重试接管；
  - 删除备份失败 → 备份残留，journal 保留，恢复扫描下次清理；
  - journal 删除失败 → 下次扫描重复处理，各步幂等。
- **MOVE 的回滚边界**：`os.replace` 成功后源路径已消失。若删除备份失败，
  **不得**删除目标 —— 那是源内容的唯一副本。
- **跨文件系统**：`Path.move` 内部先 `os.replace`，遇 `EXDEV` 才回退 copy+delete，
  且 `Path.copy` 直接写目标名、不保证原子。覆写路径**不用** `Path.move`，
  按阶段 B 的显式步骤执行：临时名必须在**目标目录内**（同文件系统），
  先写完临时文件再 `os.replace`，最后才删除源。
- **磁盘不足**（copy 模式）发生在写临时文件阶段 → 目标未被触碰，恢复备份即可。
- **权限失败** 发生在 rename/replace 阶段 → 同上。
- 备份名与临时名带任务 id，跨任务不冲突，便于事后清理。
- 覆写**永不**跨越库边界（`safe_dirs` 检查照常生效）。
- 覆写决策记入任务结果（新增计数 `overwritten`），并在日志中留痕。
- 覆写**不涉及**字幕与图片的独立备份 —— 图片是覆盖写（内容可重新下载），
  字幕是同名覆盖，风险低于视频。
- `overwrite_image=False` 时，图片改为「目标已存在则跳过」，记入 `skipped` 计数。
  这是本阶段唯一改变图片落盘行为的开关，默认值保持现状。

##### 中断恢复：`organize_journal`

落位与接管之间无法靠磁盘状态推断（「目标存在」既可能是已完成、也可能未接管），
因此需要持久化的操作状态。

```
organize_journal
  id             PK
  task_id        INTEGER  -- 冗余, 便于按任务查
  mode           TEXT     -- 'move' | 'copy' | 'hardlink' | 'symlink' (MoveMode 成员名)
  source_path    TEXT     -- 源的库内路径（整理前）
  source_file_id INTEGER  -- 源的 MediaFile.id; 仅作线索, 不是身份 (见下)
  source_oshash  TEXT | NULL  -- 覆写前源行的 oshash; 恢复时的内容指纹
  source_size    INTEGER | NULL -- 覆写前源行的 size
  source_metadata_id INTEGER | NULL -- 覆写前源行的 metadata_id
  source_number  TEXT | NULL  -- 覆写前源行的 number
  target_path    TEXT
  target_file_id INTEGER | NULL -- 目标旧索引的 MediaFile.id; 可为 NULL, 见下
  backup_path    TEXT
  phase          TEXT     -- 'prepared' | 'placed' | 'committed'
  last_error     TEXT | NULL
  created_at, updated_at
  UNIQUE(source_path, target_path)
```

**`target_file_id` 可以为 NULL**：覆写的前置条件只要求**目标路径上有视频文件**，
不要求该文件已被索引（例如库里手工放进去、尚未扫描的文件）。
因此「目标旧索引」是可选输入，不能作为恢复的前提。

**`source_file_id` 只是线索，不是身份**（第 4 轮修订指出 id 会复用；第 5 轮修订补足指纹证据要求）：
`media_files` 建表语句没有 `AUTOINCREMENT`
（`3a0088a88ce7_initial_schema.py`：`sa.Column("id", sa.Integer())` +
`sa.PrimaryKeyConstraint("id")`）。SQLite 的语义是：无 `AUTOINCREMENT` 时新 rowid
取 `max(rowid) + 1`，删除最大 rowid 的行后该值会被**复用**
（[SQLite AUTOINCREMENT](https://www.sqlite.org/autoinc.html)）。
源索引行可能被扫描清除（path 失效即删行），随后同 id 会被新登记的文件拿到 ——
恢复时按 id 取行，取到的可能是另一个文件。

因此恢复时的身份判定是**内容指纹复验**。指纹三字段**全部必填、全等才命中**：

```
指纹 = (path, oshash, size)
复验规则: path、oshash、size 三项必须全部相等。没有 NULL 跳过分支。
```

**journal 中不存在空指纹**。`compute_oshash`（`src/amane/utils/oshash.py`）对
小于 128 KiB 或不可读的文件返回 `None`，源行的 `oshash` / `size` 也可能尚未填充 ——
因此阶段 A 在写 journal 前先**现场补算**：

```
读源行 → oshash 为 NULL 时对源文件调 compute_oshash,
         size 为 NULL 时对源文件 stat 取 st_size
       → 补算结果写入 journal（同时写回源行, 接管后字段填充复用）
       → 补算后 oshash 仍为 NULL（文件 < 128 KiB 或不可读）
         → 放弃覆写, 任务记 failed（视频文件不该这么小, 不构成有效覆写对象）
```

由此 journal 的 `source_oshash` / `source_size` 恒为非 NULL，恢复时不存在
「双方都为 NULL 就跳过比较」的分支 —— 那个分支正是「等大小文件占用原路径 +
哈希为空」误通过校验的漏洞。

**指纹的精度边界**：oshash 取「文件大小 + 首尾各 64 KiB 按小端 int64 累加」，
能区分同大小不同内容的文件（首尾不同即不等）；两个文件大小相同且首尾 64 KiB
完全一致、仅中间内容不同时，oshash 理论上相同。这是项目现有的文件身份口径
（`MediaFile.oshash` 同源），本阶段沿用，不另行引入全量哈希。

恢复时按下面的次序找行，每条候选行都必须通过指纹复验，不通过即换下一条：

```
1. 按 source_file_id 取行, 复验指纹
2. 按 source_path 取行, 复验指纹
3. 都取不到或都不匹配 → 接管失败: 保留 journal 行, 写 last_error, 记日志
```

**指纹不匹配时禁止接管**：说明磁盘或索引已经与覆写时的状态脱节
（id 被复用、路径被其他文件占用、或源行被重建）。
此时把任何行的 `metadata_id` 写到目标上都是在转移别人的归属。
保留记录、写明复验失败的具体原因（哪条候选、哪些字段不一致），交人工处理。

写入时机：阶段 A 的 journal 行必须**独立提交**（OS 操作无法加入 DB 事务）；
指纹在写入时从源行读取（必要时现场补算，见上）。

恢复规则（应用启动时的恢复扫描 + 整理同一目标前的就地恢复）：

| phase | 磁盘状态 | 恢复动作 |
|---|---|---|
| `prepared` | 目标缺失、备份存在 | 回滚：备份恢复为目标 |
| `prepared` | **目标存在、备份存在** | 无法从磁盘区分目标是新内容还是旧内容，**按新内容处理**：先做目标核验（见下），通过后走接管，成功后清理备份 |
| `prepared` | 目标存在、备份缺失 | 视为回滚已完成或未开始，清理临时文件与 journal 行 |
| `placed` | 目标存在且**目标核验通过** | 按指纹复验找源行（此时接管尚未提交，源行 `path` 仍为 `source_path`）→ **重做索引接管**，成功后进清理 |
| `placed` | 目标存在但**目标核验不通过** | 目标内容已与覆写时不一致（被截断、替换或部分写入）。**禁止接管与删源**：保留源、备份与 journal 行，写 `last_error`（含现场 oshash 与期望值），交人工处理 |
| `placed` | 目标缺失 | 无法接管：目标已被外部删除。恢复备份、删除 journal 行、记日志 |
| `committed` | — | 清理分支（见下） |

**目标副本核验**（第 6 轮修订的 P1）：提交点之后，目标路径上的内容就是源内容，
因此目标的有效性用**同一份 journal 指纹**判定 —— 对目标路径现场
`compute_oshash` + stat size，与 journal 的 `source_oshash` / `source_size`
全等才算有效。接管前核验一次（`placed` 分支），删源前再核验一次
（`committed` 分支）：接管与删源之间可能间隔一次重启，目标在此期间被删除、
截断或替换的情形必须分别拦截 —— 目标行 oshash 的事后重算不能替代这次核对，
因为接管时写入的是「重算当时」的值，无法证明**此刻**磁盘上的文件仍是覆写的那份。

**接管的提交边界**（第 5 轮修订）：索引接管（删除目标旧索引行、源行 `path`
改目标路径、按表更新字段）与 journal 行 `phase=committed` 的更新在**同一个
数据库事务**内提交。两者之间不存在可中断的间隙 —— 「接管已提交、journal 仍是
`placed`」的状态在数据库里不可能出现。

恢复另设**后置复验**兜底：若候选源行通过指纹复验（id 与 path 线索之外，
oshash / size 也一致）且其 `path` 已等于 `target_path`，视为接管已完成，
进入清理分支的目标核验，不再按「身份不匹配」处理。该分支防御两类情况：
接管事务与 journal 更新曾被拆开的旧版本数据；以及同 id 新行恰好落在目标路径的
极端组合（此时指纹必须全等才放行）。

`prepared` 且目标存在时按新内容处理，是因为该状态只可能由「replace 已完成、journal 未更新」或
「回滚已恢复」产生；前者占多数，且接管可幂等重做。

**接管失败不删除 journal 行**。以下情况都**保留记录**并写入 `last_error`：

- 指纹复验全部失败（id 复用 / 路径复用 / 源行被清除）；
- 目标行取不到且 `target_file_id` 非 NULL；
- 接管事务抛错。

保留而非删除，是因为 journal 行是**唯一能说明「磁盘上这个文件来自哪次覆写」的记录**。
删掉它之后，磁盘上就只有「目标路径上有内容、索引归属不明」这一无法自愈的状态。
下次启动或下次整理同一目标时会重试；连续失败时每轮记日志，供人工核查。

只有以下情况才删除 journal 行：恢复动作全部成功、或其他相位已确定无需处理。

`target_file_id` 为 NULL 的接管：把源行的 `path` 改为目标路径，
不删除任何目标旧行 —— 因为不存在旧行。若按路径查到了新的目标行（例如扫描已入库），
则按有旧行的规则处理。

**committed 的清理分支**（含跨文件系统 MOVE 的源删除）：

```
1. 目标核验: 对 target_path 现场 compute_oshash + stat size,
   与 journal 指纹全等 → 通过; 目标缺失 / 不等 / 不可读取 → 终止清理:
   保留源、备份与 journal 行, 写 last_error（含现场值与期望值）, 交人工处理
2. 删除备份（若残留）
3. 删源前再次核验目标（同第 1 步, 覆盖第 2 步前后目标被外部改写的窗口）,
   通过后: mode = move 且源路径仍存在 → 对该磁盘文件现场 compute_oshash + stat size,
   与 journal 指纹全等才删除源;
   任一不等或取不到 → 保留 journal 行 + last_error, 下次重试, 绝不删除
   mode ≠ move → 源本来就该保留, 不做任何事
4. 全部完成 → 删除 journal 行
```

**核验顺序先目标后源**（第 6 轮修订）：目标核验不通过时源与备份都保留 ——
此时目标是坏的，源（或备份）可能就是最后一个有效副本，任何删除都不可执行。
只有目标确认有效后，删源才只是消除冗余副本。

删源前的**磁盘核验**独立于 DB 指纹复验：接管后源行的 `path` 已是目标路径，
DB 里不再有源内容的记录，唯一基准是 journal 指纹。对该磁盘文件现场重算
oshash 并与 journal 比对，能拦下「源路径被另一个文件占用」时误删新文件 ——
即使两个文件大小相同，首尾 64 KiB 的内容差异也会使 oshash 不同。

**核验的残余窗口**：目标核验与删源之间存在时间差，窗口内外部修改目标无法根除
（恢复扫描与外部进程没有互斥）。该窗口是尽力防护的已知残余：核验把「恢复必然
使用陈旧判断」收敛为「窗口内恰好发生外部改写」的小概率事件；要彻底消除需要把
源先移入保留名、再确认目标、最后异步删除的多步协议，复杂度不成比例，本阶段不做。

中断点区分：跨文件系统 MOVE 的「replace 成功、删除源之前」中断落在 `placed`
（或 committed 后未删源），此时磁盘状态与 COPY 相同（源、目标都在），
恢复必须靠 `mode` 才知道要删源。同文件系统 MOVE 的源在提交点即消失，该分支自然跳过。
删除源只发生在接管成功且目标核验通过之后：任一前提不满足时源不删除，
磁盘上源与目标并存，没有内容丢失。

两相位都需清理残留的 `.amane-tmp-<任务id>` 临时文件。

用户可见状态：因中断而未完成的任务在 `tasks` 表中仍是 RUNNING。
恢复扫描不修改历史任务状态，重做结果记入日志。

##### 覆写后的索引归属

现有 `handlers/file.py::commit_organized_media_file` 在目标路径已有索引时，
只**补充空字段**（`metadata_id` / `oshash` / `number`），再删除源索引。
这在「跳过覆写」语义下是对的（目标是别的番号条目，不该被源改写）。

在「覆写」语义下，目标视频已被源替换，目标索引必须整体归属给源条目：

| 字段 | 覆写时的处理 |
|---|---|
| `path` | 保持目标路径 |
| `metadata_id` | **覆盖**为源的 `metadata_id` |
| `number` | **覆盖**为源的 `number` |
| `oshash` | **重算**（目标内容已变） |
| `status` | 取源的状态 |
| `size` / `duration` / `codec` | **重算**或置空待刷新 |
| `mosaic` / `definition` / `has_subtitle_in_name` | 按新文件路径重新派生 |
| `library_id` | 不变（目标在本库内） |

因此覆写路径**不能复用** `commit_organized_media_file` 的补空分支，
需要独立的「接管目标索引」路径：**在一个数据库事务内**删除目标旧索引行、
把源索引行的 `path` 改为目标路径、按上表更新字段、并把 journal 行推进到
`phase=committed` —— 四步同事务提交，不存在「索引已接管、journal 未推进」的中间态
（见上文「接管的提交边界」）。

**不同番号条目覆写同一路径**：以源条目为准，目标旧条目成为该路径的唯一所有者。
目标旧条目的 `Metadata` 关系不受影响（元数据按番号存，不随文件走），
但它的这条 `MediaFile` 行会消失。删除前需确认它没有关联的外挂字幕需求
（`has_external_subtitle` 是派生属性，随行删除）。

#### 3.4.3 NFO 字段开关

新增 `NfoOptions`（独立 `nfo` section，不设 `enabled` —— 总开关是 `naming.write_nfo`）。

```
# 标题类
title: bool = True
originaltitle: bool = True
originaltitle_source: Literal["title", "original"] = "title"
sorttitle: bool = True
# 剧情类
plot: bool = True
outline: bool = True
outline_summary: bool = False
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
website: bool = False
external_ids: bool = True
add_part_to_title: bool = False
add_translated_by: bool = False
tagline_format: str | None = None
set_by_actress: bool = False
set_by_serial_name: bool = False
set_by_series: bool = False
additional_tags: list[str] = []
```

`write_nfo(metadata, nfo_path, options: NfoOptions)`。

**`originaltitle` 的两个维度分开表达**：元素是否输出（`originaltitle`）
与内容取自哪一轨（`originaltitle_source`）。一个布尔值无法同时表达
「写这个元素」与「用原文而不是译文」，把它们合并会让默认配置无法保持现状。

##### Kodi 字段语义

- `<plot>` —— **详细剧情**，可多行。
- `<outline>` —— **简短摘要**，一行。

因此原文简介**不进** `<outline>`；它改放 `<originalplot>`（Kodi 非标准字段，
Emby/Jellyfin 会忽略，但信息不丢）。`originalplot` 默认 `False`，
避免给非标准字段引入意外行为。

##### 唯一的输出规则表

`write_nfo` 的输出由**这张表**唯一决定：

| 元素 | 开关 | 取值 | 为空时 |
|---|---|---|---|
| `<title>` | `title` | `_title_with_number(number, title)` | 不写该元素 |
| `<originaltitle>` | `originaltitle` | `originaltitle_source="title"` 时取 `<title>` 的值；`"original"` 时取 `original_title`，为空回退到 `<title>` 的值 | 不写 |
| `<sorttitle>` | `sorttitle` | 同 `<title>` | 不写 |
| `<plot>` | `plot` | `plot` | 不写 |
| `<outline>` | `outline` | `outline_summary=False` 时取 `plot` 同值；`True` 时取摘要 | 不写 |
| `<originalplot>` | `originalplot` | `original_plot` | 不写该元素 |
| `<actor>` | `actor` | 每个演员一组 `<name>`/`<type>` | 不写 |
| `<director>` | `director` | 每个导演一个 | 不写 |
| `<premiered>` | `premiered` | `release` | 不写 |
| `<releasedate>` | `releasedate` | `release` | 不写 |
| `<year>` | `year` | `release` 的前 4 位 | 不写 |
| `<num>` | `num` | `number` | 不写 |
| `<runtime>` | `runtime` | `runtime` | 不写 |
| `<rating>` | `score` | `score` | 不写 |
| `<criticrating>` | `criticrating` | `int(score * 10)` | 不写 |
| `<votes>` | `votes` | 当前无数据来源 | 不写 |
| `<country>` | `country` | 当前无数据来源 | 不写 |
| `<mpaa>` | `mpaa` | `mpaa_value` | 不写 |
| `<customrating>` | `customrating` | 当前无数据来源 | 不写 |
| `<series>` | `series` | `series` | 不写 |
| `<set><name>` | `set` | `series`；`set_by_*` 为真时改取演员/番号前缀/系列 | 不写 |
| `<tag>` | `tag` | 每个标签一个，另加 `additional_tags` | 不写 |
| `<genre>` | `genre` | 每个标签一个 | 不写 |
| `<studio>` / `<maker>` | `studio` / `maker` | `studio` | 不写 |
| `<publisher>` / `<label>` | `publisher` / `label` | `publisher` | 不写 |
| `<poster>` | `poster` | `poster_url` | 不写 |
| `<cover>` | `cover` | `thumb_url` | 不写 |
| `<trailer>` | `trailer` | `trailer_url` | 不写 |
| `<website>` | `website` | `website` | 不写 |
| `<{site}id>` | `external_ids` | `external_ids` 各项 | 不写 |

**`<outline>` 的摘要规则**（仅 `outline_summary=True`）：
取 `plot` 的第一个句号/问号/叹号/换行之前的内容；
无这些分隔符时取前 120 字节（按 UTF-8 字节，不切断多字节字符）。摘要为空则不写。

**`<{site}id>` 的元素名必须清洗**：元素名直接由 `external_ids` 的键拼接，
而键是 `cache_key`，多语言站形如 `r18dev:jp`，会写出 `<r18dev:jpid>` 这种
未绑定前缀的非法 XML。改为把非 XML 名称字符（`:` 等）替换为 `_`，
即 `<r18dev_jp_id>`。这是既有缺陷的修复：
仅影响当前本就无法解析的条目，其余输出不变，需在验收中单列。

**默认值必须与现有 `write_nfo` 的实际输出逐字段一致。** 现有实现（已核对）：

| 现有行为 | 对应开关默认值 |
|---|---|
| `plot` 与 `outline` 同写 `metadata.plot`，二者同时出现 | `plot=True` `outline=True` `outline_summary=False` |
| 无 `originalplot` | `originalplot=False` |
| `premiered` / `releasedate` 同写 `metadata.release`；`year` 由前 4 位派生 | 三者均 `True` |
| **无独立 `<release>` 元素** | 模型中**不设** `release` 开关 |
| `num` 恒写 | `num=True` |
| `title` / `originaltitle` / `sorttitle` 三者同值 | 三者均 `True`，`originaltitle_source="title"` |
| `mpaa` 恒为 `JP-18+` | `mpaa=True`，`mpaa_value="JP-18+"` |
| `rating` 与 `criticrating`（score×10 截断）同时写 | `score=True` `criticrating=True` |
| 无 `votes` / `country` / `customrating` | 三者均 `False` |
| `series` 与 `set/name` 同写 `metadata.series` | `series=True` `set=True` |
| `studio` 与 `maker` 同值 | 两者均 `True` |
| `publisher` 与 `label` 同值 | 两者均 `True` |
| `tag` 与 `genre` 同写 `metadata.tags` | 两者均 `True` |
| `poster` / `cover` / `trailer` 按 URL 非空写 | 三者均 `True` |
| `director` 按 `metadata.directors` 写 | `director=True` |
| `{site}id` 按 `metadata.external_ids` 写 | `external_ids=True` |
| **无 `website`** | `website=False` |
| 无 `tagline` | `tagline_format=None` |
| 无 `set` 的其它来源 | `set_by_*` 三者均 `False` |

**改动的验收方式**：

现有 NFO 测试是子串包含与 ElementTree 解析断言，无法证明「输出不变」。
必须新增**基准用例**：固定一条含全部字段的 `Metadata`，把当前 `write_nfo`
的输出保存为基准字符串，改造后用默认 `NfoOptions` 渲染并与之逐字节比对。

行尾：写入用 `aiofiles.open(..., encoding="UTF-8")` 且未指定 `newline`，
Windows 上产生 CRLF、Linux 上 LF。基准比对以**同一平台的输出**为准；
文本内容层面要求两平台一致。

需**分别验收**：

- 存量数据（`original_title` / `original_plot` 为空）→ 默认配置输出与改造前一致。
- 含原文字段的新数据 + `originaltitle_source="original"` → `<originaltitle>` 为真实原标题。
- `originalplot=True` → `<originalplot>` 为原文简介。
- `website=True` 且 `website` 有值 → 写出 `<website>`。

`additional_tags` 支持的附加内容（对齐 MDC）：
`番号前缀` / `演员` / `分辨率` / `中文字幕` / `有码/无码` / `系列` / `片商` / `发行商`。

#### 3.4.4 NFO 单独重写

新增端点：

```
PUT /api/metadata/{id}/nfo
```

**不能按当前完整模板重算 NFO 路径**：视频模板改过之后，重算得到的 NFO 路径可能
落到另一个目录或另一个文件名，而视频**不会**跟着移动 —— 结果是 Kodi/Emby
匹配不上的孤立 NFO。

**也不能以「视频路径」为基准**：现有布局允许库内视频对应**库外**链接
（`organize/path_templates.py::_resolve_link_path` 要求链接的父目录在库根之外），
而默认 NFO 模板是 `{link_dir}/{video_name}.nfo`，即 NFO 跟随链接落在库外。
以视频目录为基准会把这种情况判为跨目录并拒绝。

正确做法：以**已落盘的消费路径**为基准，只渲染附属文件，**不重算视频与链接路径**。

**链接路径必须持久化**。`resolve_paths` 每次都会用当前的 `video_template` /
`link_template` 重新渲染，模板改过之后它算出的不是历史落盘位置；而 `MediaFile` 只存
库内视频路径，链接路径当前**没有任何持久化**（`paths.link` 只在
`handlers/file.py::execute_file_operations` 内使用，不落库）。

因此新增一列：

```
MediaFile.link_path: str | None
```

- 整理写入链接成功后落库（`create_video_link` 返回 PLACED 时）。
- 未配置链接模板时为空。
- 链接文件已不存在时，读取侧按「无链接」处理，不改写该列。
- `api/models/media.py::MediaFileResponse` 是**手写**字段清单，需手动加一行。
  `to_media_file_response` 用 `to_resp` 按字段名映射，无需改动。
- Alembic：`op.batch_alter_table` 加可空列，无数据回填。

**拆出只渲染附属文件的入口**。`resolve_paths` 承担「渲染视频 + 注入 + 渲染链接 +
渲染附属产物」四项职责，重写 NFO 只需要后两项的一部分。新增：

```python
type SidecarKind = Literal["thumb", "poster", "fanart", "extrafanart", "nfo", "trailer", "subtitle"]

def resolve_sidecar_paths(
    library: Library,
    metadata: Metadata,
    *,
    consume_path: Path,
    video_path: Path,
    source_path: Path | None = None,
    ext: str = "",
    file_info: FileInfo | None = None,
    safe_dirs: Sequence[Path] | None = (),
    actor_genders: Mapping[str, ActorGender] | None = None,
    artifacts: Collection[SidecarKind] = ALL_SIDECARS,
) -> ResolvedSidecars:
    """渲染 ``artifacts`` 指定的附属产物, 不渲染视频与链接模板.

    ``video_path`` 用于注入 ``{video_*}``; ``consume_path`` 用于注入
    ``{link_dir}`` / ``{link_name}`` (有链接) 或退化为视频侧 (无链接).
    ``source_path`` 是原始文件路径, ``{raw_dir}`` / ``{raw_name}`` /
    ``{raw_relpath}`` 由它派生 (``template.py::_build_variables``).
    ``artifacts`` 之外的产物不参与求值, 返回值对应字段为 None.
    """
```

**`artifacts` 是产物选择的接口契约**（第 5 轮修订）。渲染是逐模板求值，
每个模板都可能抛错（越界、语法）；只读返回值的 `nfo` 字段无法阻止其他模板
被求值，无关图片模板的越界错误仍会阻断 NFO 重写。因此未列入 `artifacts`
的产物**不渲染**：不解析模板、不执行 `resolve`、不触发任何校验。
`resolve_paths` 传全集（行为不变）；重写 NFO 传 `("nfo",)`，
即使 `thumb_template` 等模板当前无效，NFO 重写也不受影响。

**`source_path` 必须传入**。`raw_dir` / `raw_name` / `raw_relpath` 三个占位符取自
原始文件路径（`raw_dir = source_path.parent.name`、`raw_name = source_path.stem`、
`raw_relpath` 为相对库根的路径），而不是落盘后的路径。若该接口只收视频与消费路径，
凡使用 `raw_*` 的附属模板（含字幕模板的调用链）在重写时渲染结果会与整理时不同。
签名与 `resolve_paths` 的同名参数保持一致，上下文构造由同一个内部函数完成，
两处不得各写一份。

`resolve_paths` 改为调用它，避免两套渲染逻辑漂移；`resolve_paths` 的签名与返回类型不变
（现有调用点 `handlers/file.py`、`library/cleanup/footprint.py` 与测试均不受影响）。

**重构的等价性验收**：`resolve_paths` 重构前后，对同一输入（library / metadata /
source_path / file_info / safe_dirs / actor_genders）必须产出完全相同的
`ResolvedPaths`。既有测试 `tests/organize/test_path_templates.py` 与
`tests/handlers/test_file_ops.py` 已覆盖路径模板的主要行为，重构后必须原样通过、
不得修改断言；再补一条「使用 `raw_*` 的附属模板在重构前后渲染一致」的用例，
锁住上下文构造只有一份。

注意 `library/cleanup/footprint.py` 也用 `resolve_paths` 判断「哪些文件属于这次整理」：
它比对的是**模板算出的路径**与磁盘上的文件。重写 NFO 不复用这套逻辑，
因此清理清单不受 `link_path` 影响。

```
重写流程
1. 定位该条目的已落盘路径:
   - 视频路径 = MediaFile.path, 必须存在 → 否则 409 (不可重写)
   - 消费路径 = MediaFile.link_path (存在且文件在磁盘上) 否则 视频路径
2. 以 video_path / consume_path / source_path=MediaFile.path 构造上下文,
   调 resolve_sidecar_paths(artifacts=("nfo",)), 取其 nfo 字段
3. 校验 NFO 与消费文件配对:
   - nfo.parent == 消费路径.parent
   - nfo.stem == 消费路径.stem
   任一不满足 → 422 + 明确报错, 不静默写到别处
4. 写 NFO. 目标已存在则覆盖 (这是「重写」的语义)
```

**该端点只重写 NFO**。原始路径（`source_path`）在整理后没有持久化，
`MediaFile.path` 已是落盘路径 —— 含 `raw_*` 占位符的附属模板在重写时无法
还原整理时的取值。因此端点只渲染 NFO 模板（默认模板不含 `raw_*`），
不重算 thumb / poster / fanart / trailer 的路径；那些产物随下次整理更新。

`stem` 校验覆盖「同目录但不同名」的情况 —— Kodi 按
「与视频同目录、同 stem、`.nfo` 后缀」配对，只查目录不足以保证匹配。

**行为边界**：

| 场景 | 行为 |
|---|---|
| 条目有多个 `MediaFile` | 每个文件各自重写一份 NFO（按各自的消费路径） |
| 条目跨多个库 | 每库内各自的路径分别处理；库外文件不处理 |
| 条目无本地文件（只有元数据） | 返回 409「无本地文件，无法重写 NFO」 |
| 视频在库内、链接在库外 | 以 `link_path` 为消费路径，NFO 落在链接旁 |
| `link_path` 为空但存在同名 STRM | 不猜测：以视频为消费路径，NFO 落在视频旁 |
| NFO 模板算出的 stem 与消费文件不同 | 返回 422，报出实际与期望两个 stem |
| 链接文件已不存在 | 以视频为消费路径，NFO 落在视频旁；不改写 `link_path` |

**验收**：

- 只改 `nfo_template` 后重写 → NFO 落在消费文件旁、同 stem。
- 只改 `video_template`（不改 NFO 模板）后重写 → 仍以**已落盘**的视频路径为基准，
  不因新视频模板而挪位置。
- 只改 `link_template` 后重写 → 用持久化的 `link_path`，不是用新链接模板算出的路径。
- 三种情况都要覆盖「库内视频 + 库外链接」布局。

### 3.5 章节划分与文档

| 内容 | 落点 |
|---|---|
| 配置覆盖的合成语义（四层顺序、稀疏含义、显式设置标记） | `docs/dev/config.md` |
| 模板引擎双实现、语法分隔符与兼容 filter | `docs/dev/organize.md`（新建） |
| 识别词规则的作用域、分桶覆盖与匹配优先级 | `docs/dev/parsing.md`（新建） |
| 原文双轨字段的来源、按字段语言判定与 `website` 派生 | `docs/dev/data-model.md` |
| 覆写的备份/提交点/清理协议、`organize_journal` 与索引接管 | `docs/dev/organize.md` |
| 用户可见的命名与 NFO 行为 | `docs/user/organize.md` |
| NFO 字段语义（`plot` / `outline` / `originaltitle` / `originalplot`） | `docs/user/media-server.md` |

`docs/dev/index.md` 需登记新建的两个文档。`docs/user/media-server.md`
中「NFO 内容固定, 不提供自定义模板」的表述需随字段开关改写。

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| 覆盖 payload 校验失败 | API 返回 422 + Pydantic 错误详情；不落库 |
| 覆盖 section 不在白名单 / `scope_type` 非 `library` | 422，不落库 |
| 模板语法错误（保存时） | 422 + 错误位置 |
| 模板含未知占位符或未知 filter（保存时） | 422 + 名称与位置 |
| 语法切换后有占位符残留 | 422 + **哪个模板、哪处残留**；拒绝切换 |
| 模板渲染失败（落盘时） | 该文件整理失败，任务记 `failed`，错误信息含模板名与位置；不影响其它文件 |
| Jinja2 渲染产生越界路径 | `PathEngine.resolve` 抛 `ValueError` → 同上 |
| Jinja2 模板访问被禁属性 | `SandboxedEnvironment` 抛 `SecurityError` → 渲染失败 |
| 覆写时目标不在库内 | 拒绝覆写，记冲突 |
| **覆写阶段 A/B 失败（提交点之前）** | 回滚：删除临时文件、恢复备份到原目标名；目标保持可用；任务记 `failed` + 回滚原因 |
| **提交点之后任何失败**（journal 更新 / 索引接管 / 删除备份） | **不回滚、不删除目标**；journal 保留并写 `last_error`；任务记 `failed` 但视频可用 |
| **覆写进程中断** | 按 `organize_journal` 的 phase 恢复；不把「目标存在」当冲突跳过 |
| **恢复时源身份指纹不匹配**（id 复用 / 路径复用 / 源行清除） | **禁止接管**：journal 保留 + `last_error`（含失败候选与不一致字段），记日志交人工处理；不把其他文件的元数据写到目标上 |
| **恢复时目标副本核验不通过**（目标被删 / 截断 / 替换 / 不可读取） | **禁止接管与删源**：保留源、备份与 journal 行，写 `last_error`（现场 oshash/size 与期望值），交人工处理；源与备份中至少一份是有效副本，不丢失 |
| **源文件指纹无法补算**（< 128 KiB / 不可读） | 放弃覆写，任务记 `failed`；journal 不产生空指纹的记录 |
| **删源前磁盘 oshash 与 journal 不等** | 不删除源：journal 保留 + `last_error`，下次重试复验；源与目标并存，无内容丢失 |
| **跨文件系统 MOVE 删除源中断** | 接管已完成，源与目标并存无内容丢失；恢复按 `mode=move` 复验源指纹后删除源；`mode=copy` 不删源 |
| **恢复时源行取不到（两条线索均无匹配）** | **保留 journal 行**并写 `last_error`，下次继续重试；不删除唯一恢复记录 |
| 自定义识别词正则非法或含 RE2 不支持的构造 | 保存时 RE2 编译校验，非法 / 反向引用 / 环视返回 422 |
| **识别词 ReDoS 安全**（第 8 轮 P2） | 表测试：RE2 编译合法 pattern 通过；`(a|aa)+$` 配长近似匹配输入**线性时间完成**（功能正确 + 耗时断言）；反向引用 / 环视 → 422 |
| 识别词多条命中（同段内，含跨 `content_type` 桶） | 按 `created_at` 升序（同刻按 id 升序）首条命中生效 |
| 仅按番号解析（`text=`）时含路径规则 | 路径规则跳过，只应用番号关键词规则 |
| 存量 `raw` 快照缺新字段 | `MediaMetadata` 新字段必须有默认值 —— 缺失时 Pydantic 抛 `ValidationError`，`_fetch_one` 只 `except TypeError`，兜不住 |
| **原文字段与译文字段来源不同** | 原文按各自字段链独立确定；翻译只改 `title`/`plot` |
| 翻译失败 | `title`/`plot` 保持源站原值；`original_*` 独立存在 |
| `library.template_overrides` 与实际模板字段不一致 | 以名单为准；名单外的模板字段视为继承 |
| **单独重写 NFO 时条目无本地文件** | 409「无本地文件，无法重写 NFO」 |
| **单独重写 NFO 时目录或 stem 不匹配** | 422，报出实际路径与期望配对 |
| `external_ids` 的键含非 XML 名称字符 | 元素名清洗后写出，不产生非法 XML |

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| **刮削落库接线**（第 7 轮 P1） | 集成测试：执行一次刮削任务后，DB 行与 `GET /api/metadata/{id}` 都能读回 `original_title` / `original_plot` / `website` / `field_languages` |
| **Jinja 运行时分派**（第 7 轮 P2，第 8 轮补字幕） | 集成测试：保存 Jinja 视频/附属/**字幕**/STRM 正文/剧照模板 → 整理 → 磁盘产物路径与文件名按 Jinja 渲染（**含磁盘上的实际字幕路径**）；任一入口漏接会把 `{{ }}` 当字面量落盘 |
| **生效命名配置真实落盘**（第 7 轮 P1，第 8 轮补字幕） | 集成测试：写 `naming` 覆盖（覆盖 `video_template` 与 `subtitle_template`）后执行整理，**磁盘产物路径**（含字幕）按覆盖后的模板生成；`resolve_paths(naming=…)` / `resolve_subtitle_path(naming=…)` 不回读库行 |
| **覆写接入冲突分支**（第 7 轮 P2） | 集成测试（从 `OrganizeHandler` 发起）：`overwrite_video=False` 时 OCCUPIED 记 CONFLICT；`=True` 时走覆写协议；`overwrite_image=False` 时已有图片跳过 |
| **识别词生产入口**（第 7 轮 P2） | 集成测试：真实扫描流程下两个库对同一番号文件的解析结果（`FileInfo.content_type`）与后继 SCRAPE 任务载荷不同；**既有 `Metadata.content_type` 保持稳定**（类型全局按番号唯一、首建时定，库规则不回写既有条目——`upsert` 契约） |
| `ConfigResolver.resolve` | 表测试：四层组合顺序、嵌套 dict 深合并、非 dict 替换、空覆盖 |
| **任务层只取显式字段**（P2-3） | 表测试：`_apply_library` 填入的库值不进第 4 层；用户显式给出时进第 4 层；**`explicit_fields` 经 `model_dump`/`model_validate` 往返后保持一致**；手动与自动整理得到同一生效配置 |
| **`explicit_fields` 的 null 与历史任务兼容**（第 4 轮 P2） | 表测试：请求传 `write_nfo: null` 不进显式集（沿用库值，不压覆盖表）；`write_nfo: false` 与 `copy_resources: []` 进显式集；旧 payload 缺该键 → 按「非 None 字段即显式」解析，旧任务行为不变 |
| 稀疏覆盖的 null 契约 | 表测试：payload 含 null 被拒 422；`link_template={}` 表示不建链接；`thumb_template={}` 表示用内建默认；键缺失表示继承 |
| `template_overrides` 生效判定 | 表测试：名单内字段生效、名单外让位；`None` 作为显式值生效 |
| **历史库迁移**（P1-3） | 迁移后 15 个模板字段全在名单内；`thumb_template=None` 的库在改全局默认后落盘不变；新建库名单为空 |
| **覆盖表对既有库生效**（P1-1） | 迁移后写 `naming` 覆盖 → 覆盖生效；不写覆盖 → 落盘不变 |
| `ParseRuleSet` 匹配 | 表测试：番号优先于路径、路径含目录、大小写、`r:` 前缀、非法正则拒绝、首条命中 |
| **跨类型与跨作用域的命中顺序**（第 4 轮 P2） | 表测试：同一 pattern 同时登记 `chinese` 与 `western` → `created_at` 早者生效；两条正则重叠同此；库级与全局同词不同类型 → 按桶替换后扁平排序；同段内 `created_at` 相同时按 id 升序兜底 |
| **路径规则的路径输入**（P2-6） | 表测试：同名文件在不同目录 → 不同类型；`text=` 分支跳过路径规则；原始大小写不被归一化 |
| 识别词作用域 | 表测试：库级规则覆盖全局、按桶整体替换、两个库对同一番号取不同规则、缓存按 `(count, max(updated_at))` 失效 |
| `TemplateEngine` 扩展 | 表测试：条件块真/假（含多变量）、filter 链、嵌套 `default`、无值判定、`Unknown` 被 `default` 覆盖、未知 filter 报错 |
| **`:` 与 `|` 的分工** | 表测试：`{title|truncate=10}` 仍是值映射（渲染不变）；`{title:truncate(10)}` 走 filter；`replace("a,b","c")` 引号内含逗号可用 |
| 占位符名校验 | 表测试：未知占位符保存被拒；`dir` 别名被接受；已落盘的含未知占位符模板渲染输出仍为 `Unknown` |
| `JinjaTemplateBackend` | 表测试：`{{ }}` 与 `{% %}`、Sandbox 拒绝属性访问、undefined 变量 |
| **兼容 filter** | 表测试：`default` 对空串/空列表/`Unknown` 均生效（非原生语义）；`truncate` 按 UTF-8 字节截断且无省略号；`split`/`first`/`last` 组合 |
| 两引擎一致性 | 表测试：同一组变量在共有语义上产出一致（仅排除「变量缺失」） |
| **语法切换验证** | 表测试：自研模板切 Jinja2 被拒（含残留检测）；反向被拒；继承与回退模板也参与；两套默认模板各自可用 |
| **Jinja 变量名映射**（P2-5） | 表测试：`cd?`→`cd`、`sub?`→`sub`、`mosaic?`→`mosaic`、`def?`→`definition`；`{{ cd? }}` 解析期报错；其余 26 个同名 |
| **两套默认模板等价** | 表测试：分集/中字四种组合下，两引擎渲染路径**完全相同**（含多分集不撞路径） |
| `resolve_paths` + inplace | 表测试：视频路径 = 源路径、`video_template` 被忽略、图片落在源目录 |
| **`MoveMode.INPLACE` 分支存在** | 表测试：inplace 不移动视频；不是静默落空（用 mock 断言未调用 move/copy） |
| **覆写事务性**（P1-1） | 表测试：move/copy/hardlink/symlink 四模式各一条；磁盘不足时目标完好；权限失败时目标完好 |
| **journal 更新失败不删目标**（第 3 轮 P1） | 表测试：`os.replace` 成功、写 `phase=placed` 抛错 → 目标保留新内容、不回滚、journal 仍为 `prepared`、任务记 failed |
| **提交点后清理失败** | 表测试：删除备份抛错 / 删除 journal 抛错 → 目标保留、不报回滚；下次扫描幂等清理 |
| **跨文件系统移动** | 表测试：monkeypatch `os.replace` 抛 `EXDEV` → 走临时文件 + replace；源在接管成功前**不删除** |
| **跨文件系统 MOVE 的源删除中断**（第 4 轮 P2） | 表测试：replace 成功、删除源之前中断 → 恢复按 `mode=move` **现场重算磁盘 oshash** 与 journal 全等后删除源；同一中断点 `mode=copy` → 恢复**不删**源；磁盘文件被替换为等大小不同内容 → oshash 不等 → 不删源 |
| **journal 恢复**（P1-3） | 表测试：prepared（目标缺失→还原备份 / 目标存在→接管）、placed（重做接管后 committed）、committed（清理）；恢复幂等 |
| **journal 恢复失败保留记录**（P2-2） | 表测试：源行取不到 → journal 保留 + `last_error`；目标有文件、无索引（`target_file_id=NULL`）中断 → 接管成功；连续失败不删除记录 |
| **源身份复验**（第 4 轮 P1） | 表测试：`source_file_id` 被新文件复用（指纹不匹配）→ 拒绝接管、保留 journal、按 `source_path`+指纹兜底；源行被清除且路径无匹配 → 保留 journal 记日志；指纹一致 → 正常接管 |
| **指纹缺失与内容区分**（第 5 轮 P1） | 表测试：源行 oshash/size 为 NULL → 阶段 A 现场补算，journal 恒非 NULL；文件 < 128 KiB → 放弃覆写不产生 journal；**路径复用但内容不同** → oshash 不等 → 拒绝接管；**大小相同内容不同** → oshash 不等 → 拒绝接管与删源 |
| **接管与 journal 同事务**（第 5 轮 P2） | 表测试：接管事务提交后 journal 必为 `committed`（无中间态）；事务回滚 → 两者同时回退；恢复遇到「源行 `path == target_path` 且指纹全等」→ 按已完成接管直接进清理 |
| **目标副本核验**（第 6 轮 P1） | 表测试：`placed` 恢复、目标被替换（等大小不同内容）→ 不接管、保留源与备份与 journal；`committed` 清理、目标被删除 → 不删源，保留 journal；`committed` 清理、目标被截断 → oshash 不等 → 不删源；目标核验通过 + 源指纹通过 → 正常删源删备份删 journal |
| **覆写索引归属**（P1-2） | 表测试：`metadata_id`/`number` 被**覆盖**（非仅补空）、`oshash` 重算、文件相位字段按新路径派生；不同番号覆写同一路径时源成为唯一所有者；接管在单事务内 |
| **图片覆写默认值**（P2-8） | 表测试：默认配置下已有图片被覆盖（现状一致）；`overwrite_image=False` 时已有图片不被覆盖且计入 skipped |
| `write_nfo` + NfoOptions | 表测试：每个开关的开/关对输出 XML 的影响 |
| **NFO 默认输出基准**（P2） | 新增基准字符串用例：含全字段的 `Metadata` 在默认 `NfoOptions` 下输出与改造前逐字节一致（同平台） |
| **NFO 字段语义** | 表测试：`<plot>` = 详细剧情、`<outline>` 默认同值、`outline_summary=True` 取摘要、`originaltitle_source` 两种取值、`originalplot` 默认不写 |
| `<{site}id>` 元素名 | 表测试：`r18dev:jp` → `<r18dev_jp_id>`，输出可被 XML 解析 |
| **原文双轨聚合**（P1-4） | 表测试：标题来自日文源、剧情来自中文源时原文各取自己的链；未声明语言不写原文；翻译失败时 `title`/`plot` 保持原值 |
| `field_languages` 记录 | 表测试：随 `_fill_scalar` 记录对应字段语言；`site:lang` 形式的 key 正确拆解 |
| `website` 取值 | 表测试：取 `source_urls[field_sources[TITLE]]`；标题源无 URL → None 且**不回退**；`source_urls` 全量保留；`MetadataField` 无 `WEBSITE` 成员 |
| **枚举契约** | 表测试：`MetadataField` 全部成员存在于 `MediaMetadata`（现有用例不得失败） |
| avsox 多语言修复 | 表测试：声明 `MULTI_LANGUAGE` 后进入 `MULTI_LANGUAGE_SITES`、`_lang` 生效、`title_ja`/`description_ja` 可取到、`_first` 顺序修正（标题 ja 已在 cn 前，简介需要修） |
| `MediaMetadata` 新增字段 | 回归：存量 `raw` 快照仍能反序列化（不抛 `ValidationError`） |
| **单独重写 NFO** | 表测试：库内视频 + 库外链接落在链接旁；多文件条目各写一份；无本地文件 409；stem 不匹配 422 |
| **NFO 重写不重算路径**（P2-4） | 表测试：只改 `video_template` / 只改 `link_template` / 只改 `nfo_template` 三种情况，重写都以已落盘路径为基准；`link_path` 为空时以视频为基准 |
| **`resolve_paths` 重构等价**（第 5 轮 P2） | 表测试：重构前后对同一输入产出相同 `ResolvedPaths`（既有用例不得改断言）；含 `raw_*` 的附属模板渲染一致；`resolve_sidecar_paths` 不渲染视频与链接模板 |
| **`artifacts` 产物选择**（第 5 轮 P2） | 表测试：`artifacts=("nfo",)` 时其他产物字段为 None 且**模板不被求值**（thumb 模板越界抛错也不阻断）；`artifacts` 全集时与旧 `resolve_paths` 行为一致 |
| API 端点 | 覆盖 CRUD、422 路径、删除后回到继承 |

**回归重点**：

1. **既有库的落盘结果不变** —— 用现有测试套件验证，并补「历史库迁移后名单含全部模板字段、
   升级后再改全局默认、落盘结果不变」与「迁移后写覆盖、覆盖生效」两条用例。
2. **存量 `raw` 快照兼容** —— 补一条用旧格式 `raw` 反序列化的用例。
3. **NFO 默认输出不变** —— 现有用例只做子串断言，必须新增基准字符串比对。
   另需分别验收：存量数据输出不变；含原文字段的新数据在 `originaltitle_source="original"`
   与 `originalplot=True` 下正确产出。

## 6. 实施顺序（供实现计划参考）

按「先改现有行为、后加新能力」的顺序，每步都可独立验证。

### 第 1 组：原文双轨与数据层

1. **avsox 两处修复**（声明 `MULTI_LANGUAGE` + `_first` 顺序）
2. **`MediaMetadata` 加 `original_title` / `original_plot` / `field_languages`**（含默认值）
3. **`Metadata` 加四列** + `MetadataField` 加两枚举值 + `SCALAR_FIELDS` 加两个原文字段
   + `AggregatedMetadata` 加属性 + `MetadataFields` TypedDict
   + `update_metadata` 显式赋值 + `PartialMetadata` 排除 `website` + Alembic revision
4. **各源填原文字段与 `field_languages`**（DMM / MGStage / official / wp_works / avsox / r18dev 等）
5. **`website` 派生取值**（`source_urls[field_sources[TITLE]]`）
6. **翻译层只改 `title`/`plot`**
7. **`NfoOptions` 模型 + `write_nfo` 改造**（先加基准字符串用例，再改造）

### 第 2 组：配置基础层

8. **3 个新 section 模型**（`common` / `naming` / `nfo`）加进 `HotSettings`
9. **`ConfigResolver` + `config_overrides` 表** + `Library.template_overrides` 列
   + **历史库数据迁移**（15 个模板字段全部进名单）
10. **`parse_rules` 表（含作用域列）+ `ParseRuleSet`** + `parse_file_info` 扩展参数

### 第 3 组：模板引擎与整理

11. **模板引擎扩展**（`:` filter 链 / 条件块 / 降级链）+ 保存时占位符校验 + `TemplateBackend` 抽象
12. **`JinjaTemplateBackend` + Jinja2 主依赖 + 兼容 filter**
13. **`naming.syntax` + 两套默认模板 + 切换时的语义验证**
14. **`MoveMode.INPLACE` + `resolve_paths` 与 `execute_organize` 分支**
15. **`organize_journal` + 覆写协议**（提交点判据 / 跨文件系统 / 回滚与清理分离）
16. **覆写的索引接管**（单事务，独立于 `commit_organized_media_file`）
17. **journal 恢复扫描**（启动时 + 整理同一目标前；失败保留记录）
18. **`MediaFile.link_path` 列 + 整理时落库**
19. **`resolve_sidecar_paths` 拆分**（`resolve_paths` 改为调用它）
20. **试算端点 + 前端试算面板**
21. **覆盖 API + 前端覆盖编辑 UI**
22. **识别词管理 API + 前端**
23. **原文字段的前端展示**（详情页 + i18n）
24. **单独重写 NFO 端点**（以已落盘的消费路径为基准）

## 7. 待确认

| # | 问题 | 状态 |
|---|---|---|
| 1 | Jinja2 依赖策略 | **已定案**：主依赖 |
| 2 | NFO 默认字段集 | **已定案**：以现有 `write_nfo` 实际输出为准（3.4.3 的对照表） |
| 3 | 覆盖表与 `Library` 行的优先级 | **已定案**：覆盖表优先；`template_overrides` 名单外的库行让位 |
| 4 | `parse_rules` 表设计 | **已定案**：独立表，表内带作用域列，不进 `config_overrides` |
| 5 | `original_plot` 语义 | **已定案**：源站原文（日文） |
| 6 | `website` 设计 | **已定案**：派生单值列，`source_urls` 保留全量 |
| 7 | 自研语法的 filter 分隔符 | **已定案**：`:`，`|` 保留给值映射 |
| 8 | `copy_resources` 的三处默认值不一致 | **待定**：`Library` 模型默认「除 trailer 外全部」，`create_library` 与 `LibraryCreateRequest` 默认含 trailer。本阶段不改既有行为，统一默认值单独处理 |

### 已知限制（明确不做）

1. **Javdb / Javbus / iqqtv 不提供原标题** —— 需改 locale 或请求头，
   会影响既有抓取行为，本阶段不动。
2. **Javdb / Javbus / Javlibrary / Jav321 / freejavbt 不解析 plot** —— 不提供原文简介。
3. **`original_plot` 无数据来源的条目** —— 存量数据未重新刮削前为空，`<originalplot>` 不写。
4. **r18dev 无 `source_url`** —— 其 `mapper.py` 固定 `source_url=None`，`website` 恒为空。
5. **`<originalplot>` 是 Kodi 非标准字段** —— Emby / Jellyfin 会忽略，仅作信息留存。
6. **`votes` / `country` / `customrating` 三个开关暂无数据来源** —— 保留开关但不产出内容。
7. **`watch_dir` 作用域** —— 表结构预留，本阶段写入返回 422。
