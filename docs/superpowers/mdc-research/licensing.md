# MDC 源码与许可状态

> 核实日期 2026-11-12

## 仓库

| 仓库 | 可见性 | 许可 | 内容 | 最后推送 |
|---|---|---|---|---|
| `mdc-ng/mdc-ng` | public | 未声明（容器标签写 AGPL-3.0） | **仅前端资源**（README + `public/` 图标） | 2026-07-14 |
| `mdc-ng/cloudscraper` | public | MIT | Cloudflare 绕过库 | 2025-06-10 |
| `mdc-ng/docker-sonarr` | public | GPL-3.0 | Sonarr 集成 | 2023-02-28 |
| `mdc-ng/mdc_mbot_plugin` | public | 未声明 | 机器人插件 | 2024-06-09 |
| `mdc-ng/oh-my-opencode` | public | NOASSERTION | — | 2026-01-24 |

**后端 Rust 源码（`pkg/core`、`pkg/actress`、`src/api/…`）不在任何公开仓库中。**
Release 资产只有编译好的二进制：

```
v1.36.0 (2026-03-28)  mdc_ng_app_linux_amd64  57949912
                       mdc_ng_app_linux_arm64  55328152
```

## 许可影响

| 项 | 状态 |
|---|---|
| MDC 容器镜像 `mdcng/mdc:latest` 标签 | `AGPL-3.0`（见 `docker inspect` 的 `org.opencontainers.image.licenses`） |
| MDC GitHub 仓库 | **未声明 LICENSE 文件** |
| MDC 二进制内嵌 | 未见 license 文本 |
| Amane | **GPL-3.0**（`LICENSE`） |

### 结论

- **Amane (GPL-3.0) 与 AGPL-3.0 兼容**：AGPLv3 允许以 GPLv3 分发衍生作品。
- 但**重新实现 MDC 功能 ≠ 复制其代码**。功能本身不受版权保护（著作权只保护表达，不保护思想/方法）。
- **安全边界**：逆向得到的**行为规格**（字段名、路由、配置项、算法思路）可用于独立实现；
  **不可**直接搬运 Rust 源码、HTML/CSS/TSX 模板、或反编译得到的代码片段到 Amane。
- 若最终产物包含 MDC 的代码或资源，Amane 需转为 AGPL-3.0 并公开对应源码。

## 因此采用的路线

**黑盒行为逆向 + 独立实现**：以本目录中的
`architecture.md`（模块与职责）、`api_map.md`（接口与语义）、`config_api.md`（配置模型）、
`provider_config.md`（源与优先级）、`mdc_task_detail.json`（数据模型）作为**规格来源**，
在 Amane 的既有架构（插件系统 / 任务系统 / 聚合引擎）内重新实现。

不引用、不移植、不复用 MDC 的任何代码与前端资源；不复用其容器。
