import os
import unittest

from hotglue_smoke_test.compare.singer_output_comparator import SingerOutputComparator
from hotglue_smoke_test.compare.test_configurer import TestConfigurer
from hotglue_smoke_test.drivers.case_paths import case_paths


class TestTap(unittest.TestCase):
    def test_tap(self):
        case_name = os.environ["CASE_NAME"]
        case_dir, expected_output_dir, actual_output_dir = case_paths(case_name)
        print(
            f"Comparing outputs: test: {case_name}, "
            f"expected_output_dir {expected_output_dir}, actual_output_dir {actual_output_dir}"
        )
        test_config = TestConfigurer.get_test_config(case_dir)
        comparator = SingerOutputComparator(expected_output_dir, actual_output_dir, test_config)
        comparator.compare()
        print(f"PASSED!!: test: {case_name}")
