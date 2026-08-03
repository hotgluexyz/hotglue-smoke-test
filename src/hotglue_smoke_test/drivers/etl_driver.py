"""Compare ETL smoke expected_output/ vs test_runtime/ (per UTC datetime folder)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from hotglue_smoke_test.artifacts import list_etl_datetime_dirs
from hotglue_smoke_test.compare.csv_output_comparator import compare_csv_folder
from hotglue_smoke_test.compare.json_output_comparator import JsonOutputComparator
from hotglue_smoke_test.compare.singer_output_comparator import SingerOutputComparator
from hotglue_smoke_test.compare.snapshot_output_comparator import compare_snapshots
from hotglue_smoke_test.compare.test_configurer import TestConfigurer


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
            label = f"{case_name}/{stamp}"
            expected_etl = job_dir / "expected_output" / "etl-output"
            actual_etl = job_dir / "test_runtime" / "etl-output"
            print(
                f"[ETL COMPARE] datetime={stamp} expected={expected_etl} actual={actual_etl}"
            )

            if expected_etl.is_dir():
                if (expected_etl / "data.singer").is_file():
                    print(f"[SINGER COMPARE] {label}")
                    SingerOutputComparator(
                        str(expected_etl), str(actual_etl), test_config
                    ).compare()

                print(f"[JSON COMPARE] {label}")
                JsonOutputComparator(
                    label, str(expected_etl), str(actual_etl), test_config
                ).compare()

                print(f"[CSV COMPARE] etl-output {label}")
                compare_csv_folder(
                    label,
                    str(actual_etl),
                    str(expected_etl),
                    {"test_config": test_config},
                )

            print(f"[SNAPSHOT COMPARE] {label}")
            compare_snapshots(
                job_dir / "expected_output" / "snapshots",
                job_dir / "test_runtime" / "snapshots",
                label=label,
                test_config=test_config,
            )
            print(f"PASSED!!: {label}")
