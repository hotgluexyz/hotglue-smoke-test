"""CLI for running colocated hotglue connector smoke tests."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from hotglue_smoke_test.artifacts import (
    validate_generate,
    validate_record,
    validate_run,
    wipe_generate_artifacts,
    wipe_record_artifacts,
)
from hotglue_smoke_test.drivers import etl_driver, tap_driver, target_driver
from hotglue_smoke_test.etl import ops as etl_ops


def _print_section(title: str) -> None:
    print("=============================================")
    print(f"=== {title}")
    print("=============================================")


def _print_status(status: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {status}: {message}")


def _is_etl(tests_dir: Path) -> bool:
    return (tests_dir / "record-etl.py").is_file()


def _is_target_repo(connector_dir: Path) -> bool:
    """Detect target repos from directory name (`target-*`)."""
    return connector_dir.name.startswith("target-")


def _resolve_tests_dir(connector_dir: Path) -> Path:
    tests_dir = connector_dir / "__smoke-tests__"
    record_vcr = tests_dir / "record-vcr.py"
    record_etl = tests_dir / "record-etl.py"
    if not record_vcr.is_file() and not record_etl.is_file():
        print(
            f"Error: colocated tests require {record_vcr} or {record_etl}",
            file=sys.stderr,
        )
        sys.exit(1)
    return tests_dir

def _discover_cases(test_dir: Path, case_name: str) -> list[str]:
    if case_name == "*":
        return sorted(
            p.name
            for p in test_dir.iterdir()
            if p.is_dir() and p.name.endswith("_test")
        )

    if not case_name.endswith("_test") or Path(case_name).name != case_name:
        print(
            "Error: casename must be a single segment ending in '_test', or '*'",
            file=sys.stderr,
        )
        sys.exit(1)
    return [case_name]


def _run_record_vcr(
    connector_dir: Path,
    tests_dir: Path,
    testcase: str,
    mode: str,
    no_scrub: bool = False,
) -> None:
    record_vcr = tests_dir / "record-vcr.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(connector_dir)
    env["SMOKE_TEST_MODE"] = mode
    if no_scrub:
        env["SMOKE_TEST_NO_SCRUB"] = "1"
    else:
        env.pop("SMOKE_TEST_NO_SCRUB", None)
    print(
        f"command [SMOKE_TEST_MODE={mode} PYTHONPATH={env['PYTHONPATH']} "
        f"python {record_vcr} {testcase}]"
    )
    subprocess.run([sys.executable, str(record_vcr), testcase], env=env, check=True)


def _run_comparison(
    smoke_test_dir: Path,
    case_name: str,
    is_target: bool,
    *,
    is_etl: bool = False,
) -> None:
    os.environ["SMOKE_TEST_DIR"] = str(smoke_test_dir)
    os.environ["CASE_NAME"] = case_name

    if is_etl:
        driver = etl_driver
    else:
        driver = target_driver if is_target else tap_driver
    exit_code = pytest.main(["-s", driver.__file__])
    if exit_code != 0:
        raise subprocess.CalledProcessError(exit_code, "pytest")


def _prepare_case(
    mode: str,
    case_dir: Path,
    is_target: bool,
    force: bool,
    *,
    is_etl: bool = False,
) -> None:
    if is_etl:
        # ETL validate/wipe lives in etl.ops (day fixtures, not VCR cassette).
        return
    if mode == "record":
        validate_record(case_dir, force)
        if force:
            wipe_record_artifacts(case_dir)
    elif mode == "generate":
        validate_generate(case_dir, is_target, force)
        if force:
            wipe_generate_artifacts(case_dir)
    elif mode == "run":
        validate_run(case_dir, is_target)


def _execute_etl_case(
    mode: str,
    connector_name: str,
    testcase: str,
    connector_dir: Path,
    smoke_test_dir: Path,
    force: bool,
    no_scrub: bool,
) -> None:
    label = {
        "record": "Recording ETL fixtures",
        "generate": "Generating ETL expected_output",
        "run": "Running ETL comparison",
    }[mode]
    _print_section(f"{label}: {connector_name} / {testcase}")
    _print_status("INFO", f"Starting at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if mode == "record":
        etl_ops.record_case(
            script_root=connector_dir,
            tests_dir=smoke_test_dir,
            case_name=testcase,
            force=force,
            no_scrub=no_scrub,
        )
    elif mode == "generate":
        etl_ops.generate_case(
            script_root=connector_dir,
            tests_dir=smoke_test_dir,
            case_name=testcase,
            force=force,
        )
    elif mode == "run":
        etl_ops.run_case(
            script_root=connector_dir,
            tests_dir=smoke_test_dir,
            case_name=testcase,
        )
        _print_status("INFO", f"Running comparison for case {testcase}")
        _run_comparison(smoke_test_dir, testcase, is_target=False, is_etl=True)


def _execute_case(
    mode: str,
    connector_name: str,
    testcase: str,
    connector_dir: Path,
    smoke_test_dir: Path,
    is_target: bool,
    force: bool,
    no_scrub: bool = False,
) -> None:
    case_dir = smoke_test_dir / testcase
    is_etl = _is_etl(smoke_test_dir)
    _prepare_case(mode, case_dir, is_target, force, is_etl=is_etl)

    if is_etl:
        _execute_etl_case(
            mode,
            connector_name,
            testcase,
            connector_dir,
            smoke_test_dir,
            force,
            no_scrub,
        )
        return

    label = {
        "record": "Recording vcr",
        "generate": "Generating data.singer/state.json",
        "run": "Running comparison",
    }[mode]
    _print_section(f"{label}: {connector_name} / {testcase}")
    _print_status("INFO", f"Starting at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    _run_record_vcr(
        connector_dir,
        smoke_test_dir,
        testcase,
        mode,
        no_scrub=no_scrub,
    )

    if mode == "run":
        _print_status("INFO", f"Running comparison for case {testcase}")
        _run_comparison(smoke_test_dir, testcase, is_target)


def _run_command(args: argparse.Namespace) -> int:
    mode = args.mode
    os.environ.setdefault("TZ", "America/New_York")

    connector_dir = Path(args.connector_directory).resolve()
    smoke_test_dir = _resolve_tests_dir(connector_dir)
    connector_name = connector_dir.name.removeprefix("tap-").removeprefix("target-")
    is_target = _is_target_repo(connector_dir)
    is_etl = _is_etl(smoke_test_dir)

    _print_section("Test Configuration")
    _print_status("INFO", f"Mode: {mode}")
    _print_status("INFO", f"Connector Name: {connector_name}")
    _print_status("INFO", f"Case Name: {args.case_name}")
    if is_etl:
        kind = "etl"
    elif is_target:
        kind = "target"
    else:
        kind = "tap"
    _print_status("INFO", f"Kind: {kind}")
    _print_status("INFO", f"Connector Directory: {connector_dir}")
    _print_status("INFO", f"Test Directory: {smoke_test_dir}")

    cases = _discover_cases(smoke_test_dir, args.case_name)

    _print_section("Starting Execution")
    if args.case_name == "*":
        _print_status("INFO", f"Finding all test cases in {smoke_test_dir} that end in '_test'...")

    passed: list[str] = []
    failed: list[str] = []

    for testcase in cases:
        try:
            _execute_case(
                mode,
                connector_name,
                testcase,
                connector_dir,
                smoke_test_dir,
                is_target,
                args.force,
                no_scrub=args.no_scrub,
            )
            passed.append(testcase)
            _print_status("SUCCESS", f"Completed {mode} successfully: {testcase}")
        except (subprocess.CalledProcessError, OSError, SystemExit) as exc:
            failed.append(testcase)
            _print_status("ERROR", f"Failed {mode} for {testcase}: {exc}")

    _print_section("Summary")
    _print_status("INFO", f"Total: {len(cases)}")
    _print_status("INFO", f"Passed: {len(passed)}")
    _print_status("INFO", f"Failed: {len(failed)}")

    for name in passed:
        _print_status("SUCCESS", f"  ✓ {name}")
    for name in failed:
        _print_status("ERROR", f"  ✗ {name}")

    if failed:
        return 1
    _print_status("SUCCESS", f"All {mode} cases completed successfully.")
    return 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "case_name",
        nargs="?",
        default="*",
        help="Test case name ending in _test, or * for all (default: *)",
    )
    parser.add_argument(
        "--connector-directory",
        default=".",
        help="Path to connector repo root (default: current directory)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hotglue-smoke-test")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser(
        "record",
        help="Record VCR cassette (live API), then scrub secrets/PII",
    )
    _add_common_args(record_parser)
    record_parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe fixtures/, expected_output/, test_runtime/ and re-record",
    )
    record_parser.add_argument(
        "--no-scrub",
        action="store_true",
        help="Skip post-record scrub (debug only; do not commit unsanitized cassettes)",
    )
    record_parser.set_defaults(func=_run_command, mode="record", force=False, no_scrub=False)

    generate_parser = subparsers.add_parser("generate", help="Replay VCR and write expected_output/")
    _add_common_args(generate_parser)
    generate_parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe expected_output/ and test_runtime/ and regenerate data.singer/state.json output",
    )
    generate_parser.set_defaults(func=_run_command, mode="generate", force=False, no_scrub=False)

    run_parser = subparsers.add_parser("run", help="Replay VCR and compare against expected_output/")
    _add_common_args(run_parser)
    run_parser.set_defaults(func=_run_command, mode="run", force=False, no_scrub=False)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
