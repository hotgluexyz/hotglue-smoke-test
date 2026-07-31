"""Record / generate / run ETL smoke cases (offline transform, no VCR)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from hotglue_smoke_test.etl.base import ETLSmokeRunner
from hotglue_smoke_test.etl.scrub import list_day_dirs, scrub_tree


def load_record_etl(tests_dir: Path) -> ModuleType:
    path = tests_dir / "record-etl.py"
    spec = importlib.util.spec_from_file_location("record_etl", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_runner(tests_dir: Path) -> ETLSmokeRunner:
    """Load ``Runner`` from record-etl.py (class or instance)."""
    module = load_record_etl(tests_dir)
    runner = getattr(module, "Runner", None)
    if runner is None:
        raise SystemExit(
            f"{tests_dir / 'record-etl.py'} must export Runner = YourETLSmokeRunner "
            f"(subclass of hotglue_smoke_test.etl.base.ETLSmokeRunner)"
        )
    if isinstance(runner, ETLSmokeRunner):
        return runner
    if isinstance(runner, type) and issubclass(runner, ETLSmokeRunner):
        return runner()
    raise SystemExit(
        f"Runner must be an ETLSmokeRunner subclass or instance, got {type(runner)}"
    )


def _as_set(value: Any) -> set:
    if value is None:
        return set()
    if isinstance(value, set):
        return value
    return set(value)


def _load_test_config(case_dir: Path) -> dict[str, Any]:
    path = case_dir / "test-config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _case_env(
    runner: ETLSmokeRunner, case_dir: Path, case_name: str
) -> dict[str, str]:
    """Resolve FLOW / JOB_TYPE / TENANT: test-config.json overrides Runner defaults.

    TENANT defaults to the case folder name when unset (one Runner, many cases).
    """
    cfg = _load_test_config(case_dir)
    flow = cfg.get("flow") or cfg.get("FLOW") or runner.FLOW
    job_type = cfg.get("job_type") or cfg.get("JOB_TYPE") or runner.JOB_TYPE
    tenant = (
        cfg.get("tenant")
        or cfg.get("TENANT")
        or runner.TENANT
        or case_name
    )
    if not flow:
        raise SystemExit(
            f"{type(runner).__name__}.FLOW unset and no flow in {case_dir / 'test-config.json'}"
        )
    return {"flow": str(flow), "job_type": str(job_type), "tenant": str(tenant)}


def _preserve_config(runner: ETLSmokeRunner, case_dir: Path) -> dict[str, Any]:
    preserve_columns = _as_set(runner.PRESERVE_COLUMNS)
    preserve_values = _as_set(runner.PRESERVE_VALUES)
    preserve_keys = _as_set(runner.PRESERVE_KEYS)
    token_keys = _as_set(runner.TOKEN_KEYS)

    cfg = _load_test_config(case_dir)
    if cfg:
        preserve_columns |= _as_set(cfg.get("preserve_columns"))
        preserve_values |= _as_set(cfg.get("preserve_values"))
        preserve_keys |= _as_set(cfg.get("preserve_keys"))
        token_keys |= _as_set(cfg.get("token_keys"))

    return {
        "preserve_columns": preserve_columns,
        "preserve_values": preserve_values,
        "preserve_keys": preserve_keys,
        "token_keys": token_keys,
        "looks_like_id_key": runner.looks_like_id_key,
        "split_composite": runner.split_composite_value,
    }


def _script_dir(script_root: Path, runner: ETLSmokeRunner) -> Path:
    rel = runner.SCRIPT_DIR
    if rel:
        return (script_root / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    return script_root.resolve()


def _python_for_script(script_dir: Path) -> str:
    venv_python = script_dir / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _new_day_name(case_dir: Path) -> str:
    """Unique job folder: YYYYMMDDTHHMMSS (append-only; mimics successive hotglue jobs)."""
    for _ in range(5):
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if not (case_dir / name).exists():
            return name
        time.sleep(1)
    raise SystemExit(f"could not allocate a unique day dir under {case_dir}")


def _today_from_day_name(day_name: str) -> str:
    """Pass date portion to etl today= (YYYYMMDD)."""
    return day_name.split("T", 1)[0][:8]


def _fixtures_dir(day_dir: Path) -> Path:
    return day_dir / "fixtures"


def _expected_dir(day_dir: Path) -> Path:
    return day_dir / "expected_output"


def _runtime_dir(day_dir: Path) -> Path:
    return day_dir / "test_runtime"


def record_case(
    *,
    script_root: Path,
    tests_dir: Path,
    case_name: str,
    force: bool,
    no_scrub: bool,
) -> None:
    """Append a new datetime day with scrubbed input under fixtures/.

    Layout per day::

        <YYYYMMDDTHHMMSS>/
          fixtures/          # INPUT (sync-output, snapshots, mapping, catalog)
          expected_output/   # filled by generate
          test_runtime/      # filled by generate/run (gitignored)

    Each record is a new folder (append-only). generate/run chain snapshots from
    the previous day's post-ETL output.
    """
    del force  # ETL record is append-only; --force is a no-op here
    runner = load_runner(tests_dir)
    case_dir = tests_dir / case_name
    case_dir.mkdir(parents=True, exist_ok=True)

    day_name = _new_day_name(case_dir)
    day_dir = case_dir / day_name
    fixtures = _fixtures_dir(day_dir)

    script_dir = _script_dir(script_root, runner)
    raw_root = runner.RAW_INPUT
    raw_dir = Path(raw_root).resolve() if raw_root else script_dir

    sync_src = raw_dir / "sync-output"
    snap_src = raw_dir / "snapshots"
    if not sync_src.is_dir():
        raise SystemExit(f"missing raw sync-output at {sync_src}")

    fixtures.mkdir(parents=True)
    _copy_tree(sync_src, fixtures / "sync-output")

    # First day: seed snapshots in fixtures. Later days: empty — generate/run chain
    # previous day's post-ETL snapshots into test_runtime.
    existing_days = list_day_dirs(case_dir)
    is_first_day = len([d for d in existing_days if d.name != day_name]) == 0
    if is_first_day and snap_src.is_dir():
        _copy_tree(snap_src, fixtures / "snapshots")
    else:
        (fixtures / "snapshots").mkdir(exist_ok=True)

    mapping_src = script_dir / "mapping.json"
    if mapping_src.is_file():
        shutil.copy2(mapping_src, fixtures / "mapping.json")

    for extra in ("catalog.json", "selectedTables.json", "config.json"):
        src = script_dir / extra
        if src.is_file():
            shutil.copy2(src, fixtures / extra)

    if not no_scrub:
        cfg = _preserve_config(runner, case_dir)
        scrub_tree(fixtures, **cfg)
        print(f"Scrubbed input fixtures into {fixtures}")
    else:
        print(f"Copied unsanitized input fixtures into {fixtures} (--no-scrub)")


def _prepare_runtime_from_fixtures(
    *,
    fixtures: Path,
    runtime: Path,
    script_dir: Path,
) -> None:
    """Materialize a job ROOT_DIR from fixtures/ (input only)."""
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    for item in fixtures.iterdir():
        dest = runtime / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    mapping = runtime / "mapping.json"
    if not mapping.is_file():
        src = script_dir / "mapping.json"
        if src.is_file():
            shutil.copy2(src, mapping)
    (runtime / "etl-output").mkdir(exist_ok=True)
    (runtime / "snapshots").mkdir(exist_ok=True)


def _run_etl(
    *,
    script_dir: Path,
    root_dir: Path,
    runner: ETLSmokeRunner,
    case_dir: Path,
    case_name: str,
    today: str,
) -> None:
    python = _python_for_script(script_dir)
    etl_py = script_dir / "etl.py"
    if not etl_py.is_file():
        raise SystemExit(f"missing etl.py at {etl_py}")

    settings = _case_env(runner, case_dir, case_name)
    flow = settings["flow"]
    tenant = settings["tenant"]
    job_type = settings["job_type"]

    env = os.environ.copy()
    env.update(
        {
            "ROOT_DIR": str(root_dir),
            "base_input_dir": str(root_dir / "sync-output"),
            "output_dir": str(root_dir / "etl-output"),
            "snapshot_dir": str(root_dir / "snapshots"),
            "today": today,
            "TENANT": tenant,
            "FLOW": flow,
            "JOB_TYPE": job_type,
            "PYTHONPATH": str(script_dir),
            "VIRTUAL_ENV": str(script_dir / ".venv"),
            "PATH": f"{script_dir / '.venv' / 'bin'}:{env.get('PATH', '')}",
        }
    )

    print(
        f"command [ROOT_DIR={root_dir} FLOW={flow} JOB_TYPE={job_type} "
        f"TENANT={tenant} {python} {etl_py}]"
    )
    subprocess.run([python, str(etl_py)], env=env, check=True, cwd=str(root_dir))
    runner.after_etl(root_dir, flow=flow)


def _day_has_expected(day_dir: Path) -> bool:
    return (_expected_dir(day_dir) / "etl-output").is_dir()


def generate_case(
    *,
    script_root: Path,
    tests_dir: Path,
    case_name: str,
    force: bool,
) -> None:
    runner = load_runner(tests_dir)
    case_dir = tests_dir / case_name
    days = list_day_dirs(case_dir)
    if not days:
        raise SystemExit(
            f"no day dirs (YYYYMMDD or YYYYMMDDTHHMMSS) under {case_dir}; run record first"
        )

    if not force and all(_day_has_expected(d) for d in days):
        raise SystemExit(
            f"all days under {case_dir} already have expected_output/; "
            "pass --force to regenerate"
        )

    script_dir = _script_dir(script_root, runner)
    previous_snapshots: Path | None = None
    generated = 0

    for day_dir in days:
        fixtures = _fixtures_dir(day_dir)
        if not fixtures.is_dir():
            raise SystemExit(f"missing input fixtures at {fixtures}; re-record this day")

        expected = _expected_dir(day_dir)
        runtime = _runtime_dir(day_dir)

        if _day_has_expected(day_dir) and not force:
            # Keep chain continuity from committed expected snapshots.
            snap = expected / "snapshots"
            if snap.is_dir():
                previous_snapshots = snap
            print(f"Skipping {day_dir.name} (expected_output exists; use --force to redo)")
            continue

        if expected.exists():
            shutil.rmtree(expected)
        if runtime.exists():
            shutil.rmtree(runtime)

        _prepare_runtime_from_fixtures(
            fixtures=fixtures, runtime=runtime, script_dir=script_dir
        )

        snap_dest = runtime / "snapshots"
        seeded = fixtures / "snapshots"
        if previous_snapshots and previous_snapshots.is_dir():
            if snap_dest.exists():
                shutil.rmtree(snap_dest)
            shutil.copytree(previous_snapshots, snap_dest)
            print(f"Snapshot chained: '{previous_snapshots}' -> '{snap_dest}'")
        elif seeded.is_dir() and any(seeded.iterdir()):
            pass
        else:
            snap_dest.mkdir(exist_ok=True)

        _run_etl(
            script_dir=script_dir,
            root_dir=runtime,
            runner=runner,
            case_dir=case_dir,
            case_name=case_name,
            today=_today_from_day_name(day_dir.name),
        )

        expected.mkdir(parents=True)
        etl_out = runtime / "etl-output"
        if etl_out.is_dir():
            shutil.copytree(etl_out, expected / "etl-output")
        snap_out = runtime / "snapshots"
        if snap_out.is_dir():
            shutil.copytree(snap_out, expected / "snapshots")

        previous_snapshots = snap_out
        generated += 1
        print(f"Wrote {expected}")

    if generated == 0 and not force:
        raise SystemExit("nothing to generate; pass --force to regenerate")


def run_case(
    *,
    script_root: Path,
    tests_dir: Path,
    case_name: str,
) -> None:
    """Replay ETL into each day's test_runtime/ (comparison via etl_driver)."""
    runner = load_runner(tests_dir)
    case_dir = tests_dir / case_name
    days = list_day_dirs(case_dir)
    if not days:
        raise SystemExit(f"no day dirs under {case_dir}; run record first")

    missing = [d.name for d in days if not _day_has_expected(d)]
    if missing:
        raise SystemExit(
            f"missing expected_output for days {missing}; run generate first"
        )

    script_dir = _script_dir(script_root, runner)
    previous_snapshots: Path | None = None

    for day_dir in days:
        fixtures = _fixtures_dir(day_dir)
        runtime = _runtime_dir(day_dir)
        if runtime.exists():
            shutil.rmtree(runtime)

        _prepare_runtime_from_fixtures(
            fixtures=fixtures, runtime=runtime, script_dir=script_dir
        )

        snap_dest = runtime / "snapshots"
        seeded = fixtures / "snapshots"
        if previous_snapshots and previous_snapshots.is_dir():
            if snap_dest.exists():
                shutil.rmtree(snap_dest)
            shutil.copytree(previous_snapshots, snap_dest)
            print(f"Snapshot chained: '{previous_snapshots}' -> '{snap_dest}'")
        elif not (seeded.is_dir() and any(seeded.iterdir())):
            snap_dest.mkdir(exist_ok=True)

        _run_etl(
            script_dir=script_dir,
            root_dir=runtime,
            runner=runner,
            case_dir=case_dir,
            case_name=case_name,
            today=_today_from_day_name(day_dir.name),
        )
        previous_snapshots = runtime / "snapshots"
