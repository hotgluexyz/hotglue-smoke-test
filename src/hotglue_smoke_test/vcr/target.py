import os
import sys

import vcr

from hotglue_smoke_test.vcr.base import VCRBaseTestRunner


class VCRTargetTestRunner(VCRBaseTestRunner):
    def __init__(self, test_case: str, script_dir: str):
        super().__init__(test_case, script_dir)
        self.output_file_path = os.path.join(self.test_case_path, "test_runtime", "state.json")
        self.required_files = ["config.json", "data.singer"]

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

    def vcr_use_cassette(self, filter_query_parameters, test_config=None):
        return vcr.use_cassette(
            self.vcr_cassette_path,
            decode_compressed_response=True,
            filter_headers=["authorization"],
            filter_post_data_parameters=[
                "client_id",
                "client_secret",
                "refresh_token",
                "access_token",
            ],
            filter_query_parameters=filter_query_parameters,
            before_record_response=self.scrub_token_from_response,
        )

    def argv(self):
        return [
            "target.py",
            "--config",
            os.path.join(self.test_case_path, "config.json"),
        ]
