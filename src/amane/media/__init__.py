from .crop import resolve_crop_plan
from .images import (
    CropRotation,
    StampOutcome,
    apply_cover_watermarks,
    apply_cover_watermarks_from_info,
    crop_box,
    crop_poster,
    format_crop_box_args,
    needs_upscale,
    probe_oriented_size,
    probe_size,
    reencode_jpeg,
    rotated_size,
    should_crop_poster,
    stamp_cover,
    validate_crop_box,
)
from .nfo import NfoOptions, write_nfo
from .pipeline import MaterializedImages, manual_crop_image, materialize_images
from .resource_store import AcquireResult, ResourceStore, UpscaleOutcome, derived_locator

__all__ = [
    "AcquireResult",
    "CropRotation",
    "MaterializedImages",
    "NfoOptions",
    "ResourceStore",
    "StampOutcome",
    "UpscaleOutcome",
    "apply_cover_watermarks",
    "apply_cover_watermarks_from_info",
    "crop_box",
    "crop_poster",
    "derived_locator",
    "format_crop_box_args",
    "manual_crop_image",
    "materialize_images",
    "needs_upscale",
    "probe_oriented_size",
    "probe_size",
    "reencode_jpeg",
    "resolve_crop_plan",
    "rotated_size",
    "should_crop_poster",
    "stamp_cover",
    "validate_crop_box",
    "write_nfo",
]
