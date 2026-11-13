# MDC 内部架构（逆向结果）

> 来源: `nm` 符号表 + 字符串中的源码路径 + 容器内目录结构。日期 2026-11-12。

## 技术栈

| 项 | 事实 |
|---|---|
| 语言 | Rust（crate 名 `mdc_ng_app`，符号表未 strip，60979 个 FUNC） |
| 后端框架 | **actix-web**（389 actix_web + 130 actix_http + 76 actix_server 符号） |
| 前端 | Next.js（`/opt/frontend/.next`，App Router + RSC） |
| 数据库 | SQLite（SeaORM：`seaql_migrations` 表） |
| 镜像 | `mdcng/mdc:latest`，Debian 12，x86_64 |
| 进程 | PID 1 = `entrypoint.sh` → PID 13 = `mdc_ng_app` → PID 14 = `next-server` |
| 附带二进制 | `ffprobe` 78MB、`deeplx` 9MB（DeepL 本地翻译）、`curl-impersonate/`（TLS 指纹伪装） |

## Workspace 结构

源码是 monorepo，路径前缀 `pkg/<name>/src/...`：

| 包 | 职责 |
|---|---|
| `pkg/core` | 核心：scraper、config、parser、nfo、mdc_context、utils |
| `pkg/actress` | 演员刮削（action/scraper.rs） |
| `pkg/ai` | AI 命名模板生成 |
| `pkg/entity` | 数据模型 |
| `pkg/service` | 服务层 |
| `pkg/migration` | SeaORM 迁移 |

顶层 `src/` 是应用壳：`action/{actress,manual,reorganize,scrape}`、`api/{ai,config,file,file_tree,resources,system}`、`monitor/`、`util/{fs_async,video_filter}`。

## 模块树（mdc_ng_app 自身符号）

```
mdc_ng_app::action::actress        # 演员任务
mdc_ng_app::action::manual          # 手动任务（扫描目录→整理）
mdc_ng_app::action::reorganize      # 整理（移动/硬链/软链）
mdc_ng_app::action::scrape          # 刮削
mdc_ng_app::api::ai                 # AI 命名模板
mdc_ng_app::api::config             # 配置读写
mdc_ng_app::api::configure          # 路由注册
mdc_ng_app::api::file               # 文件操作
mdc_ng_app::api::file_tree          # 文件树浏览
mdc_ng_app::api::resources          # tasks / manual_jobs / actress_tasks / reorganize
mdc_ng_app::api::system             # 系统
mdc_ng_app::cleanup                 # 源目录清理
mdc_ng_app::monitor::async_watcher  # 目录监控
mdc_ng_app::monitor::watch_and_scrape
mdc_ng_app::util::fs / fs_async / video_filter / fc2
```

## pkg/core 内部结构

```
pkg/core/src/
├── config/           # common, naming, migration, mod
├── mdc_context/      # folder, image, link, nfo, parse, scrape   ← 刮削结果的落地视图
├── nfo/              # builder, serializer
├── parser/           # cn_parser, fallback_parser, janime_parser,
│                     # jav_parser, part, standard_parser
├── scraper/
│   ├── coordinator.rs
│   ├── providers/          # carib, fc2, javbus, javdb, javlibrary,
│   │                       # madou, madouqu, mgstage, mmtv, theporndb
│   └── providers_custom/   # dmm/{graphql_scraper,json_path,mod},
│                           # provider.rs, utils.rs, mod.rs
├── types/
├── utils/            # cp, evil, face_detection, filename_parser, image,
│                     # request, request_curl, sanitizer, static_data,
│                     # task_log, translate, video_image
└── webhook/
```

### 刮削器目录（provider 清单以源码为准）

- **内置 provider**（`scraper/providers/`）：`carib`、`fc2`、`javbus`、`javdb`、`javlibrary`、`madou`、`madouqu`、`mgstage`、`mmtv`、`theporndb`
- **自定义 provider**（`scraper/providers_custom/`）：`dmm`（GraphQL + json_path）、`provider.rs`（trait 定义）、`utils.rs`
- 其余 provider（Jav321、Mmtv、Miss_av、Airav、Hbox_jp、Freejavbt、Xiao_huang_shu、7mmtv 等）走通用 provider trait 的 base_url 配置。

### 其他 notable 模块

| 模块 | 说明 |
|---|---|
| `utils/face_detection` | 人脸识别裁剪，用 `seeta_fd_frontal_v1.0.bin`（从 GitHub releases 下载） |
| `utils/request_curl` | 基于 curl 的请求（`curl-impersonate/` 目录，TLS 指纹伪装） |
| `utils/static_data` | 演员/标签静态映射表（`mapping_actor.xml`、`mapping_info.xml`） |
| `utils/translate` | 翻译（deeplx 本地 + OpenAI 兼容 + deepl） |
| `utils/evil` | 命名可疑 |
| `parser/part` | 分集（part）识别 |
| `mdc_context/*` | 刮削结果 → 磁盘布局（folder/image/link/nfo） |

## 关键 crate 依赖（按符号数）

| crate | 符号数 | 用途 |
|---|---|---|
| `tokio` | 2417 | 异步运行时 |
| `sea_query` | 611 | SQL 查询构造（SeaORM） |
| `image` | 601 | 图片处理（裁剪/缩放/编码） |
| `actix_web` | 389 | HTTP 框架 |
| `serde_json` / `serde` | 677 | 序列化 |
| `sea_orm` | 257 | ORM |
| `image_webp` | 183 | WebP |
| `reqwest` | 93 | HTTP 客户端 |
| `selectors` | 51 | CSS 选择器 |
| `scraper` | 45 | HTML 解析（比 scraper crate 更底层） |
| `sea_orm_migration` | 35 | 迁移 |
| `rustface` | 16 | **人脸识别**（裁剪海报） |
| `serde_xml_rs` | 6 | 解析 `mapping_actor.xml` 等 |

## 前端路由（Next.js App Router，共 8 页）

```
/opt/frontend/.next/server/app/
├── (auth)/login/page.js
├── (app)/page.js            # 首页仪表盘
├── (app)/tasks/page.js       # 刮削记录
├── (app)/providers/page.js   # 数据源管理
├── (app)/filemanager/page.js # 文件管理
├── (app)/persons/page.js     # 演员管理
├── (app)/manual-jobs/page.js # 手动任务
└── _not-found/page.js
```

设置页是 `(app)` 布局内的 tab 组件（非独立路由），故 URL 恒为 `/settings/common`，靠客户端切换。

## 外部资源下载（运行时网络依赖）

- `releases/download/actress_db/Actress-20231123.db` — 演员资料库
- `releases/download/actress_db/seeta_fd_frontal_v1.0.bin` — 人脸检测模型
- `github.com/gfriends/gfriends` — 头像库（与 Amane 同一来源）
- `github.com/mdc-ng/mdc-core` — 主源码仓库（AGPL-3.0）

## 数据库

两个 SQLite 文件（`/config/data/`）：

### `mdc_ng.db`（主库）

| 表 | 行数 | 说明 |
|---|---|---|
| `task` | 900 | 刮削任务主表 |
| `log` | 14150 | 任务日志 |
| `task_log_session` | 448 | 日志会话 |
| `manual_job` | 3 | 手动任务（扫描目录→整理） |
| `actress_task` | 0 | 演员任务 |
| `actress_log` | 0 | 演员日志 |

`task` 表关键列：
- `number`、`actors`、`title`、`original_title`、`release`、`poster_provider`（均为 VIRTUAL 生成列，从 `metadata` JSON 提取）
- `parse_result` (json) — 番号解析结果，含 `category`、`extra_meta`（is_censored/is_uncensored_hack/has_subtitle/resolution/part…）
- `image_result` (json) — poster/thumb/fanart/extra_fanarts 本地路径
- `metadata` (json) — 最终聚合结果（43 字段，PascalCase）
- `metadata_provider_info` (json) — 每个字段来自哪个源
- `metadata_all_sites` (json) — 各站原始返回快照
- `link_mode`、`delete_empty_parent_after_move`、`override_url`、`part`、`video_prefix`、`reorganize_use_global_config`

### `Actress.db`（演员资料库，独立）

表 `Info`：Name / Roma / Href / Birthday / Age / Height / Bust / Waist / Hip / Cup / Birthplace / CareerPeriod / DebutWork / Account / OfficialSite / UpdateTime / Completeness

主键 `Href`。文件 10.9MB。

## MDC 的元数据字段（43 个，PascalCase）

ActorLinks, Actors, Category, Censored, CensoredPrefix, Country, Cover, DMMCid, Director, ExtraFanart, FanartPath, Hack, HackPrefix, JavdbId, NoSubtitlePrefix, Number, OriginalActors, OriginalPlot, OriginalTitle, Outline, Poster, PosterPath, PublishNumber, Publisher, Release, ResolutionPrefix, ResolutionRaw, Runtime, SerialName, SerialNumber, Series, Source, SourceFilename, SourcePath, Studio, Tags, ThumbPath, Title, Trailer, UserRating, UserVotes, Website, Year

### 与 Amane 字段对照

| MDC | Amane | 差异 |
|---|---|---|
| `Title` | `title` | — |
| （无） | `original_title` | **MDC 无原标题字段** |
| `Outline` | `plot` | — |
| `OriginalPlot` | （无） | MDC 保留原文简介 |
| `Actors` | `actors: list[str]` | MDC 是字符串 |
| `OriginalActors` | （无） | MDC 保留原名 |
| `ActorLinks` | `source_urls` | MDC 只存 javdb 链接 |
| `Tags` | `tags: list[str]` | — |
| `UserRating` | `scores: dict[site,float]` | **MDC 单值，Amane 多站分列** |
| `UserVotes` | （无） | MDC 有，MDC 的 Javdb 提供 |
| `Studio` / `Publisher` | `studio` / `publisher` | — |
| `Release` | `release` | — |
| `Runtime` | `runtime` | MDC 是字符串 |
| `Series` | `series` | — |
| `Director` | `directors: list[str]` | MDC 单值 |
| `Cover` | `thumb_urls` | MDC 单 URL |
| `Poster` | `poster_urls` | MDC 单 URL |
| `ExtraFanart` | `extrafanart_urls: dict[str, list[str]]` | MDC 逗号分隔字符串 |
| `Trailer` | `trailer_urls` | — |
| `Year` | （由 release 派生） | — |
| `Country` | （无） | MDC 有 |
| `Category` / `CensoredPrefix` / `HackPrefix` | `content_type` | MDC 拆成多个展示字段 |
| `DMMCid` / `JavdbId` / `PublishNumber` | `external_ids: dict` | MDC 硬编码字段名 |
| `ResolutionPrefix` / `ResolutionRaw` | `MediaFile.definition` | MDC 在 metadata 里 |
| `PosterPath` / `ThumbPath` / `FanartPath` | `organize` 产物 | — |
| （无） | `locked_fields` | **Amane 有字段锁** |
| （无） | `field_sources` | MDC 用 `metadata_provider_info` 等价 |
| （无） | `raw` | MDC 用 `metadata_all_sites` 等价 |
