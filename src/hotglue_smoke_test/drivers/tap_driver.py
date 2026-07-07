import os
import unittest

from hotglue_smoke_test.compare.singer_output_comparator import SingerOutputComparator
from hotglue_smoke_test.compare.test_configurer import TestConfigurer


def _test_root_dir() -> str:
    return os.environ["TAP_TEST_DIR"]


def _case_paths(case_name: str) -> tuple[str, str, str]:
    test_root = _test_root_dir()
    if "/" in case_name:
        case_dir = os.path.join(test_root, case_name.replace("/", os.sep))
    else:
        case_dir = os.path.join(test_root, case_name)
    if not os.path.isdir(case_dir):
        raise FileNotFoundError(f"The test case directory '{case_dir}' does not exist.")
    return case_dir, os.path.join(case_dir, "expected_output"), os.path.join(case_dir, "test_runtime")


def discover_cases(case_name: str) -> list[str]:
    test_root = _test_root_dir()
    if case_name == "*":
        return sorted(
            entry
            for entry in os.listdir(test_root)
            if os.path.isdir(os.path.join(test_root, entry)) and entry.endswith("_test")
        )
    if "/" in case_name:
        _case_paths(case_name)
        return [case_name]
    _case_paths(case_name)
    return [case_name]


class TestTap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case_name = os.environ.get("CASE_NAME")
        if not case_name:
            raise EnvironmentError("CASE_NAME must be set.")
        cls.cases = discover_cases(case_name)

    def test_tap(self):
        for case_name in self.cases:
            case_dir, expected_output_dir, actual_output_dir = _case_paths(case_name)
            print(
                f"Comparing outputs: test: {case_name}, "
                f"expected_output_dir {expected_output_dir}, actual_output_dir {actual_output_dir}"
            )
            test_config = TestConfigurer.get_test_config(case_dir)
            comparator = SingerOutputComparator(expected_output_dir, actual_output_dir, test_config)
            comparator.compare()
            print(f"PASSED!!: test: {case_name}")
