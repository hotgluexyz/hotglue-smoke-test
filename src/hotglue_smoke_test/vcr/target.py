import os
import sys

from hotglue_smoke_test.vcr.base import VCRBaseTestRunner


class VCRTargetTestRunner(VCRBaseTestRunner):
    def __init__(self, test_case: str, script_dir: str):
        self.required_files = ["config.json", "data.singer"]
        super().__init__(test_case, script_dir)

    @property
    def output_basename(self) -> str:
        return "state.json"

    def run_launch(self):
        with (
            open(os.path.join(self.test_case_path, "data.singer"), "r") as input_file,
            open(self.output_file_path, "w") as output_file,
        ):
            sys.stdin = input_file
            sys.stdout = output_file
            try:
                self.launch()
            finally:
                sys.stdin = sys.__stdin__
                sys.stdout = sys.__stdout__

    def vcr_use_cassette(self, filter_query_parameters):
        return super().vcr_use_cassette(
            filter_query_parameters,
            before_record_response=self.scrub_token_from_response,
        )

    def argv(self):
        return [
            self.module(),
            "--config",
            os.path.join(self.test_case_path, "config.json"),
        ]
