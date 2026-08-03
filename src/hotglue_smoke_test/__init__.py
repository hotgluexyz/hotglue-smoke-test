"""Smoke-test harness for hotglue taps, targets, and ETLs."""

from hotglue_smoke_test.etl import ETLSmokeRunner, promote_external_ids_to_snapshots
from hotglue_smoke_test.vcr import VCRBaseTestRunner, VCRTapTestRunner, VCRTargetTestRunner

__version__ = "1.1.0"

__all__ = [
    "ETLSmokeRunner",
    "VCRBaseTestRunner",
    "VCRTapTestRunner",
    "VCRTargetTestRunner",
    "promote_external_ids_to_snapshots",
    "__version__",
]
