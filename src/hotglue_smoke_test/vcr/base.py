import inspect
import json
import os
import sys
from abc import ABC, abstractmethod
from contextlib import nullcontext

import debugpy
import vcr
from freezegun import freeze_time

from hotglue_smoke_test.vcr.sanitize import (
    sanitize_cassette_file,
    scrub_tokens_in_json,
)

SMOKE_TEST_MODES = {"record", "generate", "run"}


class VCRBaseTestRunner(ABC):
    def __init__(self, test_case: str, script_dir: str):
        self.test_case = test_case
        self.script_dir = script_dir
        self.test_case_path = os.path.join(self.script_dir, test_case)
        self.test_config_path = os.path.join(self.test_case_path, "test-config.json")
        self.vcr_cassette_path = os.path.join(self.test_case_path, "fixtures", "vcr.yaml")

        self.mode = os.environ.get("SMOKE_TEST_MODE", "run")
        if self.mode not in SMOKE_TEST_MODES:
            raise ValueError(
                f"SMOKE_TEST_MODE must be one of {sorted(SMOKE_TEST_MODES)}, got {self.mode!r}"
            )
        self.output_file_path = self._resolve_output_path()

    @property
    @abstractmethod
    def output_basename(self) -> str:
        pass

    def _resolve_output_path(self) -> str:
        if self.mode == "record":
            return os.devnull
        subdir = "expected_output" if self.mode == "generate" else "test_runtime"
        return os.path.join(self.test_case_path, subdir, self.output_basename)

    def run_test(self):
        if os.environ.get("DEBUG", "").lower() == "true":
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

        if self.mode == "record":
            os.makedirs(os.path.dirname(self.vcr_cassette_path), exist_ok=True)
        else:
            os.makedirs(os.path.dirname(self.output_file_path), exist_ok=True)

        filter_query_parameters = test_config.get("filter_query_parameters", [])
        print(f"Filtering query parameters: {filter_query_parameters}")

        with self.vcr_use_cassette(filter_query_parameters):
            freeze_datetime = test_config.get("freeze_time")
            ctx = freeze_time(freeze_datetime) if freeze_datetime else nullcontext()
            with ctx:
                self.run_launch()

        if self.mode == "record":
            print(f"VCR cassette recorded: {self.vcr_cassette_path}")
            if os.environ.get("SMOKE_TEST_NO_SCRUB") == "1":
                print("Skipping cassette scrub (--no-scrub)")
            else:
                self.sanitize_cassette()
                print(f"VCR cassette sanitized: {self.vcr_cassette_path}")
        else:
            print(f"Captured output written to: {self.output_file_path}")

    @classmethod
    def main(cls):
        if len(sys.argv) != 2:
            print("Usage: record-vcr.py <testcase>")
            sys.exit(1)
        test_case = sys.argv[1]
        script_dir = os.path.dirname(os.path.abspath(inspect.getfile(cls)))
        runner = cls(test_case, script_dir)
        runner.run_test()

    def scrub_token_from_response(self, response):
        # Token scrub during record; full sanitize_cassette runs after record completes.
        try:
            body = response["body"]["string"].decode("utf-8")
            data = json.loads(body)
            if isinstance(data, dict):
                data = scrub_tokens_in_json(data)
                response["body"]["string"] = json.dumps(data).encode("utf-8")
                response["headers"]["Content-Length"] = [str(len(response["body"]["string"]))]
        except json.JSONDecodeError:
            pass
        return response

    def sanitize_cassette(self):
        """Default: scrub OAuth tokens from cassette response JSON bodies."""
        def scrub_tokens_only(body: str) -> str:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return body
            if isinstance(data, dict):
                data = scrub_tokens_in_json(data)
            return json.dumps(data)

        sanitize_cassette_file(self.vcr_cassette_path, scrub_response=scrub_tokens_only)

    def vcr_use_cassette(self, filter_query_parameters, before_record_response=None):
        kwargs = {
            "decode_compressed_response": True,
            "filter_headers": ["authorization"],
            "filter_post_data_parameters": [
                "client_id",
                "client_secret",
                "refresh_token",
                "access_token",
            ],
            "filter_query_parameters": filter_query_parameters,
        }
        if before_record_response is not None:
            kwargs["before_record_response"] = before_record_response
        return vcr.use_cassette(self.vcr_cassette_path, **kwargs)

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
    def argv(self) -> list[str]:
        pass
