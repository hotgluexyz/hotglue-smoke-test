"""Base class for colocated ETL smoke (subclass in record-etl.py).

Mirrors VCRBaseTestRunner: CLI shells out to record-etl.py → main() → run_test().
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from abc import ABC
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hotglue_smoke_test.artifacts import etl_datetime_has_expected, list_etl_datetime_dirs
from hotglue_smoke_test.etl.scrub import DEFAULT_SKIP_SCRUB_NAMES, scrub_tree


class ETLSmokeRunner(ABC):
    """Per-script smoke config + record/generate/run orchestration (no VCR).

    Subclass in ``__smoke-tests__/record-etl.py`` and call ``YourClass.main()``.
    """

    FLOW: str = ""
    TENANT: str | None = None
    JOB_TYPE: str = "read"
    # Optional override of script root relative to parent of __smoke-tests__.
    SCRIPT_DIR: str | None = None
    # Optional alternate dir that holds live sync-output/snapshots for record.
    RAW_INPUT: str | None = None

    PRESERVE_COLUMNS: set[str] = set()
    PRESERVE_VALUES: set[Any] = set()
    PRESERVE_KEYS: set[str] = set()
    TOKEN_KEYS: set[str] = set()
    # Filenames under fixtures/ left unsanitized (schema/mapping). Override in record-etl.py.
    SKIP_SCRUB_NAMES: set[str] = set(DEFAULT_SKIP_SCRUB_NAMES)

    def __init__(self, test_case: str, tests_dir: str | Path):
        self.test_case = test_case
        self.tests_dir = Path(tests_dir)
        self.case_dir = self.tests_dir / test_case
        self.mode = os.environ.get("SMOKE_TEST_MODE", "run")
        self.force = os.environ.get("SMOKE_TEST_FORCE") == "1"
        self.no_scrub = os.environ.get("SMOKE_TEST_NO_SCRUB") == "1"
        self.script_root = self._resolve_script_root()

    def _resolve_script_root(self) -> Path:
        """Script dir with etl.py: parent of __smoke-tests__, or SCRIPT_DIR override."""
        base = self.tests_dir.parent
        rel = self.SCRIPT_DIR
        if rel:
            return (base / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        return base.resolve()

    def should_scrub_key(self, key: str) -> bool:
        """Return True if a JSON *dict key* should be scrubbed (not only values).

        Schema labels must return False. Default: scrub values only.
        """
        return False

    def split_composite_value(self, value: str) -> tuple[str, str] | None:
        """Optionally split a composite so each side is scrubbed independently.

        Return ``(left, right)`` to rejoin as ``left--right`` after replace
        (``PRESERVE_VALUES`` keeps enums). Default: scrub the whole value.
        """
        return None

    def after_etl(self, root_dir: Path, *, flow: str | None = None) -> None:
        """Hook after ``etl.py`` exits. Default no-op (smoke has no target).

        ``flow`` is the case-effective FLOW (class default or test-config override).
        Override to simulate target-written id-map snapshots, etc.
        """
        return None

    @classmethod
    def main(cls) -> None:
        if len(sys.argv) != 2:
            print("Usage: record-etl.py <testcase>")
            sys.exit(1)
        test_case = sys.argv[1]
        tests_dir = Path(inspect.getfile(cls)).resolve().parent
        runner = cls(test_case, tests_dir)
        runner.run_test()

    def run_test(self) -> None:
        if self.mode == "record":
            self._record()
        elif self.mode == "generate":
            self._generate()
        elif self.mode == "run":
            self._run()
        else:
            raise SystemExit(f"unknown SMOKE_TEST_MODE={self.mode!r}")

    def _load_test_config(self) -> dict[str, Any]:
        path = self.case_dir / "test-config.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text())

    def _case_env(self) -> dict[str, str]:
        """Resolve FLOW / JOB_TYPE / TENANT: test-config.json overrides class defaults."""
        cfg = self._load_test_config()
        flow = cfg.get("flow") or cfg.get("FLOW") or self.FLOW
        job_type = cfg.get("job_type") or cfg.get("JOB_TYPE") or self.JOB_TYPE
        tenant = (
            cfg.get("tenant")
            or cfg.get("TENANT")
            or self.TENANT
            or self.test_case
        )
        if not flow:
            raise SystemExit(
                f"{type(self).__name__}.FLOW unset and no flow in "
                f"{self.case_dir / 'test-config.json'}"
            )
        return {"flow": str(flow), "job_type": str(job_type), "tenant": str(tenant)}

    def _preserve_config(self) -> dict[str, Any]:
        preserve_columns = _as_set(self.PRESERVE_COLUMNS)
        preserve_values = _as_set(self.PRESERVE_VALUES)
        preserve_keys = _as_set(self.PRESERVE_KEYS)
        token_keys = _as_set(self.TOKEN_KEYS)

        cfg = self._load_test_config()
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
            "should_scrub_key": self.should_scrub_key,
            "split_composite": self.split_composite_value,
            "skip_scrub_names": _as_set(self.SKIP_SCRUB_NAMES),
        }

    def _python_for_script(self) -> str:
        venv_python = self.script_root / ".venv" / "bin" / "python"
        if venv_python.is_file():
            return str(venv_python)
        return sys.executable

    def _record(self) -> None:
        """Append a new UTC datetime folder with scrubbed input under fixtures/."""
        self.case_dir.mkdir(parents=True, exist_ok=True)

        job_name = _new_datetime_dir_name(self.case_dir)
        job_dir = self.case_dir / job_name
        fixtures = _fixtures_dir(job_dir)

        raw_root = self.RAW_INPUT
        raw_dir = Path(raw_root).resolve() if raw_root else self.script_root

        sync_src = raw_dir / "sync-output"
        snap_src = raw_dir / "snapshots"
        if not sync_src.is_dir():
            raise SystemExit(f"missing raw sync-output at {sync_src}")

        fixtures.mkdir(parents=True)
        _copy_tree(sync_src, fixtures / "sync-output")

        # First job: seed snapshots in fixtures. Later jobs: empty — generate/run
        # chain previous job's post-ETL snapshots into test_runtime.
        existing = list_etl_datetime_dirs(self.case_dir)
        is_first = len([d for d in existing if d.name != job_name]) == 0
        if is_first and snap_src.is_dir():
            _copy_tree(snap_src, fixtures / "snapshots")
        else:
            (fixtures / "snapshots").mkdir(exist_ok=True)

        mapping_src = self.script_root / "mapping.json"
        if mapping_src.is_file():
            shutil.copy2(mapping_src, fixtures / "mapping.json")

        for extra in ("catalog.json", "selectedTables.json", "config.json"):
            src = self.script_root / extra
            if src.is_file():
                shutil.copy2(src, fixtures / extra)

        if not self.no_scrub:
            scrub_tree(fixtures, **self._preserve_config())
            print(f"Scrubbed input fixtures into {fixtures}")
        else:
            print(f"Copied unsanitized input fixtures into {fixtures} (--no-scrub)")

    def _prepare_runtime_from_fixtures(self, fixtures: Path, runtime: Path) -> None:
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
            src = self.script_root / "mapping.json"
            if src.is_file():
                shutil.copy2(src, mapping)
        (runtime / "etl-output").mkdir(exist_ok=True)
        (runtime / "snapshots").mkdir(exist_ok=True)

    def _run_etl(self, root_dir: Path, today: str) -> None:
        python = self._python_for_script()
        etl_py = self.script_root / "etl.py"
        if not etl_py.is_file():
            raise SystemExit(f"missing etl.py at {etl_py}")

        settings = self._case_env()
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
                "PYTHONPATH": str(self.script_root),
                "VIRTUAL_ENV": str(self.script_root / ".venv"),
                "PATH": f"{self.script_root / '.venv' / 'bin'}:{env.get('PATH', '')}",
            }
        )

        print(
            f"command [ROOT_DIR={root_dir} FLOW={flow} JOB_TYPE={job_type} "
            f"TENANT={tenant} {python} {etl_py}]"
        )
        subprocess.run([python, str(etl_py)], env=env, check=True, cwd=str(root_dir))
        self.after_etl(root_dir, flow=flow)

    def _generate(self) -> None:
        """Replay fixtures → expected_output/ (folder policy is CLI artifacts.py)."""
        jobs = list_etl_datetime_dirs(self.case_dir)
        previous_snapshots: Path | None = None

        for job_dir in jobs:
            fixtures = _fixtures_dir(job_dir)
            if not fixtures.is_dir():
                raise SystemExit(
                    f"missing input fixtures at {fixtures}; re-record this datetime"
                )

            expected = _expected_dir(job_dir)
            runtime = _runtime_dir(job_dir)

            # Partial regenerate: keep jobs that already have expected unless --force.
            if etl_datetime_has_expected(job_dir) and not self.force:
                snap = expected / "snapshots"
                if snap.is_dir():
                    previous_snapshots = snap
                print(
                    f"Skipping {job_dir.name} "
                    "(expected_output exists; use --force to redo)"
                )
                continue

            if expected.exists():
                shutil.rmtree(expected)
            if runtime.exists():
                shutil.rmtree(runtime)

            self._prepare_runtime_from_fixtures(fixtures, runtime)

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

            self._run_etl(runtime, _today_from_datetime_dir(job_dir.name))

            expected.mkdir(parents=True)
            etl_out = runtime / "etl-output"
            if etl_out.is_dir():
                shutil.copytree(etl_out, expected / "etl-output")
            snap_out = runtime / "snapshots"
            if snap_out.is_dir():
                shutil.copytree(snap_out, expected / "snapshots")

            previous_snapshots = snap_out
            print(f"Wrote {expected}")

    def _run(self) -> None:
        """Replay ETL into each datetime's test_runtime/ (comparison via etl_driver)."""
        jobs = list_etl_datetime_dirs(self.case_dir)
        previous_snapshots: Path | None = None

        for job_dir in jobs:
            fixtures = _fixtures_dir(job_dir)
            runtime = _runtime_dir(job_dir)
            if runtime.exists():
                shutil.rmtree(runtime)

            self._prepare_runtime_from_fixtures(fixtures, runtime)

            snap_dest = runtime / "snapshots"
            seeded = fixtures / "snapshots"
            if previous_snapshots and previous_snapshots.is_dir():
                if snap_dest.exists():
                    shutil.rmtree(snap_dest)
                shutil.copytree(previous_snapshots, snap_dest)
                print(f"Snapshot chained: '{previous_snapshots}' -> '{snap_dest}'")
            elif not (seeded.is_dir() and any(seeded.iterdir())):
                snap_dest.mkdir(exist_ok=True)

            self._run_etl(runtime, _today_from_datetime_dir(job_dir.name))
            previous_snapshots = runtime / "snapshots"


def promote_external_ids_to_snapshots(
    root_dir: Path,
    flow: str,
    *,
    remote_id_prefix: str = "remote-",
) -> None:
    """Generic helper: append singer ``externalId`` rows into ``*_{flow}.snapshot.csv``.

    Call from ``after_etl`` when the script's target would normally write InputId/RemoteId
    maps that the next job's ETL reads.
    """
    singer = root_dir / "etl-output" / "data.singer"
    if not singer.is_file():
        return

    by_stream: dict[str, list[str]] = {}
    with singer.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("type") != "RECORD":
                continue
            ext = (msg.get("record") or {}).get("externalId")
            stream = msg.get("stream")
            if not stream or ext is None or ext == "":
                continue
            by_stream.setdefault(stream, []).append(str(ext))

    snaps = root_dir / "snapshots"
    snaps.mkdir(exist_ok=True)
    for stream, external_ids in by_stream.items():
        path = snaps / f"{stream}_{flow}.snapshot.csv"
        existing: dict[str, str] = {}
        if path.is_file():
            with path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("InputId"):
                        existing[row["InputId"]] = row.get("RemoteId") or ""

        added = 0
        for ext in external_ids:
            if ext in existing:
                continue
            remote = remote_id_prefix + hashlib.sha1(ext.encode()).hexdigest()[:12]
            existing[ext] = remote
            added += 1

        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["InputId", "RemoteId"])
            writer.writeheader()
            for input_id, remote_id in existing.items():
                writer.writerow({"InputId": input_id, "RemoteId": remote_id})

        if added:
            print(f"Promoted {added} {stream} id(s) -> {path.name}")


def _as_set(value: Any) -> set:
    if value is None:
        return set()
    if isinstance(value, set):
        return value
    return set(value)


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _new_datetime_dir_name(case_dir: Path) -> str:
    """Unique job folder: UTC ``YYYYMMDDTHHMMSS`` (append-only; successive hotglue jobs)."""
    for _ in range(5):
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if not (case_dir / name).exists():
            return name
        time.sleep(1)
    raise SystemExit(f"could not allocate a unique UTC datetime dir under {case_dir}")


def _today_from_datetime_dir(name: str) -> str:
    """Calendar date (YYYYMMDD) for etl ``today`` env from a UTC datetime folder name."""
    return name.split("T", 1)[0][:8]


def _fixtures_dir(job_dir: Path) -> Path:
    return job_dir / "fixtures"


def _expected_dir(job_dir: Path) -> Path:
    return job_dir / "expected_output"


def _runtime_dir(job_dir: Path) -> Path:
    return job_dir / "test_runtime"
