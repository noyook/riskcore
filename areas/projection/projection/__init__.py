"""projection package — regulatory read-side projections (areas/projection/)."""
from .service import ProjectionService
from .stamp import assert_stamped, build_stamp, stamp_line, strip_regenerated_at

__all__ = [
    "ProjectionService",
    "assert_stamped",
    "build_stamp",
    "stamp_line",
    "strip_regenerated_at",
]
