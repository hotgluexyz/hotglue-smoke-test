import json
import os
import sys
from abc import ABC, abstractmethod
from typing import List

import debugpy
import vcr
from freezegun import freeze_time


class VCRBaseTestRunner(ABC):
    def __init__(self, test_case: str, script_dir: str):
        self.test_case = test_case
        self.script_dir = script_dir
        self.test_case_path = os.path.join(self.script_dir, test_case)
        self.test_config_path = os.path.join(self.test_case_path, "test-config.json")
        self.vcr_cassette_path = os.path.join(self.test_case_path, "fixtures", "vcr.yaml")
        self.is_recording = not os.path.exists(self.vcr_cassette_path)

        self.output_file_path = os.path.join(self.test_case_path, "test_runtime", "data.singer")
        self.required_files = ["config.json", "catalog-selected.json"]
        self.catalog_attr = "catalog"

    def run_test(self):
        if os.getenv("DEBUG", "false").lower() == "true":
            debugpy.listen(("localhost", 5678))
            print("Waiting for debugger attach...")
            debugpy.wait_for_client()
            print("Debugger is attached, continuing...")

        for filename in self.required_files:
            file_path = os.path.join(self.test_case_path, filename)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Missing required file: {file_path}")

        test_config = {}
        if os.path.exists(self.test_config_path):
            with open(self.test_config_path) as config_file:
                test_config = json.load(config_file)

        os.environ["IS_TEST"] = "true"
        if "ignore_streams" in test_config:
            os.environ["IGNORE_STREAMS"] = ",".join(test_config["ignore_streams"])
            print(f"IGNORE_STREAMS set to: {os.environ['IGNORE_STREAMS']}")

        if "include_streams" in test_config:
            os.environ["INCLUDE_STREAMS"] = ",".join(test_config["include_streams"])
            print(f"INCLUDE_STREAMS set to: {os.environ['INCLUDE_STREAMS']}")

        if "env" in test_config:
            for key, value in test_config["env"].items():
                os.environ[key] = str(value)
                print(f"{key} set to: {os.environ[key]}")

        sys.argv = self.argv()

        os.makedirs(os.path.dirname(self.output_file_path), exist_ok=True)

        filter_query_parameters = test_config.get("filter_query_parameters", [])
        print(f"Filtering query parameters: {filter_query_parameters}")

        with self.vcr_use_cassette(filter_query_parameters, test_config):
            freeze_datetime = test_config.get("freeze_time")
            if freeze_datetime:
                with freeze_time(freeze_datetime):
                    self.run_launch()
            else:
                self.run_launch()
        print(f"Captured output written to: {self.output_file_path}")

    @classmethod
    def main(cls):
        if len(sys.argv) != 2:
            print("Usage: record-vcr.py <testcase>")
            sys.exit(1)
        test_case = sys.argv[1]
        runner = cls(test_case)
        runner.run_test()

    def scrub_token_from_response(self, response):
        try:
            token_keys = ["access_token", "refresh_token"]
            body = response["body"]["string"].decode("utf-8")
            data = json.loads(body)

            for key in token_keys:
                if key in data:
                    data[key] = key
                    response["body"]["string"] = json.dumps(data).encode("utf-8")
            response["headers"]["Content-Length"] = [str(len(response["body"]["string"]))]
        except json.JSONDecodeError:
            pass
        return response

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
        )

    @abstractmethod
    def module(self) -> str:
        pass

    @abstractmethod
    def run_launch(self):
        pass

    @abstractmethod
    def launch(self):
        pass

    @abstractmethod
    def argv(self) -> List[str]:
        pass
