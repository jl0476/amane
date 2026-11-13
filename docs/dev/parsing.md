# 解析与识别词

> 番号 / 类型解析与识别词匹配的跨模块契约; 函数与匹配语义的细节见 `src/amane/parsing/` 各模块 docstring.

## 纯函数边界

`src/amane/parsing/` 不做 I/O、不读配置. `src/amane/parsing/rules.py::ParseRuleSet` 由调用方从 `parse_rules` 表行构造 (`ParseRuleSpec` 为行投影) 后传入解析函数; 按库缓存与失效在 `src/amane/app/runtime.py::AppRuntime` — 版本键取该库相关行 (含全局) 的 `(count, max(updated_at))`, 规则 CRUD 后重建. 规则是 DB 行, 缓存不随 `_rebuild` 重建 (那由配置文件变化触发).

## 识别词

规则只存在 `parse_rules` 表, 不进 `config_overrides`; 表结构与 `scope_id` 用 0 表全局的原因见 `src/amane/db/models.py::ParseRule`. 匹配契约:

- 作用域按 `(kind, content_type)` 分桶合成: 库级桶有启用规则则整体替换全局同桶; 桶只决定合成, 不决定匹配次序.
- 匹配是单一扁平序列: 番号段全部先于路径段, 段内 `created_at` 升序 (同刻按 id), 首条命中即覆盖内置类型判定. 合成在 `ParseRuleSet.from_specs` 构造时完成, `apply_rules` 只做匹配.
- 路径段匹配调用方传入的原始路径字符串 (非 `path_key` 归一形式); 大小写不敏感由匹配实现按 casefold 处理. `text=` 分支没有路径, 只应用番号段.
- 匹配引擎为 RE2 (`google-re2`): 线性时间从引擎层消除灾难回溯, 反向引用 / 环视编译即拒 (保存与构造都经 `compile_rule_pattern` 校验, API 层映射 422); 长度上限 256 定位是防滥用. Python `re` 不参与识别词匹配.

## 生产入口注入

`rules=` 必须接入全部真实解析入口, 由 `src/amane/app/runtime.py::AppRuntime.parse_rules_for` 按库提供 (含全局合成):

| 入口 | 位置 |
|---|---|
| 扫描 | `src/amane/handlers/refresh.py::RefreshHandler.handle` |
| 整理落盘 | `src/amane/handlers/file.py::apply_file_operations` |
| 任务请求解析 | `src/amane/api/models/tasks.py::ScrapeRequest.build_payload` |
| 清理清单 | `src/amane/library/cleanup/footprint.py::build_footprint` |
| 调度 | `src/amane/scheduler/service.py::WatcherService._on_file_found` |
| agent 提交 | `src/amane/agent/task_ops.py::submit_task` |

字幕配对 (`src/amane/organize/subtitles.py`) 与 Alembic 数据迁移 (一次性重投影) 不注入: 前者走 `text=` 分支本就跳过路径段, 后者保持历史口径.
