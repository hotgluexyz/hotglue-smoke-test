"""Compare ETL smoke expected_output/ vs test_runtime/ (per UTC datetime folder)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import pandas as pd

from hotglue_smoke_test.artifacts import list_etl_datetime_dirs
from hotglue_smoke_test.compare.singer_output_comparator import SingerOutputComparator
from hotglue_smoke_test.compare.test_configurer import TestConfigurer


def _compare_snapshot_trees(expected: Path, actual: Path, label: str) -> None:
    if not expected.is_dir():
        return
    assert actual.is_dir(), f"[{label}] missing actual snapshots at {actual}"

    expected_files = sorted(
        p.relative_to(expected)
        for p in expected.rglob("*")
        if p.is_file() and p.suffix.lower() in {".csv", ".parquet", ".json"}
    )
    for rel in expected_files:
        exp_path = expected / rel
        act_path = actual / rel
        assert act_path.is_file(), f"[{label}] missing snapshot file {act_path}"
        suffix = exp_path.suffix.lower()
        if suffix == ".parquet":
            pd.testing.assert_frame_equal(
                pd.read_parquet(exp_path),
                pd.read_parquet(act_path),
                check_dtype=False,
            )
        elif suffix == ".csv":
            pd.testing.assert_frame_equal(
                pd.read_csv(exp_path),
                pd.read_csv(act_path),
                check_dtype=False,
            )
        else:
            assert exp_path.read_text() == act_path.read_text(), (
                f"[{label}] snapshot json mismatch: {rel}"
            )


class TestEtl(unittest.TestCase):
    def test_etl(self) -> None:
        case_name = os.environ["CASE_NAME"]
        smoke_dir = Path(os.environ["SMOKE_TEST_DIR"])
        case_dir = smoke_dir / case_name
        test_config = TestConfigurer.get_test_config(str(case_dir))

        jobs = list_etl_datetime_dirs(case_dir)
        assert jobs, f"no UTC datetime dirs under {case_dir}"

        for job_dir in jobs:
            stamp = job_dir.name
            expected_etl = job_dir / "expected_output" / "etl-output"
            actual_etl = job_dir / "test_runtime" / "etl-output"
            print(
                f"[ETL COMPARE] datetime={stamp} expected={expected_etl} actual={actual_etl}"
            )

            if (expected_etl / "data.singer").is_file():
                SingerOutputComparator(
                    str(expected_etl), str(actual_etl), test_config
                ).compare()

            _compare_snapshot_trees(
                job_dir / "expected_output" / "snapshots",
                job_dir / "test_runtime" / "snapshots",
                label=f"{case_name}/{stamp}",
            )
            print(f"PASSED!!: {case_name}/{stamp}")
