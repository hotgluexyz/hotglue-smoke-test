import inspect
import json
import os
import sys
from abc import ABC, abstractmethod
from contextlib import nullcontext

import vcr
from faker import Faker
from freezegun import freeze_time

from hotglue_smoke_test.vcr.sanitize import (
    sanitize_cassette_file,
    sanitize_config_credentials,
    scrub_response_json,
)


class VCRBaseTestRunner(ABC):
    FILTER_HEADERS = ["authorization"]
    PRESERVE_KEYS: set[str] = set()
    TOKEN_KEYS = [
        "access_token",
        "refresh_token",
        "api_key",
        "api_secret",
        "auth_token",
        "client_id",
        "client_secret",
        "password",
        "token",
        "sender_password",
        "user_password",
        "ns_consumer_key",
        "ns_consumer_secret",
        "ns_token_key",
        "ns_token_secret",
    ]

    def __init__(self, test_case: str, script_dir: str):
        self.test_case = test_case
        self.script_dir = script_dir
        self.test_case_path = os.path.join(self.script_dir, test_case)
        self.test_config_path = os.path.join(self.test_case_path, "test-config.json")
        self.vcr_cassette_path = os.path.join(self.test_case_path, "fixtures", "vcr.yaml")

        self.mode = os.environ.get("SMOKE_TEST_MODE", "run")
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
        for filename in self.required_files:
            file_path = os.path.join(self.test_case_path, filename)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Missing required file: {file_path}")

        test_config = {}
        if os.path.exists(self.test_config_path):
            with open(self.test_config_path) as config_file:
                test_config = json.load(config_file)

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
                # Click CLIs raise SystemExit(0) on success; swallow so post-record scrub runs.
                try:
                    self.run_launch()
                except SystemExit as exc:
                    if exc.code not in (0, None):
                        raise

        if self.mode == "record":
            print(f"VCR cassette recorded: {self.vcr_cassette_path}")
            if os.environ.get("SMOKE_TEST_NO_SCRUB") == "1":
                print("Skipping cassette scrub (--no-scrub)")
            else:
                self.sanitize_cassette()
                sanitize_config_credentials(self.test_case_path, self.TOKEN_KEYS)
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

    def sanitize_cassette(self):
        """Default-scrub response JSON leaves; PRESERVE_KEYS stay real. Tokens scrubbed after."""
        faker = Faker()
        Faker.seed(hash(self.test_case) & 0xFFFFFFFF)
        cache = {}
        sanitize_cassette_file(
            self.vcr_cassette_path,
            scrub_response=lambda body: scrub_response_json(
                body, set(self.PRESERVE_KEYS), faker, cache, set(self.TOKEN_KEYS)
            ),
        )

    def vcr_use_cassette(self, filter_query_parameters):
        return vcr.use_cassette(
            self.vcr_cassette_path,
            decode_compressed_response=True,
            filter_headers=list(self.FILTER_HEADERS),
            filter_post_data_parameters=list(self.TOKEN_KEYS),
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
    def argv(self) -> list[str]:
        pass
