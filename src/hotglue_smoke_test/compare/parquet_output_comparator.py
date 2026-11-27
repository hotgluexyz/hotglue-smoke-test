"""Parquet folder compare for ETL etl-output (``DEFAULT_EXPORT_FORMAT=parquet``)."""

from __future__ import annotations

import datetime
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def compare_parquet_folder(
    label: str,
    expected_dir: Path,
    actual_dir: Path,
    test_config: dict[str, Any],
) -> None:
    """Compare *.parquet files by name: file set, schema, row count, row values.

    Honours ``ignore_files`` (file stem), ``ignore_columns`` (per stem or ``*``,
    top-level columns) and ``sort_config`` (per stem). Nested list/struct cells are
    compared by value. Without ``sort_config``, row order must match.
    """
    ignore_files = set(test_config.get("ignore_files") or [])
    ignore_columns = test_config.get("ignore_columns") or {}
    sort_config = test_config.get("sort_config") or {}

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
        dropped = list(ignore_columns.get(stem, [])) + list(ignore_columns.get("*", []))
        exp_df = pd.read_parquet(expected_dir / name).drop(columns=dropped, errors="ignore")
        act_df = pd.read_parquet(actual_dir / name).drop(columns=dropped, errors="ignore")

        assert _schema(exp_df) == _schema(act_df), (
            f"[{label}] {name} schema mismatch:\n"
            f"expected {_schema(exp_df)}\nactual   {_schema(act_df)}"
        )
        assert len(exp_df) == len(act_df), (
            f"[{label}] {name} record count mismatch: "
            f"expected {len(exp_df)}, actual {len(act_df)}"
        )

        exp_rows, act_rows = _rows(exp_df), _rows(act_df)
        if stem in sort_config:
            exp_rows = _sorted(exp_rows, sort_config[stem])
            act_rows = _sorted(act_rows, sort_config[stem])
        for index, (exp_row, act_row) in enumerate(zip(exp_rows, act_rows)):
            assert exp_row == act_row, (
                f"[{label}] {name} row {index} mismatch:\n"
                f"expected {exp_row}\nactual   {act_row}"
            )
        print(f"Success! Parquet comparison [{label}] {name}: {len(act_rows)} records")


def _schema(df: pd.DataFrame) -> dict[str, str]:
    return {column: str(dtype) for column, dtype in sorted(df.dtypes.items())}


def _rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    columns = sorted(df.columns)
    return [
        {column: _plain(value) for column, value in zip(columns, row)}
        for row in df[columns].itertuples(index=False, name=None)
    ]


def _sorted(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: [repr(row.get(key)) for key in keys])


def _plain(value: Any) -> Any:
    """Nested Parquet cells (ndarray/list/struct dict) and NaN/NaT as comparable Python."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp, datetime.datetime, datetime.date)):
        return value.isoformat()
    return value
