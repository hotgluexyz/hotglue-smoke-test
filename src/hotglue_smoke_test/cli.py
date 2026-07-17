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
from hotglue_smoke_test.drivers import tap_driver, target_driver


def _print_section(title: str) -> None:
    print("=============================================")
    print(f"=== {title}")
    print("=============================================")


def _print_status(status: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {status}: {message}")


def _resolve_tests_dir(connector_dir: Path) -> Path:
    tests_dir = connector_dir / "__tests__"
    record_vcr = tests_dir / "record-vcr.py"
    if not record_vcr.is_file():
        print(
            f"Error: colocated tests require {record_vcr}",
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

    if not case_name.endswith("_test"):
        print("Error: casename must either end in '_test' or be '*'", file=sys.stderr)
        sys.exit(1)
    return [case_name]


def _python_executable(connector_dir: Path) -> str:
    venv_python = connector_dir / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _run_record_vcr(
    connector_dir: Path,
    tests_dir: Path,
    testcase: str,
    python_exe: str,
    mode: str,
    no_scrub: bool = False,
) -> None:
    record_vcr = tests_dir / "record-vcr.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(connector_dir)
    env["SMOKE_TEST_MODE"] = mode
    if no_scrub:
        env["SMOKE_TEST_NO_SCRUB"] = "1"
    print(
        f"command [SMOKE_TEST_MODE={mode} PYTHONPATH={env['PYTHONPATH']} "
        f"python {record_vcr} {testcase}]"
    )
    subprocess.run([python_exe, str(record_vcr), testcase], env=env, check=True)


def _run_comparison(smoke_test_dir: Path, case_name: str, is_target: bool) -> None:
    os.environ["SMOKE_TEST_DIR"] = str(smoke_test_dir)
    os.environ["CASE_NAME"] = case_name

    driver = target_driver if is_target else tap_driver
    exit_code = pytest.main(["-s", driver.__file__])
    if exit_code != 0:
        raise subprocess.CalledProcessError(exit_code, "pytest")


def _prepare_case(
    mode: str,
    case_dir: Path,
    is_target: bool,
    force: bool,
) -> None:
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


def _execute_case(
    mode: str,
    connector_name: str,
    testcase: str,
    connector_dir: Path,
    smoke_test_dir: Path,
    is_target: bool,
    python_exe: str,
    force: bool,
    no_scrub: bool = False,
) -> None:
    case_dir = smoke_test_dir / testcase
    _prepare_case(mode, case_dir, is_target, force)

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
        python_exe,
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

    _print_section("Test Configuration")
    _print_status("INFO", f"Mode: {mode}")
    _print_status("INFO", f"Connector Name: {args.connector_name}")
    _print_status("INFO", f"Case Name: {args.case_name}")
    _print_status("INFO", f"Target Mode: {args.target}")
    _print_status("INFO", f"Connector Directory: {connector_dir}")
    _print_status("INFO", f"Test Directory: {smoke_test_dir}")

    python_exe = _python_executable(connector_dir)
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
                args.connector_name,
                testcase,
                connector_dir,
                smoke_test_dir,
                args.target,
                python_exe,
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
    parser.add_argument("connector_name", help="Connector name without tap-/target- prefix")
    parser.add_argument("case_name", help="Test case name ending in _test, or * for all")
    parser.add_argument("--connector-directory", required=True, help="Path to connector repo root")
    parser.add_argument("--target", action="store_true", help="Run as target")


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
