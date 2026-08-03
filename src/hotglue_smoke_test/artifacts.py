"""Smoke test case artifact paths, validation, and wipe helpers."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from hotglue_smoke_test.vcr.base import VCRBaseTestRunner

# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------


def _rmtree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def _die(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Connector (tap / target)
# ---------------------------------------------------------------------------


def cassette_path(case_dir: Path) -> Path:
    return case_dir / "fixtures" / "vcr.yaml"


def output_path(case_dir: Path, mode: str, is_target: bool) -> Path:
    filename = "state.json" if is_target else "data.singer"
    if mode == "generate":
        return case_dir / "expected_output" / filename
    if mode == "run":
        return case_dir / "test_runtime" / filename
    raise ValueError(f"record mode has no output file (mode={mode!r})")


def wipe_record_artifacts(case_dir: Path) -> None:
    for name in ("fixtures", "expected_output", "test_runtime"):
        _rmtree(case_dir / name)


def wipe_generate_artifacts(case_dir: Path) -> None:
    for name in ("expected_output", "test_runtime"):
        _rmtree(case_dir / name)


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
    for key in VCRBaseTestRunner.TOKEN_KEYS:
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


# ---------------------------------------------------------------------------
# ETL
# ---------------------------------------------------------------------------

# UTC job folders: ``YYYYMMDDTHHMMSS`` (not calendar day-only).
_ETL_DATETIME_DIR_RE = re.compile(r"^\d{8}T\d{6}$")


def list_etl_datetime_dirs(case_dir: Path) -> list[Path]:
    """UTC datetime job folders under an ETL case (``YYYYMMDDTHHMMSS``)."""
    if not case_dir.is_dir():
        return []
    dirs = [
        p for p in case_dir.iterdir() if p.is_dir() and _ETL_DATETIME_DIR_RE.match(p.name)
    ]
    return sorted(dirs, key=lambda p: p.name)


def etl_datetime_has_expected(job_dir: Path) -> bool:
    """True when a UTC job dir already has generate output (``expected_output/etl-output``)."""
    return (job_dir / "expected_output" / "etl-output").is_dir()


def wipe_etl_record_artifacts(case_dir: Path) -> None:
    """Wipe UTC datetime job dirs; keep test-config.json (record is otherwise append-only)."""
    for job_dir in list_etl_datetime_dirs(case_dir):
        _rmtree(job_dir)


def wipe_etl_generate_artifacts(case_dir: Path) -> None:
    for job_dir in list_etl_datetime_dirs(case_dir):
        _rmtree(job_dir / "expected_output")
        _rmtree(job_dir / "test_runtime")


def validate_etl_generate(case_dir: Path, force: bool) -> None:
    jobs = list_etl_datetime_dirs(case_dir)
    if not jobs:
        _die(f"no UTC datetime dirs under {case_dir}; run record first")
    if not force and all(etl_datetime_has_expected(d) for d in jobs):
        _die(
            f"all datetime dirs under {case_dir} already have expected_output/; "
            "pass --force to wipe expected_output/ and test_runtime/ per datetime and regenerate"
        )


def validate_etl_run(case_dir: Path) -> None:
    jobs = list_etl_datetime_dirs(case_dir)
    if not jobs:
        _die(f"no UTC datetime dirs under {case_dir}; run record first")
    missing = [d.name for d in jobs if not etl_datetime_has_expected(d)]
    if missing:
        _die(
            f"missing expected_output for datetime dirs {missing}; run generate first"
        )
