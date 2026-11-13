# MDC HTTP API 地图

> 探测日期 2026-11-12，经已登录会话 `fetch()` 实测。后端 actix-web，基础路径 `/api`。
> 未登录时所有端点返回 307 → `/login`（前端走 session cookie）。

## 可用端点（实测 200）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/version` | 返回 `v1.36.0` |
| GET | `/api/config/common` | 整理目录、整理模式、过滤器、监控配置 |
| GET | `/api/config/naming` | 命名模板（folder/file/title/suffix）、分类映射与正则规则 |
| GET | `/api/config/metadata` | 刮削后处理（严格模式、映射、翻译、Flaresolverr） |
| GET | `/api/config/provider` | **刮削源配置 + 优先级**（见 `provider_config.md`） |
| GET | `/api/config/download` | 下载内容类型、缩略图策略、高清海报、裁剪策略、字幕库 |
| GET | `/api/config/nfo` | NFO 字段开关与标签格式 |
| GET | `/api/config/watermark` | 水印样式、布局、偏移、类型开关 |
| GET | `/api/config/webhook` | Webhook 端点、触发事件、Jinja2 body 模板 |
| GET | `/api/config/system` | 刮削线程数、超时、OpenAI 兼容配置 |
| GET | `/api/config/proxy` | 代理地址、超时、预告片超时 |
| GET | `/api/config/actress` | 演员刮削、Emby 集成配置 |
| GET | `/api/tasks?page=N&page_size=M` | 任务列表，返回 `{data, num_pages, total_count}` |
| GET | `/api/tasks/{id}` | **任务详情**（含 metadata / provider_info / all_sites / parse_result） |
| GET | `/api/manual-jobs` | 手动任务列表 |
| GET | `/api/file_tree?root=PATH[&path=PATH]` | 文件树浏览（返回 path/name/is_dir/size/date） |
| GET | `/api/config/provider/test/latest` | 源健康状态快照（`{running, updated_at, results[]}`） |
| POST | `/api/config/provider/test` | 触发源连通性测试 |

## 确认不存在（404）

`/api/task`、`/api/tasks/list`、`/api/resources/tasks`、`/api/resources/manual_jobs`、
`/api/resources/actress_tasks`、`/api/actress-tasks`、`/api/actress`、`/api/reorganize`、
`/api/system/info`、`/api/config`（无子路径）、`/api/tasks/{id}/log`、`/api/logs`、
`/api/metadata`、`/api/search`、`/api/lookahead`、`/api/ai/generate`、`/api/file`、
`/api/config/speedsettings`

## 对集成的意义

**可读**：MDC 的全部配置、任务结果、文件树都能通过 HTTP 拿到。
**不可写**（未验证但很可能可写）：配置端点有对应 `src/api/config/*.rs` 处理器，
`/api/config/provider` 的 `POST /test` 已证实是写端点，故 PUT/POST 配置应可写。

### 任务状态码（实测）

| status | 含义 |
|---|---|
| 0 | 等待中 |
| 2 | 成功（任务 752 详情 `status=2, stage=1000`） |

`stage` 是阶段位掩码（`1000` = 完成）。`trigger`：0 = 手动。

### 任务详情里的三个 JSON 字段（集成数据流的关键）

| 字段 | 内容 | Amane 对应 |
|---|---|---|
| `metadata` | 43 字段最终聚合结果（PascalCase） | `Metadata` 表 |
| `metadata_provider_info` | 每个字段来自哪个源 | `field_sources` |
| `metadata_all_sites` | 各站原始返回快照 | `raw` |
| `parse_result` | 番号解析结果 + `extra_meta`（码/字幕/分辨率/分集） | `MediaFile` 相位字段 |
| `image_result` | 本地 poster/thumb/fanart 路径 | `organize` 产物 |

## MDC 的 provider 类型体系（符号表）

| 类型 | 出现次数 | 说明 |
|---|---|---|
| `MetadataKey` | 93 | 字段键枚举（对应 43 个 metadata 字段） |
| `ProviderName` | 66 | 源名枚举（21 个源） |
| `Provider` | 59 | 源结构体 |
| `Metadata` | 51 | 元数据结构体 |
| `MetadataTrait` | 40 | **元数据 trait**（源按此 trait 提供字段） |
| `ProviderConfig` | 29 | 源配置结构体 |
| `ProviderPriorities` | 28 | 优先级结构体（含 `by_fields` / `ignore_fields`） |
| `Scraper` | 99 | 刮削器 |
| `ScraperContext` | 2 | 刮削上下文 |
| `ProviderSearchConfig` | 6 | 搜索配置 |
| `ProviderError` | 6 | 错误类型 |
