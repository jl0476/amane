# MDC 刮削编排机制（从任务日志与配置反推）

> 来源：`/api/tasks/752/logs` 实测（31 条），以及 `/api/config/*` 与源码路径符号。

## stage 码（任务阶段）

| stage | 含义 | 实证日志 |
|---|---|---|
| 100 | 准备 | `Version: v1.36.0`、`视频文件路径: …`、`番号识别结果: 'ADN-426'` |
| 200 | 刮削 | `开始从以下[11]个站点并行抓取数据: dmm, Mgstage, Javlibrary, hbox_jp, Javdb, Javbus, jav321, 7mmtv, airav_io, freejavbt, …` |
| 300 | 图片 | — |
| 400 | 整理 | — |
| 500 | NFO / 元数据落盘 | — |
| 600 | 收尾 | — |
| 1000 | 完成（task.status=2） | — |

日志表结构：`log(id, task_id, stage, level, message, session_id)`，
`task_log_session(id, task_id, session_type, created_at)` 记录一次任务的多次会话。

## 三个关键机制

### 1. 并行抓取

日志明确：`开始从以下[11]个站点并行抓取数据`。站点列表 = 全局优先级 ∩ 该分类，
再叠加字段优先级涉及的站点，**去重后一次性并发发起**。

> Amane 当前是**两段并发**（`NEEDS_PARTIAL` 走第二段），MDC 是一段全并发。

### 2. 防流控冷却锁

```
检测到防流控设置，等待其它线程完成任务... <Javdb>
等待防流控冷却 10s... <Javdb>
防流控结束，开始刮削 <Javdb>
```

- 源级配置项 `cooldown_seconds`（实测 Javdb = 10，其余源 = 0）
- 同一源的请求必须串行且间隔 ≥ cooldown
- 冷却期间占用一个锁，其它线程等待

对应二进制符号：`mdc_ng_app::create_cooldown_lock_map`。
Amane 有 per-host 平滑漏桶限速（容量 1、不允许突发），语义相近但 MDC 是**显式冷却秒数**，
且 MDC 的「防流控」判定来自源的 403 响应（源级冷却是被动触发）。

### 3. 403 自动过墙（FlareSolverr）

```
<MgStage> 检测到 403，开始尝试 FlareSolverr 过墙
```

- 配置项：`/api/config/metadata.flaresolverr`（本机实测 `http://<REDACTED>:8191/v1`）
- 触发点：HTTP 403
- 实现：MDC 内置 `deeplx`（本地 DeepL 翻译）与 `curl-impersonate`（TLS 指纹伪装）

Amane 对应能力：`net/errors.py::classify_block` 拦截判定表 + `solver` 浏览器后端
（FlareSolverr 兼容）。**语义几乎一致**，Amane 还有 `bsk`/`cdp` 两种 MDC 没有的过墙途径。

## 逐条目配置覆盖模型

MDC 的 8 个配置 section：

```
common, watermark, parser, naming, download, metadata, provider, nfo
```

**全局**在 `/api/config/<section>`；**逐条目覆盖**在：

| 载体 | 覆盖 API |
|---|---|
| 手动任务 | `GET /api/manual-jobs/{id}/config` |
| 目录监控 | 同一套 section（前端路由 `settings/watch-dir/[id]/[section]`） |
| 文件管理条目 | 前端路由 `filemanager/@modal/(.)[id]/[section]` |

实测 `/api/manual-jobs/11/config` 返回：

```json
{"common":null,"watermark":null,"parser":null,"naming":null,
 "download":null,"metadata":null,"provider":null,"nfo":null}
```

**每个 section 独立**：null = 继承全局，非 null = 该条目专用覆盖。
这是「一套全局默认值 + 按需局部覆盖」的模型。

Amane 当前只有**全局配置**，无逐条目覆盖机制
（Amane 有 `task.payload` 与 `OrganizePayload` 可传参，但没有配置覆盖层）。

## MDC 的分类识别

`/api/config/provider` 的两个映射：

```json
"series_type_keywords":         {"jav_censored":[],"jav_uncensored":[],"jav_amateur":[],"cn":[],"ea":[]},
"series_type_keywords_in_path": {"jav_censored":[],"jav_uncensored":[],"jav_amateur":[],"cn":[],"ea":[]}
```

- 两者都是**按分类的关键词列表**
- `_in_path` 版本匹配**完整路径**，另一个匹配**文件名**
- 界面上分为「自定义识别词（番号）」与「自定义识别词（路径）」，各 5 类（有码/无码/素人/国产/欧美）

分类键名：`jav_censored` / `jav_uncensored` / `jav_amateur` / `jav_fc2` / `cn` / `ea`
（还有 `cn_parser.rs`、`standard_parser.rs`、`fallback_parser.rs`、`janime_parser.rs`、
`jav_parser.rs` 多种解析器）。

> Amane 的 `content_type` 键名是 `censored`/`uncensored`/`chinese`/`western`/`fc2`/`amateur`/`hentai`。
> **注意 Amane 多一个 `hentai`（里番），MDC 没有**；MDC 的 `jav_fc2` 对应 Amane 的 `fc2`。
> MDC 的 `ea`（欧美）与 Amane 的 `western` 对应。

## MDC 源清单（21 个，配置实测）

`Carib` `Fc2` `Javbus` `Javdb` `Javlibrary` `Madou` `Madouqu` `Mgstage` `Mmtv`
`ThePornDB` `airav_io` `avbase` `avmoo` `avsox` `dmm` `fc2_hub` `freejavbt` `hbox_jp`
`jav321` `miss_av` `xiao_huang_shu`

**已启用 18 个**，禁用 3 个：`avbase`、`avmoo`、`avsox`。

界面上还出现但不在配置里的：`Javdb` 的 `CD: 10s`（即 Javdb 的 cooldown=10）、
`7mmtv`（在日志的 11 站列表里，但配置里没有 → 可能是硬编码源）。

README 声称「30+ 刮削源」，配置里 21 个。差异部分可能来自：
- 硬编码源（7mmtv、Miss_av、Mmtv 等在 `providers/` 目录有实现但配置里缺失）
- 界面上以 `Airav_io` / `Fc2_hub` 等形式列出的别名

## 与 Amane 的能力缺口（按 MDC 机制）

| MDC 机制 | Amane 现状 | 缺口 |
|---|---|---|
| 逐条目配置覆盖（8 section） | 无 | **需新建配置覆盖层** |
| 源级冷却秒数 | `rate_limit`（秒/请求，漏桶） | 语义近似，可对齐 |
| 11 站一段全并发 | 两段并发（`NEEDS_PARTIAL`） | 需评估 |
| 403 → FlareSolverr 自动过墙 | `classify_block` + `solver` 后端 | 已有 |
| 用户自定义番号/路径识别词 | 内置规则，无用户入口 | **需新建** |
| 5 种整理模式 | organize 任务，语义不同 | **需对齐** |
| 命名 Jinja2 模板引擎 | 路径模板（`path-template-schema`） | **需核对差距** |
| 人脸识别裁剪 | `crop_poster` + 固定比例 | MDC 有 `rustface` 人脸识别 |
| 日亚/Tenhow 高清海报 | 无 | **需新建** |
| 水印布局（偏移/间距/布局方式） | `scale` + `corners` | 颗粒度差距大 |
| 演员源 Wikipedia/minnano/graphis | minnano/wikipedia/theporndb/avbase/gfriends/javdb | **多出 graphis** |
