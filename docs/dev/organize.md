# 整理 (ORGANIZE)

> 源码: `handlers/file.py::OrganizeHandler` (任务入口) 与 `organize/` (落盘单元); 本文只记跨模块契约与边界.
> 协议步骤与失败分支的完整定义见 `organize/overwrite.py` 模块 docstring, 本文不重复.

## 落盘链路

任务入口解析一次生效配置 (`handlers/file.py::resolve_effective_config`, 规格 3.1.3) 后,
`execute_file_operations` 依次执行: `target_state` 预检 → 图片 → 落盘 (`execute_organize`) →
链接 → NFO → 字幕 → `commit_organized_media_file` (索引提交). 路径渲染只有一份实现
(`organize/path_templates.py::resolve_paths` / `resolve_sidecar_paths`), 落盘与清理清单共用.

## 覆写协议 (规格 3.4.2)

分派与协议分离: `execute_file_operations` 的 `OCCUPIED` 预检按 `common.overwrite_video` 分派
(False 维持现状记 `CONFLICT` 跳过; True 进入 `organize/overwrite.py::overwrite_target`),
前置检查 (目标在库内、是视频、不同于源) 是该预检的延伸; 覆写与跳过共用同一次预检.
`common.overwrite_image` 只影响图片写入: False 时目标已有图片跳过并计入结果 `skipped`.

- 提交点 = 目标路径上出现新内容 (`os.replace` 返回成功). 提交点之前失败回滚 (备份还原 + 删
  journal 行); 之后任何失败都不删除目标 —— MOVE 模式下源内容的副本此时只在目标路径上.
- journal (`organize_journal` 表) 是磁盘操作无法进入数据库事务的唯一持久凭据: 阶段 A 独立提交;
  索引接管与 `phase=committed` 在同一个数据库事务内 (`db/repos/organize_journal.py::
  takeover_overwrite_target`), 不存在「索引已接管、journal 未推进」的中间态.
- 指纹 = (path, oshash, size), 恢复时三字段全等才命中, 没有 NULL 跳过分支: SQLite 无
  `AUTOINCREMENT`, id 会被复用, journal 的 `source_file_id` / `target_file_id` 只是线索不是身份.
  阶段 A 现场补算 (`compute_oshash` + stat) 后 journal 指纹恒非空.
- 备份与临时文件名带任务 id (`<目标名>.amane-overwrite-<任务id>` / `.amane-tmp-<任务id>`);
  同一目标同时刻至多一条进行中的覆写, journal 行在案即对后续请求记 `CONFLICT`.

## 中断恢复

恢复入口是 `organize/overwrite.py::recover_organize_journals` (矩阵见模块 docstring), 两处调用:
`AppRuntime` 初始化时安排一次全量扫描; `execute_file_operations` 在占用判定前对命中 journal 的
目标就地恢复 —— 占用判定与覆写分派都基于恢复后的磁盘事实. 身份判定用指纹三字段全等
(按 id → path 次序找候选, 后置复验放行 `path` 已等于 `target_path` 的行, 视为接管已完成);
目标有效性用同一份 journal 指纹对目标现场重算判定. 恢复失败一律保留 journal 行写 `last_error`
交人工处理, 各步幂等, 下次启动或下次整理同一目标时重试.

## 链接路径持久化

`MediaFile.link_path` 记录链接的落盘位置: `execute_file_operations` 在 `create_video_link`
返回 PLACED 后写入实际落盘路径, 对外经 `api/models/media.py::MediaFileResponse` 暴露.
`resolve_paths` 每次按当前模板重渲染, 结果不是历史落盘位置, 读取侧 (NFO 重写等) 以该列为准;
链接文件已不存在时按无链接处理, 不改写该列.

## 导入边界

`organize/overwrite.py` 不得被 `amane.db` 侧导入: `db.models` 已经导入 organize 包, 反向依赖成环.
因此 `Repository` / `MediaFile` 在该模块只进 `TYPE_CHECKING`; 经 `organize/__init__` 导出是安全的.
