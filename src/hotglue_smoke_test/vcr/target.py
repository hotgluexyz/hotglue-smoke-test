import os
import sys
from typing import Any

from hotglue_smoke_test.vcr.base import VCRBaseTestRunner
from hotglue_smoke_test.vcr.target_scrub import scrub_target_case


class VCRTargetTestRunner(VCRBaseTestRunner):
    # Literal values the target branches on (enums, currencies); kept real so
    # replay takes the same code path it took while recording.
    PRESERVE_VALUES: set[Any] = set()

    def __init__(self, test_case: str, script_dir: str):
        self.required_files = ["config.json", "data.singer"]
        super().__init__(test_case, script_dir)

    def sanitize_cassette(self):
        """Scrub data.singer and the cassette together with one shared value map."""
        scrubber = scrub_target_case(
            self.test_case_path,
            self.vcr_cassette_path,
            preserve_keys=set(self.PRESERVE_KEYS),
            token_keys=set(self.TOKEN_KEYS),
            preserve_values=set(self.PRESERVE_VALUES),
            scrub_uri=self.scrub_uri,
        )
        print(
            f"Scrubbed {scrubber.scrubbed_values} values "
            f"({len(scrubber.references)} reused as references)"
        )

    @property
    def output_basename(self) -> str:
        return "state.json"

    def run_launch(self):
        if self.mode == "record":
            self._require_live_singer()
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

    def _require_live_singer(self):
        """Record must see real Singer values. Scrub rewrites data.singer in place."""
        path = os.path.join(self.test_case_path, "data.singer")
        text = open(path).read()
        if "-Fallback-scrubbed-" in text or "Fake-" in text:
            raise SystemExit(
                "data.singer is already scrubbed "
                f"({path}). Restore the original live payload before record; "
                "recording fakes makes QuickBooks lookups miss and generate replay that miss."
            )

    def argv(self):
        return [
            self.module(),
            "--config",
            os.path.join(self.test_case_path, "config.json"),
        ]
