"""Compare expected_output/snapshots vs test_runtime/snapshots for ETL smoke."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from hotglue_smoke_test.compare.csv_output_comparator import compare_csv_folder


def compare_snapshots(
    expected: Path,
    actual: Path,
    *,
    label: str,
    test_config: dict[str, Any],
) -> None:
    """CSV via legacy compare_csv_folder; parquet/json pairwise (smoke extension)."""
    if not expected.is_dir():
        return
    assert actual.is_dir(), f"[{label}] missing actual snapshots at {actual}"

    compare_csv_folder(
        label,
        str(actual),
        str(expected),
        {"test_config": test_config},
    )

    expected_extra = sorted(
        p.relative_to(expected)
        for p in expected.rglob("*")
        if p.is_file() and p.suffix.lower() in {".parquet", ".json"}
    )
    for rel in expected_extra:
        exp_path = expected / rel
        act_path = actual / rel
        assert act_path.is_file(), f"[{label}] missing snapshot file {act_path}"
        if exp_path.suffix.lower() == ".parquet":
            pd.testing.assert_frame_equal(
                pd.read_parquet(exp_path),
                pd.read_parquet(act_path),
                check_dtype=False,
            )
        else:
            assert exp_path.read_text() == act_path.read_text(), (
                f"[{label}] snapshot json mismatch: {rel}"
            )
