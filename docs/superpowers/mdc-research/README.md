# MDC 逆向与集成调研

> 调研日期: 2026-11-12
> 目标系统: MDC (mdc-ng/mdc-core) v1.36.0
> 部署: Docker 容器 `mdc`, 内网地址与数据目录不入库

## 探索方式

- chrome-devtools MCP wedge 卡死 → 降级 `patchright.connect_over_cdp` 连本机已开的 Chrome 调试端口，实际登录 MDC 后遍历全站路由与设置页。
- 容器内 `docker exec` 读二进制与数据库；`nm` + `c++filt` demangle Rust 符号。
- 通过已登录会话 `fetch()` 探测内部 JSON API。

## 落盘文件

| 文件 | 内容 |
|---|---|
| `db_schema.sql` | 两个 SQLite 库的完整 DDL |
| `config_api.md` | 配置 API 的字段级说明 |
| `architecture.md` | 内部架构与模块树 |
| `api_map.md` | HTTP 端点地图 |
| `scraping_orchestration.md` | 刮削编排机制与 stage 码 |
| `provider_config.md` | 21 个刮削源的配置字段 |
| `licensing.md` | 上游仓库与许可状态 |
| `frontend_routes.txt` | Next.js 前端路由清单 |
| `mdc_symbols_clean.txt` | MDC 自身符号（678 条，已去 hash 后缀） |

逆向过程的原始产物 (配置与任务 JSON、界面文本、全量符号表、上游克隆) 不入库, 留在本地调研区。

## 关键结论（速览）

1. **MDC 是 Rust 写的**（crate `mdc_ng_app`），不是 Go。Next.js 前端 + actix-web 后端。
2. **有完整的 JSON 配置 API**（11 个 `/api/config/*`，GET + PUT），配置可读写同步。
3. **有任务查询 API**：`/api/tasks`、`/api/tasks/{id}`、`/api/manual-jobs`、`/api/version`、`/api/file_tree?root=`。
4. **数据模型与 Amane 高度对应**：`metadata` ↔ Amane `Metadata`；`metadata_provider_info` ↔ `field_sources`；`metadata_all_sites` ↔ `raw`。
5. **MDC 无公开插件系统**：刮削器写死在 `pkg/core/src/scraper/providers/`。
6. **MDC 是 AGPL-3.0**，源码公开（github.com/mdc-ng/mdc-core）。

详见 `architecture.md` 与 `config_api.md`。
