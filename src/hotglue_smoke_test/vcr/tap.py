import os
from contextlib import redirect_stdout
from unittest.mock import patch

from hotglue_smoke_test.vcr.base import VCRBaseTestRunner


class VCRTapTestRunner(VCRBaseTestRunner):
    def __init__(self, test_case: str, script_dir: str):
        self.required_files = ["config.json", "catalog-selected.json"]
        self.catalog_attr = "catalog"
        super().__init__(test_case, script_dir)

    @property
    def output_basename(self) -> str:
        return "data.singer"

    def run_launch(self):
        with open(self.output_file_path, "w") as output_file:
            with redirect_stdout(output_file):
                if self.is_recording:
                    self.launch()
                else:
                    with patch("time.sleep", return_value=None):
                        self.launch()

    def argv(self):
        args = [
            self.module(),
            "--config",
            os.path.join(self.test_case_path, "config.json"),
            f"--{self.catalog_attr}",
            os.path.join(self.test_case_path, "catalog-selected.json"),
        ]

        state_file = os.path.join(self.test_case_path, "state.json")
        if os.path.exists(state_file):
            args.extend(["--state", state_file])

        filters_file = os.path.join(self.test_case_path, "selected-filters.json")
        if os.path.exists(filters_file):
            args.extend(["--selected-filters", filters_file])

        return args
