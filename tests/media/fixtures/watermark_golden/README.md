# watermark_golden — 水印默认产物字节基准

在基线 `c412b72` 的隔离检出采集 (规格 §3.7): P3 各触碰水印产物的任务以「相同输入与配置下产物逐字节等于 `expected/`」为验收条件.

## 输入集

`inputs/<name>.jpg` 为叠印前的原始输入: PIL `Image.new("RGB", size, (0, 0, 255))` 经 PIL 默认 JPEG 参数 (quality=75) 落盘. `expected/<name>.jpg` 为基线代码叠印后的产物. 全部用例 scale 默认 0.08、`watermark_dir=None` (内置包)、`jpeg_quality` 默认 95; `corners=None` 等价默认全 TOP_LEFT.

| name | 说明 |
|---|---|
| default-scale-all-phases | 800×538, 默认配置, 中字/无码/破解/4K 同命中 |
| multi-phase-leaked | 800×538, 默认配置, 中字/无码/流出/1080p 同命中 |
| four-corners | 800×538, 显式角位占满四角 (清单见脚本 CASES) |
| sub40-unclamped | 640×480, target_h=round(480×0.08)=38, 未触 16 下限的 <40 边界 |
| small-boundary | 200×30, target_h=max(16, round(2.4))=16, 触 `_STAMP_MIN_HEIGHT` 下限 |
| small-four-corners | 200×30 + four-corners 的显式角位 |
| poster-single-phase | 400×570, 仅中字 |

## 复现

```bash
git worktree add /tmp/golden-c412b72 c412b72 --detach
cd /tmp/golden-c412b72 && uv sync --frozen
uv run python /tmp/verify_golden.py   # 清单见下, 原地覆写 /tmp 副本后与 expected/ 比对
```

验证脚本 (清单即权威口径, 与采集脚本一致):

```python
import shutil
from pathlib import Path

from amane.enums import WatermarkCorner, WatermarkKind
from amane.media.images import apply_cover_watermarks
from amane.parsing import Mosaic

ROOT = Path("tests/media/fixtures/watermark_golden")
FOUR_CORNERS = {
    WatermarkKind.SUBTITLE: WatermarkCorner.TOP_LEFT,
    WatermarkKind.UNCENSORED: WatermarkCorner.TOP_RIGHT,
    WatermarkKind.CRACKED: WatermarkCorner.BOTTOM_RIGHT,
    WatermarkKind.DEFINITION: WatermarkCorner.BOTTOM_LEFT,
}
CASES = [
    ("default-scale-all-phases", dict(has_subtitle=True, uncensored=True, mosaic=Mosaic.CRACKED, definition="4K"), None),
    ("multi-phase-leaked", dict(has_subtitle=True, uncensored=True, mosaic=Mosaic.LEAKED, definition="1080p"), None),
    ("four-corners", dict(has_subtitle=True, uncensored=True, mosaic=Mosaic.CRACKED, definition="4K"), FOUR_CORNERS),
    ("sub40-unclamped", dict(has_subtitle=True, uncensored=True, mosaic=Mosaic.CRACKED, definition="4K"), None),
    ("small-boundary", dict(has_subtitle=True, uncensored=True, mosaic=Mosaic.CRACKED, definition="4K"), None),
    ("small-four-corners", dict(has_subtitle=True, uncensored=True, mosaic=Mosaic.CRACKED, definition="4K"), FOUR_CORNERS),
    ("poster-single-phase", dict(has_subtitle=True, uncensored=False, mosaic=None, definition=None), None),
]
for name, phases, corners in CASES:
    work = Path(f"/tmp/golden-verify-{name}.jpg")
    shutil.copy(ROOT / "inputs" / f"{name}.jpg", work)
    assert apply_cover_watermarks(work, **phases, corners=corners) is True, name
    assert work.read_bytes() == (ROOT / "expected" / f"{name}.jpg").read_bytes(), name
print("golden verified")
```

若需从零重建输入而非复用 `inputs/`: 按上表 size 用 PIL 生成纯色 (0, 0, 255) RGB 图、默认 JPEG 参数落盘, 与 `inputs/` 逐字节一致后重复验证.

字节稳定性前提: 同一 Pillow 版本 (采集与消费两侧 uv.lock 均为 pillow 13.0.0, 自带同版 libjpeg-turbo); 跨 Pillow 大版本允许字节漂移, 届时按本文件流程重新采集.
