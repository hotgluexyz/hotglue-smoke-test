import os
import unittest

from hotglue_smoke_test.compare.state_output_comparator import StateOutputComparator
from hotglue_smoke_test.compare.test_configurer import TestConfigurer
from hotglue_smoke_test.drivers.case_paths import case_paths


class TestTarget(unittest.TestCase):
    def test_target(self):
        case_name = os.environ["CASE_NAME"]
        case_dir, expected_output_dir, actual_output_dir = case_paths(case_name)
        print(
            f"Comparing outputs: test: {case_name}, "
            f"expected_output_dir {expected_output_dir}, actual_output_dir {actual_output_dir}"
        )
        test_config = TestConfigurer.get_test_config(case_dir)
        state_comparator = StateOutputComparator(expected_output_dir, actual_output_dir, test_config)
        state_comparator.compare()
        print(f"PASSED!!: test: {case_name}")
