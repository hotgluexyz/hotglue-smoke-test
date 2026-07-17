"""Smoke test case artifact paths, validation, and wipe helpers."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from hotglue_smoke_test.vcr.sanitize import TOKEN_KEYS


def cassette_path(case_dir: Path) -> Path:
    return case_dir / "fixtures" / "vcr.yaml"


def output_path(case_dir: Path, mode: str, is_target: bool) -> Path:
    filename = "state.json" if is_target else "data.singer"
    if mode == "generate":
        return case_dir / "expected_output" / filename
    if mode == "run":
        return case_dir / "test_runtime" / filename
    raise ValueError(f"record mode has no output file (mode={mode!r})")


def _rmtree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def wipe_record_artifacts(case_dir: Path) -> None:
    for name in ("fixtures", "expected_output", "test_runtime"):
        _rmtree(case_dir / name)


def wipe_generate_artifacts(case_dir: Path) -> None:
    for name in ("expected_output", "test_runtime"):
        _rmtree(case_dir / name)


def _die(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_record(case_dir: Path, force: bool) -> None:
    if cassette_path(case_dir).is_file() and not force:
        _die(
            f"cassette already exists at {cassette_path(case_dir)}; "
            "pass --force to wipe fixtures/, expected_output/, and test_runtime/ and re-record"
        )
    _validate_live_credentials(case_dir)


def _validate_live_credentials(case_dir: Path) -> None:
    config_path = case_dir / "config.json"
    if not config_path.is_file():
        return
    config = json.loads(config_path.read_text())
    for key in TOKEN_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or not value:
            #int were converted to strings on the sanitize_config_credentials
            continue
        if "***" in value:
            _die(
                f"config.json contains placeholder {key}={value!r}; "
                "copy live credentials from .secrets/config.json into the case config before recording"
            )


def validate_generate(case_dir: Path, is_target: bool, force: bool) -> None:
    cassette = cassette_path(case_dir)
    if not cassette.is_file():
        _die(f"missing cassette {cassette}; run record first")
    expected_output = output_path(case_dir, "generate", is_target)
    if expected_output.is_file() and not force:
        _die(
            f"expected output file already exists at {expected_output}; "
            "pass --force to wipe expected_output/ and test_runtime/ and regenerate"
        )


def validate_run(case_dir: Path, is_target: bool) -> None:
    cassette = cassette_path(case_dir)
    if not cassette.is_file():
        _die(f"missing cassette {cassette}; run record first")
    expected_output = output_path(case_dir, "generate", is_target)
    if not expected_output.is_file():
        _die(f"missing expected output file {expected_output}; run generate first")
