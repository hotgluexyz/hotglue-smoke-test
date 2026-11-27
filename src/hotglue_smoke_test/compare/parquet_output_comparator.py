"""Parquet folder compare for ETL etl-output (``DEFAULT_EXPORT_FORMAT=parquet``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def compare_parquet_folder(
    label: str,
    expected_dir: Path,
    actual_dir: Path,
    test_config: dict[str, Any],
) -> None:
    """Compare *.parquet files by name: Arrow schema, row count, then row values in order.

    Compares Arrow tables directly, so nullable integer widths and values above 2**53
    are not lost to pandas float conversion. Honours ``ignore_files`` (file stem) and
    ``ignore_columns`` (per stem or ``*``, top-level columns).
    """
    ignore_files = set(test_config.get("ignore_files") or [])
    ignore_columns = test_config.get("ignore_columns") or {}

    def names(directory: Path) -> list[str]:
        return sorted(
            p.name for p in directory.glob("*.parquet") if p.stem not in ignore_files
        )

    expected = names(expected_dir)
    actual = names(actual_dir) if actual_dir.is_dir() else []
    assert expected == actual, (
        f"[{label}] parquet file mismatch: expected {expected}, actual {actual}"
    )

    for name in expected:
        stem = Path(name).stem
        dropped = set(ignore_columns.get(stem, [])) | set(ignore_columns.get("*", []))
        exp = pq.read_table(expected_dir / name)
        act = pq.read_table(actual_dir / name)
        exp = exp.drop_columns([c for c in exp.column_names if c in dropped])
        act = act.drop_columns([c for c in act.column_names if c in dropped])

        assert exp.schema.equals(act.schema, check_metadata=False), (
            f"[{label}] {name} schema mismatch:\nexpected {exp.schema}\nactual   {act.schema}"
        )
        assert exp.num_rows == act.num_rows, (
            f"[{label}] {name} record count mismatch: "
            f"expected {exp.num_rows}, actual {act.num_rows}"
        )
        if not exp.equals(act):
            # Arrow decides; Python rows only locate the difference (they can hide
            # sub-microsecond values, so a mismatch may not show up in them).
            for index, (exp_row, act_row) in enumerate(zip(exp.to_pylist(), act.to_pylist())):
                assert exp_row == act_row, (
                    f"[{label}] {name} row {index} mismatch:\n"
                    f"expected {exp_row}\nactual   {act_row}"
                )
            raise AssertionError(f"[{label}] {name} values differ (below Python precision)")
        print(f"Success! Parquet comparison [{label}] {name}: {act.num_rows} records")
