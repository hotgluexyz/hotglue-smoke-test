"""Base class for colocated ETL smoke config (subclass in record-etl.py)."""

from __future__ import annotations

import csv
import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ETLSmokeRunner(ABC):
    """Per-script smoke config. Mirror of VCRTapTestRunner for ETL (no VCR).

    Subclass in ``__smoke-tests__/record-etl.py`` and export ``Runner = YourClass``.
    """

    FLOW: str = ""
    TENANT: str | None = None
    JOB_TYPE: str = "read"
    # Optional override of script root relative to CLI script_root / cwd.
    SCRIPT_DIR: str | None = None
    # Optional alternate dir that holds live sync-output/snapshots for record.
    RAW_INPUT: str | None = None

    PRESERVE_COLUMNS: set[str] = set()
    PRESERVE_VALUES: set[Any] = set()
    PRESERVE_KEYS: set[str] = set()
    TOKEN_KEYS: set[str] = set()

    @abstractmethod
    def looks_like_id_key(self, key: str) -> bool:
        """Return True if a JSON *dict key* should be scrubbed as an entity id.

        Schema labels (``business_details``, ``fields``, …) must return False.
        """
        raise NotImplementedError

    def split_composite_value(self, value: str) -> tuple[str, str] | None:
        """Optionally split a composite string so the suffix stays real.

        Return ``(prefix, suffix)`` when only the prefix should be scrubbed
        (e.g. ``entity_id--USD`` → ``("entity_id", "USD")``). Default: scrub whole value.
        """
        return None

    def after_etl(self, root_dir: Path, *, flow: str | None = None) -> None:
        """Hook after ``etl.py`` exits. Default no-op (smoke has no target).

        ``flow`` is the case-effective FLOW (class default or test-config override).
        Override to simulate target-written id-map snapshots, etc.
        """
        return None


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
