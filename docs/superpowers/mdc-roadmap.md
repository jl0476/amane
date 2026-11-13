# MDC 能力迁移总纲

> 范围决策（2026-11-12，用户选定）：**全部主要功能迁移** · **原生 Python 重写** · **核心内置 + 可插拔部分插件化**
> 命名引擎：**两者兼容**（自研语法为默认，Jinja2 兼容层可选）
> 人脸识别裁剪：**引入 OpenCV**

## 背景

MDC（mdc-ng/mdc-core v1.36.0）是面向 Emby/Jellyfin 的影片刮削与文件整理流水线。
Amane 是以番号为键的元数据中枢。二者互补而非替代：MDC 强在「让媒体库好看且规整」，
Amane 强在「元数据可追溯、可聚合、可被任何下游消费」。

本总纲把 MDC 的 25 个能力缺口逐条映射到 Amane 的扩展点，作为长期路线图。
**本文件是路线图，不是实现规格** —— 每个阶段另出正式规格文档。

逆向材料在 `docs/superpowers/mdc-research/`（架构、API 地图、配置字段、数据模型、许可分析）。

## 许可边界

- MDC 后端 Rust 源码**不公开**（release 只发二进制），容器标签 AGPL-3.0。
- Amane 是 GPL-3.0，与 AGPL-3.0 兼容，但**不搬运 MDC 代码**。
- 迁移方式是：**黑盒行为逆向 → 独立实现**。
  可用：字段语义、配置结构、算法思路、接口形状。
  不可用：Rust 源码、Next.js 模板、CSS、二进制反编译出的代码片段。

## 阶段划分

依赖关系决定顺序：P0 是其余全部的基础设施。

```
P0 配置基础层
 ├── P1 命名与整理
 ├── P2 图片处理链
 └── P3 水印系统
       └── P4 插件 API v2（把 P1–P3 的可插拔部分开放给插件）
```

---

## P0 — 配置基础层

| 缺口 | MDC 规格 | Amane 现状 | 方案 |
|---|---|---|---|
| 逐条目配置覆盖 | 8 个 section（common/watermark/parser/naming/download/metadata/provider/nfo），每个独立 `null` = 继承全局 | 只有全局 `HotSettings` + `Library` 行复制式覆盖 | 新增**配置覆盖层**，见 P0 规格 |
| 用户自定义识别词 | `series_type_keywords` / `series_type_keywords_in_path`，各 5 类（有码/无码/素人/国产/欧美），番号优先于路径 | `parsing/file_info.py` 是纯函数包，**不能读配置** | 识别词存 DB（仿 `FacetRule` 表驱动范式），`parsing/` 接收规则参数化输入 |
| 源级冷却秒数 | `cooldown_seconds`（显式秒数，403 触发） | `rate_limit`（秒/请求，漏桶） | 语义对齐，保留 Amane 漏桶为主，冷却作为补充 |

**P0 的关键技术难点**：Amane 的 `HotSettings` 用 `exclude_none=True` 落盘，
而「显式 null = 继承」需要 null 有语义。解决方向：覆盖层用**独立的 DB 表**存稀疏 JSON，
不用 Pydantic 模型表达 null。

---

## P1 — 命名与整理

| 缺口 | MDC 规格 | Amane 现状 | 方案 |
|---|---|---|---|
| 整理模式 | 5 种：硬链接/软链接/复制/移动/原地 | 4 种（`MoveMode`），原地无独立语义 | 加 `inplace` 模式 |
| 命名模板引擎 | Jinja2，30+ 变量，条件判断，filter，降级链 | 自研 `{name}` + 可选组 + 值映射，31 变量 | **两者兼容**：自研引擎扩展（加条件块/filter/降级链）+ Jinja2 兼容层 |
| 命名分类映射 | `category_mapping` + `category_rules`（正则覆盖，按序首条命中） | 只有 `{content_type}` 占位符 | 新增映射规则表 |
| 文件前后缀 | `prefixes{}` / `suffixs{}` / `title` / `file` / `folder` / `max_actor_count` / `max_folder_length` / `image_with_prefix` | 无独立前后缀概念 | 由模板表达即可，不单独建模 |
| 命名预览与试算 | `POST /api/config/naming/preview` + 测试用例 CRUD | 无 | 新增预览端点 + 前端试算面板 |
| AI 命名模板 | `pkg/ai` + `api::ai`，自然语言 → Jinja2 | 无 | 复用 Amane `llm` 段与 `agent` 能力 |
| NFO 内容开关 | 30+ 个 `enable_*` + `additional_tags` + `tagline_format` + `set_by_*` | **NFO 内容固定，无任何开关** | NFO 字段级开关模型 |
| NFO 单独重写 | `PUT /api/resources/tasks/{id}/nfo` | 无 | 新增端点 |
| 覆写策略 | `target_overwrite_pic` / `target_overwrite_video` | 冲突即跳过 | 新增覆写选项，保留默认跳过 |
| 整理动作历史 | `reorganize_tasks` + `ReorganizeDetail` | 只有任务日志 | 用任务系统承载（不必新建表） |
| 字幕独立目录 | `subtitles_folder`（字幕独立目录） | 字幕跟随 `{link_dir}` | 路径模板已支持，独立目录由模板表达 |
| 剧照后台场景 | `extra_fanarts_behind_scenes` | 无 | 走 download_resources 扩展 |

**Amane 领先、迁移时不得丢失**：
- 超分模块（`src/amane/sr/`）—— MDC 完全没有
- 路径逃逸防护（`PathEngine.resolve` + `safe_dirs`）
- 视频扩展名始终取源文件（`video_dest`）
- 模板占位符 schema 下发前端（`/api/libraries/path-template-schema` + `map_keys`）

---

## P2 — 图片处理链

| 缺口 | MDC 规格 | Amane 现状 | 方案 |
|---|---|---|---|
| 分类化裁剪 | 5 类：`crop_type_censored/uncensored/cn/amateur/fc2`（Right/None/Face） | 1 种固定右侧比例 | `dict[ContentType, CropStrategy]`，复用 `DictField` 范式 |
| 人脸识别裁剪 | `rustface` + Seeta FD 模型 | **完全没有** | **OpenCV**（YuNet / Haar），作为可选依赖 |
| 高清海报源 | 日亚（1778×2529）+ Tenhow（1055×1500） | 无 | 做成**插件能力**（P4），内置不实现 |
| 下载策略 | `thumb_download_strategy`（Priority / 图片质量择优）+ `image_strict_mode` | 顺序优先，第一个成功即停 | 新增「质量择优」策略：访问全部候选比分辨率/体积 |
| 剧照后台场景 | `extra_fanarts_behind_scenes` | 无 | 同 P1 |
| 海报裁剪比例 | `crop_aspect`：完整海报 2.12/3 vs Emby 2/3 | `poster_ratio` 单一比例 | 新增比例枚举 |
| 裁剪结果优先 | 比较裁剪质量与原海报分辨率 | 无 | 新增比较策略 |

**P2 风险点**：OpenCV 在桌面版（Swift/.NET AOT）与 Android 壳的分发体积与打包方式。
需要确认能否做成 optional extra。

---

## P3 — 水印系统

| 缺口 | MDC 规格 | Amane 现状（3 字段） | 方案 |
|---|---|---|---|
| 逐类开关 | 9 个 `watermark_*` 布尔 | 无逐类开关 | `dict[WatermarkKind, bool]` |
| 逐类固定位置 | 5 个 `*_fixed`（0-3 或自动） | `corners` 是「按类选角」，无「固定不移动」语义 | `corners` 值域扩展为「角位 + 固定/自动」 |
| 水印样式 | `style: OldRed` 等多套 | 固定内置 PNG，只能同名覆盖 | 样式目录化：`{data_dir}/watermarks/{style}/{stem}.png` |
| 布局模式 | `mode: Clockwise` 等 | 固定「上往下/下往上」单向 | 新增布局枚举 |
| 偏移/间距/起始 | `offset_x` / `offset_y` / `gap` / `start` | 自动推导，不可调 | 新增可配字段 |
| 作用目标 | `thumb` / `poster` / `fanart` 三选 | 固定 thumb+poster，fanart 永不加 | 新增目标集合 |
| 自定义目录 | `custom_folder` | 固定 `{data_dir}/watermarks/` | 新增配置项 |

---

## P4 — 插件 API v2

当前 `PLUGIN_API_VERSION = "1"`，插件只能提供**影片元数据源**与**播放源**。
迁移所需的新扩展点：

| 新能力 | 需要的新契约 | 备注 |
|---|---|---|
| 整理策略 | `OrganizeStrategyPlugin` | 自定义落盘行为 |
| 命名模板 | 模板语法扩展注册 | 允许插件贡献占位符 |
| 水印样式 | `WatermarkStylePlugin` | 贡献水印图与样式 |
| HD 海报源 | `PosterSourcePlugin` | 日亚/Tenhow 等 |
| 演员来源 | `ActorSourcePlugin` | `SourceCapability.ACTOR_PROFILE`/`ACTOR_IMAGE` 枚举已存在但未接入插件侧 |
| 人脸识别后端 | `FaceDetectorPlugin` | 让用户自选检测器 |

**注意**：`docs/dev/plugins.md:93` 的「不支持」清单需同步更新。

---

## 阶段间的共享基础设施

以下能力在 P0 建成，P1–P3 复用：

1. **配置覆盖解析器**：`(全局, 条目覆盖, 任务 payload) -> 生效配置`
2. **配置 schema 驱动的设置页**：Amane 已有 `x-*` 扩展机制，新增 section 自动出现在 UI
3. **模板引擎抽象**：自研 + Jinja2 两套实现，同一接口
4. **预览/试算端点**：命名、裁剪、水印三类改动都需要「先试算再落盘」
5. **测试范式**：表测试优先、边界/非法用例必备（`docs/dev/testing.md`）

---

## 文档落点

| 内容 | 位置 |
|---|---|
| 本总纲 | `docs/superpowers/mdc-roadmap.md` |
| 逆向材料 | `docs/superpowers/mdc-research/` |
| 跨模块契约（配置覆盖语义、模板引擎接口） | `docs/dev/`（随各阶段实现同步） |
| 用户可见行为（命名规则、水印、裁剪） | `docs/user/` |
| 单文件实现原因 | 该文件注释 / docstring |

> `docs/dev/` 只服务 Agent 且要求精简，**总纲不入 `docs/dev/`** ——
> 那是研究材料，不是跨模块契约。
