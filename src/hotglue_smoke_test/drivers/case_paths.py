"""Resolve a single smoke-test case directory from CASE_NAME / TAP_TEST_DIR."""

from __future__ import annotations

import os


def case_paths(case_name: str) -> tuple[str, str, str]:
    test_root = os.environ["TAP_TEST_DIR"]
    case_dir = os.path.join(test_root, case_name.replace("/", os.sep))
    if not os.path.isdir(case_dir):
        raise FileNotFoundError(f"The test case directory '{case_dir}' does not exist.")
    return case_dir, os.path.join(case_dir, "expected_output"), os.path.join(case_dir, "test_runtime")
