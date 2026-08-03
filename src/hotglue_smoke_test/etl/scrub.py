"""Deterministic DataFrame/JSON scrubbing for ETL fixtures (connector-agnostic)."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from faker import Faker

from hotglue_smoke_test.vcr.sanitize import make_faker_replace_fn, redact_credential

# CSV fixtures store datetimes as strings; keep ISO-looking values unscrubbed.
_ISO_TEMPORAL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_HASH_SUFFIX = ".hash.snapshot"
# Schema / mapping files — scrubbing breaks ETL field paths and catalog_types.
# Overridable via ETLSmokeRunner.SKIP_SCRUB_NAMES in record-etl.py.
DEFAULT_SKIP_SCRUB_NAMES = frozenset(
    {
        "catalog.json",
        "selectedTables.json",
        "mapping.json",
    }
)

ShouldScrubKey = Callable[[str], bool]
# Return (left, right) to scrub each side via replace (PRESERVE_VALUES applies); else None.
SplitComposite = Callable[[str], tuple[str, str] | None]


def stable_seed(value: Any) -> int:
    """Stable 31-bit seed from (type, value) for cross-process deterministic fakes."""
    raw = f"{type(value).__name__}:{value!r}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:16], 16) % (2**31)


def _cache_key(value: Any) -> Any | None:
    try:
        raw_key = tuple(value) if isinstance(value, list) else value
        hash(raw_key)
        return (type(value), raw_key)
    except TypeError:
        return None


def _is_temporal(value: Any) -> bool:
    if isinstance(
        value,
        (datetime.datetime, datetime.date, datetime.time, pd.Timestamp, pd.Timedelta),
    ):
        return True
    return isinstance(value, str) and bool(_ISO_TEMPORAL_RE.match(value.strip()))


def make_deterministic_replace_fn(
    *,
    preserve_values: set[Any],
    cache: dict,
    split_composite: SplitComposite | None = None,
) -> Callable[[str, Any], Any]:
    """Same original → same fake via per-value Faker seed (no committed map)."""

    def replace(key: str, value: Any) -> Any:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return value
        if value in preserve_values:
            return value
        # Timestamps/dates: keep as-is (ETL filters/groupbys; not PII scrub targets).
        if _is_temporal(value):
            return value

        field = key.split(".")[-1].replace("_", "").lower()
        # Keep amounts/rates/counts unless the column is clearly an id.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if field not in {"id", "remoteid", "inputid", "externalid"} and not field.endswith(
                "id"
            ):
                return value

        # Optional connector split: scrub each side; PRESERVE_VALUES keeps enums.
        if isinstance(value, str) and split_composite is not None:
            parts = split_composite(value)
            if parts is not None:
                left, right = parts
                return f"{replace(key, left)}--{replace(key, right)}"

        ck = _cache_key(value)
        if ck is not None and ck in cache:
            return cache[ck]

        faker = Faker()
        faker.seed_instance(stable_seed(value))
        fake = make_faker_replace_fn(faker, {})(key, value)

        if ck is not None:
            cache[ck] = fake
        return fake

    return replace


def scrub_json(
    obj: Any,
    *,
    replace_fn: Callable[[str, Any], Any],
    preserve_keys: set[str],
    token_keys: set[str],
    should_scrub_key: ShouldScrubKey,
    scrub_dict_keys: bool = True,
) -> Any:
    """Scrub JSON leaves (and optionally dict keys via should_scrub_key)."""
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            key_str = key if isinstance(key, str) else str(key)
            new_key: Any = key
            if (
                scrub_dict_keys
                and isinstance(key, str)
                and key not in preserve_keys
                and key not in token_keys
                and should_scrub_key(key)
            ):
                new_key = replace_fn(key_str, key)

            if key_str in token_keys:
                out[new_key] = redact_credential(value)
            elif key_str in preserve_keys:
                out[new_key] = value
            else:
                out[new_key] = scrub_json(
                    value,
                    replace_fn=replace_fn,
                    preserve_keys=preserve_keys,
                    token_keys=token_keys,
                    should_scrub_key=should_scrub_key,
                    scrub_dict_keys=scrub_dict_keys,
                )
        return out
    if isinstance(obj, list):
        return [
            scrub_json(
                item,
                replace_fn=replace_fn,
                preserve_keys=preserve_keys,
                token_keys=token_keys,
                should_scrub_key=should_scrub_key,
                scrub_dict_keys=scrub_dict_keys,
            )
            for item in obj
        ]
    return replace_fn("", obj)


def _maybe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def scrub_series(
    series: pd.Series,
    column: str,
    *,
    replace_fn: Callable[[str, Any], Any],
    preserve_columns: set[str],
    preserve_values: set[Any],
    preserve_keys: set[str],
    token_keys: set[str],
    should_scrub_key: ShouldScrubKey,
) -> pd.Series:
    if column in preserve_columns:
        return series

    def cell(value: Any) -> Any:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return value
        if value in preserve_values:
            return value
        parsed = _maybe_json_loads(value)
        if isinstance(parsed, (dict, list)):
            scrubbed = scrub_json(
                parsed,
                replace_fn=replace_fn,
                preserve_keys=preserve_keys,
                token_keys=token_keys,
                should_scrub_key=should_scrub_key,
                # Nested payload keys are schema — scrub values only.
                scrub_dict_keys=False,
            )
            return json.dumps(scrubbed) if isinstance(value, str) else scrubbed
        return replace_fn(column, value)

    return series.map(cell)


def scrub_dataframe(
    df: pd.DataFrame,
    *,
    replace_fn: Callable[[str, Any], Any],
    preserve_columns: set[str],
    preserve_values: set[Any],
    preserve_keys: set[str],
    token_keys: set[str],
    should_scrub_key: ShouldScrubKey,
    hash_pk_only: bool = False,
) -> pd.DataFrame:
    out = df.copy()
    for column in out.columns:
        if hash_pk_only and column == "hash":
            continue
        out[column] = scrub_series(
            out[column],
            column,
            replace_fn=replace_fn,
            preserve_columns=preserve_columns,
            preserve_values=preserve_values,
            preserve_keys=preserve_keys,
            token_keys=token_keys,
            should_scrub_key=should_scrub_key,
        )
    return out


def scrub_file(
    path: Path,
    *,
    replace_fn: Callable[[str, Any], Any],
    preserve_columns: set[str],
    preserve_values: set[Any],
    preserve_keys: set[str],
    token_keys: set[str],
    should_scrub_key: ShouldScrubKey,
) -> None:
    suffix = path.suffix.lower()
    hash_pk_only = _HASH_SUFFIX in path.name

    if suffix == ".parquet":
        df = pd.read_parquet(path)
        scrubbed = scrub_dataframe(
            df,
            replace_fn=replace_fn,
            preserve_columns=preserve_columns,
            preserve_values=preserve_values,
            preserve_keys=preserve_keys,
            token_keys=token_keys,
            should_scrub_key=should_scrub_key,
            hash_pk_only=hash_pk_only,
        )
        scrubbed.to_parquet(path, index=False)
        return

    if suffix == ".csv":
        df = pd.read_csv(path)
        scrubbed = scrub_dataframe(
            df,
            replace_fn=replace_fn,
            preserve_columns=preserve_columns,
            preserve_values=preserve_values,
            preserve_keys=preserve_keys,
            token_keys=token_keys,
            should_scrub_key=should_scrub_key,
            hash_pk_only=hash_pk_only,
        )
        scrubbed.to_csv(path, index=False)
        return

    if suffix == ".json":
        data = json.loads(path.read_text())
        scrubbed = scrub_json(
            data,
            replace_fn=replace_fn,
            preserve_keys=preserve_keys,
            token_keys=token_keys,
            should_scrub_key=should_scrub_key,
        )
        path.write_text(json.dumps(scrubbed, indent=4) + "\n")
        return


def scrub_tree(
    root: Path,
    *,
    preserve_columns: set[str],
    preserve_values: set[Any],
    preserve_keys: set[str],
    token_keys: set[str],
    should_scrub_key: ShouldScrubKey,
    split_composite: SplitComposite | None = None,
    skip_scrub_names: set[str] | frozenset[str] | None = None,
    cache: dict | None = None,
) -> None:
    """Scrub parquet/csv/json under root in place (deterministic)."""
    cache = {} if cache is None else cache
    skip_names = (
        DEFAULT_SKIP_SCRUB_NAMES
        if skip_scrub_names is None
        else frozenset(skip_scrub_names)
    )
    replace_fn = make_deterministic_replace_fn(
        preserve_values=preserve_values,
        cache=cache,
        split_composite=split_composite,
    )
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in skip_names:
            continue
        if path.suffix.lower() not in {".parquet", ".csv", ".json"}:
            continue
        scrub_file(
            path,
            replace_fn=replace_fn,
            preserve_columns=preserve_columns,
            preserve_values=preserve_values,
            preserve_keys=preserve_keys,
            token_keys=token_keys,
            should_scrub_key=should_scrub_key,
        )

