# 设计规格：水印系统（MDC 迁移 P3）

> 日期: 2026-11-17
> 状态: 已实现（第 2 版；design-plan 阶段经 R0001–R0004 四轮审核 PASS，implementation 阶段 PASS）
> 范围: MDC 能力迁移第三阶段 —— 逐类开关、逐类固定位置、水印样式目录化、布局模式、偏移/间距/起始可配、作用目标、自定义目录、水印覆盖节激活、预览端点
> 上游总纲: `docs/superpowers/mdc-roadmap.md`「P3 — 水印系统」
> 前置: P2 已实现于 `feat/mdc-p2-image-pipeline`（`c412b72`）；本文引用的覆盖层 / 生效配置传递 / 整理期重选均以其落地形态为基线

## 0. 已定决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | 逐类开关 | `kinds: dict[WatermarkKind, bool]`，默认全 `true`（等价现状「全部相位都加」）；相位推导后按开关过滤 |
| 2 | 逐类固定位置 | `corners` 值域扩展为 `WatermarkCorner \| AUTO`，默认全 `TOP_LEFT`（现状值不变）；显式角位 = 钉死（现状语义），`AUTO` = 跟随布局模式分配 |
| 3 | 布局模式 | `layout: Literal["stack", "clockwise"]` 默认 `"stack"`（现状：同角多枚向内叠）；`clockwise` = 多枚角标按相位顺序顺时针轮转分配到四角 |
| 4 | 偏移/间距/起始 | `offset_x` / `offset_y: int = 0`（在自动 pad 基础上叠加）、`gap: int \| None = None`（None = 现状自动推导）、`start: int = 0`（clockwise 的起始角序号，stack 忽略） |
| 5 | 作用目标 | `targets: frozenset[Literal["thumb", "poster", "fanart"]]` 默认 `{thumb, poster}`（现状）；`fanart` = extrafanart 每张都叠水印 |
| 6 | 水印样式 | `style: str = "default"`；样式目录化 `watermarks/{style}/{stem}.png`；内置包仍为 default 一套 |
| 7 | 自定义目录 | `custom_folder: Path \| None = None`（None = `{data_dir}/watermarks/`），优先级最高 |
| 8 | 默认产物逐字节不变 | 全部新字段默认值 = 现状行为；`style="default"` 的查找链兼容旧位置 `{data_dir}/watermarks/{stem}.png` |
| 9 | 覆盖节激活 | `watermark` 覆盖节从预留变激活（P2 metadata 节同款模式）；水印发生在整理期 → **库级生效**，刮削期不加水印（现状不变） |
| 10 | 预览端点 | `POST /api/libraries/{id}/preview/watermark`：按库级生效配置对指定内部资源图试算叠印，返回 `image/jpeg`（与真实落盘产物同编码），不落盘 |
| 11 | 水印不进 Resource 缓存 | 水印只作用于库路径产物（现状不变）；配置变化 → 重新整理生效，无需重刮 |
| 12 | MDC `resolution_style` 不迁移 | Amane 的 definition 相位按实际分辨率取章（`_stamp_stems`），MDC 的「分辨率样式变体」无对应缺口 |

## 1. 目标

把 MDC 的「水印系统」能力以原生 Python 实现进 Amane：逐类开关与逐类固定位置、水印样式目录化、布局模式、偏移/间距/起始可配、作用目标扩展到 fanart，**默认配置下产物逐字节不变**。

**成功标准**（可验证）：

1. 关闭某一 `WatermarkKind` 后重新整理，该相位角标从产物上消失，其余相位不变。
2. 某类 `corners` 配为显式角位 → 钉死该角（现状语义）；配为 `AUTO` → 由 `layout` 决定角位。
3. `layout="clockwise"` 时多枚角标按相位顺序顺时针轮转占角；`start` 平移起始角。
4. `offset_x` / `offset_y` / `gap` 改变水印落点；`gap=None` 时与现状逐字节一致。
5. `targets` 含 `fanart` → extrafanart 每张叠水印；不含 thumb/poster → 对应产物不叠。
6. `style` 切换后章图从对应样式目录解析；`custom_folder` 优先于一切。
7. `watermark` 覆盖节四层合成生效；库级覆盖对整理期产物生效（刮削期不加水印）。
8. 预览端点按库级生效配置返回叠印结果，不落盘、不写 Resource。
9. 全部新字段取默认值时，产物与升级前逐字节一致（含已开启水印的存量库）。

**非目标**：

- MDC `resolution_style`（分辨率章样式变体）→ 不迁移（决策 12）。
- 文字水印 / 动态生成章图 → 不实现（PNG 相位制不变）。
- 水印进 Resource 缓存或刮削期物化 → 不实现（决策 11）。
- `WatermarkStylePlugin` 插件契约 → P4。
- 水印参数变化触发自动重整理 → 不实现（重新整理即生效）。

## 2. 现状与约束

### 2.1 水印管线现状

- `src/amane/media/images.py::apply_cover_watermarks`：相位 → stems（`_stamp_stems`：中字/无码/破解/流出/分辨率，顺序固定）→ `load_stamp` 逐枚取图 → `_fit_stamp`（裁透明边 + 按高度缩放）→ 按角分组 → `_paste_stamps` 叠印 → JPEG 落盘。异常吞掉记 warning 返回 False。
- `_paste_stamps` 布局：同角多枚「上角往下、下角往上」向内叠，右角右对齐；`pad = max(4, target_h // 8)`、`gap = max(4, target_h // 10)`、`target_h = max(_STAMP_MIN_HEIGHT, round(height × scale))`（scale 钳 0.03–0.25）。
- `_corner_for`：按 stem 反查 `WatermarkKind`，取 `corners[kind]`，缺 key 补 `TOP_LEFT`。
- `src/amane/media/watermarks/__init__.py::load_stamp`：stem 合法性（小写字母数字）→ 用户目录 `{data_dir}/watermarks/{stem}.png` 优先（损坏回退内置）→ 内置包 PNG。
- 调用点：仅整理期 `handlers/file.py::_place_library_images`（thumb / poster 两个目标，`overwrite_image=True` 时对既有图叠印）；刮削期 `materialize_images` 不加水印。
- `watermark_dir` 参数链：`handlers/file.py` 一路从 `data_dir` 派生传入。
- `WatermarkConfig`（`config/manager.py:520`）：`enabled=false` / `scale=0.08` / `corners` 默认全 `TOP_LEFT`；已挂 `HotSettings.watermark`。
- `watermark` 覆盖节**预留**（`config/overrides.py` 白名单拒绝，422）。

### 2.2 MDC 规格（对照）

`/api/config/watermark`（`mdc-research/config_api.md:143`）：`enabled` / `style`（`OldRed` 等多套）/ `mode`（`Clockwise`）/ `resolution_style` / `scale`（整数）/ `start` / `gap` / `offset_x` / `offset_y` / `custom_folder` / `thumb` / `poster` / `fanart`（作用目标）/ 6 个 `watermark_*` 相位开关 + 6 个 `watermark_*_fixed`（0–3 = 角位，5 = 自动）。

Amane 侧对应方案以 roadmap「P3」表的方案列为准（见 §0 各决策），不复刻 MDC 的值域（`scale` 保持比例语义、相位集合用 `WatermarkKind` 5 类）。

### 2.3 与 P2 的交互约束

- 叠印顺序：裁剪 → 超分 → 水印（既有契约，P2 未改）。
- 整理期重选（P2）产出的 poster 经由 `_place_library_images` 复制路径 → 照常叠水印。
- `overwrite_image=False` 跳过的产物不重写 → 水印也随之不动（现状语义保留）。

## 3. 设计

### 3.1 配置面：`WatermarkConfig` 扩展与覆盖节激活

`WatermarkConfig`（`extra="forbid"`）新增字段：

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `kinds` | `dict[WatermarkKind, bool]` | 全 `true` | 逐类开关；相位推导后过滤，`false` 的相位不取章 |
| `corners` | `dict[WatermarkKind, WatermarkCorner \| AUTO]` | 全 `TOP_LEFT` | 值域扩展；`AUTO` 跟随 `layout`，显式角位钉死 |
| `layout` | `Literal["stack", "clockwise"]` | `"stack"` | 布局模式（§3.3） |
| `offset_x` / `offset_y` | `int`（≥ 0） | `0` | 在自动 `pad` 基础上的像素偏移（x 向内收、y 向下推；下角 y 向上） |
| `gap` | `int \| None`（≥ 0） | `None` | 同角相邻两枚的间距；`None` = 现状推导 `max(4, target_h // 10)`（含下限，stack 与 clockwise 一致） |
| `start` | `int`（0–3） | `0` | `clockwise` 的起始角序号（0=左上 1=右上 2=右下 3=左下）；`stack` 忽略 |
| `targets` | `frozenset[Literal["thumb", "poster", "fanart"]]` | `{thumb, poster}` | 作用目标集合 |
| `style` | `str`（`^[a-z0-9_-]+$`） | `"default"` | 样式名（§3.2） |
| `custom_folder` | `Path \| None` | `None` | 自定义水印目录；`None` = `{data_dir}/watermarks/` |

沿用既有契约：payload 禁 null、`extra="forbid"`、`corners` 的 frozen-keys 校验扩展到新值域。**现状 `WatermarkConfig` 尚无 `extra="forbid"`**（`config/manager.py:520`，仅 enabled/scale/corners 三字段）——本期扩展时补齐，未知键 422。`enabled` / `scale` 语义不变。

**兼容不变量**：以上全部默认值组合起来 = 现状行为（逐字节）。

`watermark` 覆盖节激活：`config/overrides.py` 白名单加入（预留拒绝列表移除 watermark），`EffectiveConfigResponse` 扩展 watermark 节，API/client/i18n 联动（P2 metadata 节同款：`just generate` + `web/src/i18n`）。水印发生在整理期 → 支持**库级覆盖**：`resolve_effective_config` (+watermark 节) → `EffectiveOrganizeConfig` → `_place_library_images`。

### 3.2 样式目录化与查找链

`load_stamp(stem, data_dir_root, custom_folder, style)`（双根签名：data_dir 根 + custom_folder 经配置单独透传）查找链，**custom_folder 非 None 时为完全替代**（data_dir 系各级不参与）：

```
custom_folder 非 None：
  1. custom_folder / {stem}.png → 4. 内置包
custom_folder 为 None：
  2. {data_dir_root}/watermarks/{style}/{stem}.png   （仅 style ≠ "default"）
  3. {data_dir_root}/watermarks/{stem}.png           （旧位置；仅 style = "default" —— 兼容存量用户）
  4. 内置包 {stem}.png                                （包结构不变，default 一套）
```

- 共四级；style ≠ "default" 时旧位置（第 3 级）**不兜底**——防止 style 用户静默混入旧图。
- 用户文件损坏 → 记 warning 回退下一级查找（现状语义）。
- `style` 非法字符在配置层 422（`^[a-z0-9_-]+$`），路径拼接前不再二次过滤。
- `watermark_dir` 既有单参数链退役：`images.py` 的水印行为函数改为接收 data_dir 根与 custom_folder 两个来源（由调用方从生效配置解析），避免单 Path 参数承载两个语义。
- `custom_folder` 指向不存在目录 → 第 1 级跳过（等同无文件）。

### 3.3 布局模式

- `stack`（默认，现状）：同角多枚向内叠（上角往下、下角往上），右角右对齐。`pad`、`gap` 自动推导规则不变；`offset_x` / `offset_y` 在 pad 落点基础上叠加（上角 y = pad + offset_y，下角 y = height − pad − stamp_h − offset_y，x 同理向内收）。
- `clockwise`：按相位顺序（`_stamp_stems` 顺序，经 `kinds` 过滤后）依次分配角位，顺时针轮转 `TOP_LEFT → TOP_RIGHT → BOTTOM_RIGHT → BOTTOM_LEFT → TOP_LEFT …`，起始角 = `start`；同角第二枚起沿用向内叠。显式 `corners`（非 AUTO）钉死的相位**不参与轮转**但**占据其角位的轮转位置**（按相位序占位，防止后继相位落到同一角）；**四角全部被显式钉死时**，AUTO 相位从 `start` 起始角向内叠（与同角第二枚同规则）。
- `AUTO` 角位解析：`stack` 下 AUTO = `TOP_LEFT`（现状缺 key 补齐语义）；`clockwise` 下 AUTO = 按上述轮转。
- `gap=None` 的推导为 `max(4, target_h // 10)`（含下限），stack 与 clockwise 一致。

### 3.4 作用目标与 fanart

- `targets` 决定 `_place_library_images` 内哪些产物叠水印：`thumb` / `poster` 判定沿用现状（`*_written or overwrite_image` 且文件存在）；`fanart` 为新增分支 —— extrafanart 复制后对每张叠水印（`overwrite_image=False` 跳过的张不叠）。
- fanart 叠水印使用同一 `apply_cover_watermarks_from_info` 入口（同一 file_info 口径）。
- 刮削期不加水印（现状不变）；`materialize_images` 无水印参数。

### 3.5 水印与裁剪/超分顺序

裁剪 → 超分 → 水印（既有契约不变）。水印只作用于库路径最终产物，参数变化重新整理即生效。

### 3.6 预览端点

`POST /api/libraries/{library_id}/preview/watermark`，请求体 `{url: <内部资源 URL>, phase: {has_subtitle: bool, uncensored: bool, mosaic: Mosaic \| None, definition: str \| null}}`（`phase.mosaic` 取 `parsing/file_info.py::Mosaic` 三成员或 null 缺省，与 FileInfo 同口径）：

- **相位显式传入**（与 `FileInfo` 同口径四元组，必填）——预览是配置试算工具，相位由调用方（前端从条目当前信息填充）给定，端点不做资源反查，保证确定性；`kinds` 过滤在预览中同样生效。
1. 取该库级生效 `WatermarkConfig`（覆盖层合成，与整理期同一口径）。
2. 经 `ResourceStore.resolve` 取源图（不存在 404）。
3. 复制到临时文件按配置叠印（不落库、不写 Resource、临时即删）。
4. 返回 `image/jpeg`（沿用现状 `_save_jpeg` 保存语义，与真实落盘产物同编码）。

失败路径：源不可读 / 叠印异常 → 422 带原因，不返回半成品。

### 3.7 默认产物逐字节不变

- 全部新字段默认值 = 现状行为（§3.1 表）；`style="default"` 查找链兼容旧位置（§3.2 第 3 级）。
- **字节基准（golden fixture）**：在任务 1 **改动产物路径之前**，在基线 `c412b72` 的隔离检出（git worktree，不移动被审工作树 HEAD）对固定输入集生成基准字节入库（含默认 scale、`target_h < 40` 的小图边界与四角组合）；此后每个触碰水印产物的任务以「默认配置产物 == golden」为 GREEN 条件，杜绝「用已改代码生成基准」的自证循环。
- 验收基准：既有水印测试（角位、叠印顺序、scale 钳制）原样通过；`gap=None` 与现状推导（含 `max(4, …)` 下限）逐字节一致。

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| `kinds` 值非法（非 bool） | 422（Pydantic 校验） |
| `corners` 值非法（非枚举/非 AUTO） | 422 |
| `layout` / `targets` / `style` 非法 | 422（`targets` 未知成员拒绝、`style` 字符集拒绝） |
| `offset` / `gap` / `start` 越界 | 422（字段级 ge/le） |
| 未知配置键 | 422（`extra="forbid"` 本期补齐） |
| 某相位章图缺失 | 跳过该枚（现状语义），不回退文字 |
| 用户章图损坏 | 记 warning 回退下一级查找（现状语义） |
| `style` 目录不存在 | 该级跳过，回退下一级查找 |
| `custom_folder` 指向不存在目录 | 第 1 级跳过（等同无文件） |
| fanart 单张叠印失败 | 该张记 warning 不中断其余张 |
| 预览源不可读 / 叠印异常 | 422 带原因 |
| 覆盖 payload 禁 null | 422（沿用既有契约） |

## 5. 测试策略

遵循 `docs/dev/testing.md`：表测试优先、必须含边界与非法用例。

| 测试对象 | 要点 |
|---|---|
| `WatermarkConfig` 校验 | 新字段默认值、kinds/corners/targets/style/offset/gap/start 的非法值 422、未知键 422（extra=forbid 补齐）、frozen-keys 兼容 |
| 相位过滤 | `kinds=false` 的相位不出现；多类组合表测试 |
| AUTO/显式角位 | AUTO 跟随 layout；显式角位钉死；混合配置；四角全钉死时 AUTO 回退 |
| `clockwise` 布局 | 相位→角位轮转表测试；`start` 平移；显式角位占位不轮转；`gap` 生效 |
| `offset` | 上下左右四角的落点像素断言 |
| 样式查找链 | 四级链逐级命中表测试（含 custom_folder+style 组合行）；旧位置兼容（style=default）；**style≠default 旧位置不兜底负向行**；损坏回退；custom_folder 不存在 |
| 作用目标 | targets 组合 × 产物存在性；fanart 多张逐张叠印；overwrite_image 交互 |
| 默认逐字节 | golden fixture（基线 `c412b72` 采集，含 target_h < 40 小图）与默认配置产物字节一致 |
| 覆盖节 | PUT/GET/DELETE 往返、库级覆盖生效、payload 禁 null、未知键 422、四层合成 |
| 预览端点 | 200 image/jpeg（魔数断言）、相位随请求变化、404（源缺失）、422（叠印失败）、不落盘断言 |
| 裁剪→超分→水印顺序 | 回归：顺序与产物不变 |

## 6. 实施顺序（供实现计划参考）

1. `kinds` 逐类开关 + 相位过滤（配置模型 + images.py）
2. `corners` 值域扩展（AUTO）+ `layout="stack"` 显式化（现状回归基线）
3. `clockwise` 布局 + `start` / `offset_x` / `offset_y` / `gap` 可配
4. 样式目录化 + 查找链 + `custom_folder`
5. `targets` 作用目标 + fanart 叠印 + 库级生效配置传递链
6. `watermark` 覆盖节激活 + API/client/i18n 联动
7. 预览端点
8. 文档同步 + 打包冒烟（样式目录为运行时 data_dir 产物，无需打包变更，冒烟确认即可）

## 7. 已知限制

1. `resolution_style` 不迁移 —— definition 相位按实际分辨率取章。
2. 库级 quality/水印的交互：水印在重选产物复制后叠印，`overwrite_image=False` 的既有跳过语义不变。
3. `clockwise` 的轮转规则为本设计的可复现定义（相位顺序 × `start`），非 MDC 逆向的逐位复刻 —— MDC 二进制不可逆向细节，以「可验收、可复现」为准。
4. 预览端点不做并发保护 —— 只读试算，不落盘，无共享状态。
5. 水印章图为 PNG 相位制，无文字回退 —— 缺章即缺，等待用户提供。
6. Android 壳的水印渲染不在本期验证（桌面与 Docker 闭环）。
