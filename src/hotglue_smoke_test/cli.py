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
    resolve_case_dir,
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


def _resolve_tap_source_dir(tap_directory: str | None) -> Path:
    if not tap_directory:
        print("Error: --tap-directory is required.", file=sys.stderr)
        sys.exit(1)
    return Path(tap_directory).resolve()


def _resolve_tests_dir(tap_source_dir: Path) -> Path:
    tests_dir = tap_source_dir / "__tests__"
    record_vcr = tests_dir / "record-vcr.py"
    if not record_vcr.is_file():
        print(
            f"Error: colocated tests require {record_vcr}",
            file=sys.stderr,
        )
        sys.exit(1)
    return tests_dir


def _discover_cases(test_dir: Path, case_name: str, test_suite: str | None) -> list[str]:
    if case_name == "*":
        pattern_dir = test_dir / test_suite if test_suite else test_dir
        return sorted(
            p.name
            for p in pattern_dir.iterdir()
            if p.is_dir() and p.name.endswith("_test")
        )

    if not case_name.endswith("_test"):
        print("Error: casename must either end in '_test' or be '*'", file=sys.stderr)
        sys.exit(1)
    return [case_name]


def _case_name_for_driver(testcase: str, test_suite: str | None) -> str:
    if test_suite:
        return f"{test_suite}/{testcase}"
    return testcase


def _python_executable(tap_source_dir: Path) -> str:
    venv_python = tap_source_dir / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _load_ci_env(tap_source_dir: Path) -> None:
    ci_env = tap_source_dir / "ci.env"
    if ci_env.is_file():
        for line in ci_env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key, value)


def _run_record_vcr(
    tap_source_dir: Path,
    tests_dir: Path,
    testcase: str,
    test_suite: str | None,
    python_exe: str,
    mode: str,
    no_scrub: bool = False,
) -> None:
    record_vcr = tests_dir / "record-vcr.py"
    case = f"{test_suite}/{testcase}" if test_suite else testcase
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tap_source_dir)
    env["SMOKE_TEST_MODE"] = mode
    if no_scrub:
        env["SMOKE_TEST_NO_SCRUB"] = "1"
    print(
        f"command [SMOKE_TEST_MODE={mode} PYTHONPATH={env['PYTHONPATH']} "
        f"python {record_vcr} {case}]"
    )
    subprocess.run([python_exe, str(record_vcr), case], env=env, check=True)


def _run_comparison(tap_test_dir: Path, case_name: str, is_target: bool) -> None:
    os.environ["TAP_TEST_DIR"] = str(tap_test_dir)
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
    tap_name: str,
    testcase: str,
    tap_source_dir: Path,
    tap_test_dir: Path,
    is_target: bool,
    test_suite: str | None,
    python_exe: str,
    force: bool,
    no_scrub: bool = False,
) -> None:
    case_dir = resolve_case_dir(tap_test_dir, testcase, test_suite)
    _prepare_case(mode, case_dir, is_target, force)

    label = {
        "record": "Recording",
        "generate": "Generating",
        "run": "Running",
    }[mode]
    _print_section(f"{label}: {tap_name} / {testcase}")
    _print_status("INFO", f"Starting at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    _run_record_vcr(
        tap_source_dir,
        tap_test_dir,
        testcase,
        test_suite,
        python_exe,
        mode,
        no_scrub=no_scrub,
    )

    if mode == "run":
        case_name = _case_name_for_driver(testcase, test_suite)
        _print_status("INFO", f"Running comparison for case {case_name}")
        _run_comparison(tap_test_dir, case_name, is_target)


def _run_command(args: argparse.Namespace, mode: str) -> int:
    os.environ.setdefault("TZ", "America/New_York")

    tap_source_dir = _resolve_tap_source_dir(args.tap_directory)
    tap_test_dir = _resolve_tests_dir(tap_source_dir)
    os.environ["TAP_SOURCE_DIR"] = str(tap_source_dir)
    _load_ci_env(tap_source_dir)

    _print_section("Test Configuration")
    _print_status("INFO", f"Mode: {mode}")
    _print_status("INFO", f"TAP Name: {args.tap_name}")
    _print_status("INFO", f"Case Name: {args.case_name}")
    _print_status("INFO", f"Target Mode: {args.target}")
    _print_status("INFO", f"Tap Source Directory: {tap_source_dir}")
    _print_status("INFO", f"Test Directory: {tap_test_dir}")

    python_exe = _python_executable(tap_source_dir)
    test_suite = os.environ.get("TEST_SUITE")
    cases = _discover_cases(tap_test_dir, args.case_name, test_suite)
    force = getattr(args, "force", False)
    no_scrub = getattr(args, "no_scrub", False)

    _print_section("Starting Execution")
    if args.case_name == "*":
        _print_status("INFO", f"Finding all test cases in {tap_test_dir} that end in '_test'...")

    passed: list[str] = []
    failed: list[str] = []

    for testcase in cases:
        try:
            _execute_case(
                mode,
                args.tap_name,
                testcase,
                tap_source_dir,
                tap_test_dir,
                args.target,
                test_suite,
                python_exe,
                force,
                no_scrub=no_scrub,
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


def cmd_record(args: argparse.Namespace) -> int:
    return _run_command(args, "record")


def cmd_generate(args: argparse.Namespace) -> int:
    return _run_command(args, "generate")


def cmd_run(args: argparse.Namespace) -> int:
    return _run_command(args, "run")


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("tap_name", help="Connector name without tap-/target- prefix")
    parser.add_argument("case_name", help="Test case name ending in _test, or * for all")
    parser.add_argument("--tap-directory", required=True, help="Path to connector repo root")
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
    record_parser.set_defaults(func=cmd_record, target=False, force=False, no_scrub=False)

    generate_parser = subparsers.add_parser("generate", help="Replay VCR and write expected_output/")
    _add_common_args(generate_parser)
    generate_parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe expected_output/ and test_runtime/ and regenerate data.singer/state.json output",
    )
    generate_parser.set_defaults(func=cmd_generate, target=False, force=False)

    run_parser = subparsers.add_parser("run", help="Replay VCR and compare against expected_output/")
    _add_common_args(run_parser)
    run_parser.set_defaults(func=cmd_run, target=False)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
