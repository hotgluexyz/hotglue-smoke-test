"""CSV folder compare (ported from test-framework OutputComparator.compare_csv_folder)."""

from __future__ import annotations

import glob
import os

import pandas as pd
from pandas.testing import assert_frame_equal

pd.set_option("expand_frame_repr", False)
pd.set_option("display.max_columns", 999)


def compare_csv_folder(label, actual_output_path, expected_output_path, options=None):
    options = options or {}
    test_config = options.get("test_config") or {}
    ignore_columns = test_config.get("ignore_columns") or {}
    ignore_files = test_config.get("ignore_files") or []
    sort_config = test_config.get("sort_config") or {}
    rename_config = test_config.get("rename_config") or {}
    dtypes_config = test_config.get("dtypes_config") or {}
    filter_config = test_config.get("filter_config") or {}
    date_conversion_config = test_config.get("date_conversion_config") or {}

    if not os.path.exists(expected_output_path):
        return

    actual = {}
    read_csv_files_from_folder(actual_output_path, actual, ignore_files)

    expected = {}
    read_csv_files_from_folder(expected_output_path, expected, ignore_files)

    print(f"actual = {actual.keys()} {len(actual)}")
    print(f"expected = {expected.keys()} {len(expected)}")
    assert len(actual) == len(expected), (
        f"File count missmatch: actual has {len(actual)} files; expected has {len(expected)} files"
    )

    if len(actual) == 0:
        print(
            f"No files to compare [{label}]!! Folder {expected_output_path} and "
            f"{actual_output_path} are both empty."
        )
        return

    for file in actual:
        if file in ignore_files:
            continue

        try:
            if file in rename_config:
                actual[file] = actual[file].rename(columns=rename_config[file])

            if file in dtypes_config:
                actual[file] = actual[file].astype(dtypes_config[file])
                expected[file] = expected[file].astype(dtypes_config[file])

            if file in date_conversion_config:
                for column_name, format in date_conversion_config[file].items():
                    actual_dt_column = pd.to_datetime(actual[file][column_name])
                    if not actual_dt_column.dt.tz:
                        actual_dt_column = actual_dt_column.dt.tz_localize("Etc/UTC")
                    actual[file][column_name] = (
                        actual_dt_column.dt.tz_convert("Etc/UTC").dt.strftime(format)
                    )

                    # ponytail: same as test-framework (uses actual column for expected)
                    expected_dt_column = pd.to_datetime(actual[file][column_name])
                    if not expected_dt_column.dt.tz:
                        expected_dt_column = expected_dt_column.dt.tz_localize("Etc/UTC")
                    expected[file][column_name] = (
                        expected_dt_column.dt.tz_convert("Etc/UTC").dt.strftime(format)
                    )

            if file in filter_config:
                actual[file] = filter_config[file](actual[file])
                expected[file] = filter_config[file](expected[file])

            if file in ignore_columns or "*" in ignore_columns:
                for column_name in ignore_columns.get(file, []) + ignore_columns.get(
                    "*", []
                ):
                    if column_name in actual[file]:
                        actual[file] = actual[file].drop([column_name], axis=1)
                    if column_name in expected[file]:
                        expected[file] = expected[file].drop([column_name], axis=1)

            if actual[file].empty and expected[file].empty:
                print(f"Success! [{file}] is empty in expected and actual.")
                continue

            if file in sort_config:
                expected[file] = (
                    expected[file].sort_values(by=sort_config[file]).reset_index(drop=True)
                )
                actual[file] = (
                    actual[file].sort_values(by=sort_config[file]).reset_index(drop=True)
                )
            else:
                expected[file] = expected[file].sort_index(axis=1).reset_index(drop=True)
                actual[file] = actual[file].sort_index(axis=1).reset_index(drop=True)

            actual_count = len(actual[file])
            expected_count = len(expected[file])

            assert actual_count == expected_count, (
                f"Test [{label}] FAILED!! Stream '{file}' record count mismatch\n"
                f"Expected: {actual_count}, Actual: {expected_count}"
            )

            assert_frame_equal(actual[file], expected[file], check_names=True)
            print(
                f"Success! Comparison of file [{file}] in ETL output. "
                f"Record cound = {actual_count}"
            )
        except AssertionError as e:
            print(actual[file])
            print(expected[file])
            print(actual[file].dtypes)
            print(expected[file].dtypes)
            print(f"Failed case {label}! Comparison of file [{file}] in ETL output.")
            raise e
    print(f"Comparison successful [{label}].")


def read_csv_files_from_folder(abs_path, csv_dict, ignore_files):
    ignore_files = ignore_files or []
    if not os.path.isdir(abs_path):
        return
    csv_files = glob.glob(abs_path + "/*.csv")
    for file in csv_files:
        file_name = os.path.basename(file).split(".")[0]

        if file_name in ignore_files:
            print(f"File {file_name} is ignored in test_config.json.")
            continue

        prior_data = csv_dict.get(file_name)
        new_data = pd.read_csv(file)
        csv_dict[file_name] = pd.concat([prior_data, new_data])
