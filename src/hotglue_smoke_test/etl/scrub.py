"""Deterministic DataFrame/JSON scrubbing for ETL fixtures (connector-agnostic)."""

from __future__ import annotations

import ast
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

ShouldScrubKey = Callable[[str], bool]
# Return an odd-length interleaved list ``[part, sep, part, ...]``: even indices are
# scrubbed via replace, odd indices are kept as literal separators. Else None.
SplitComposite = Callable[[str], list[str] | None]


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


def _is_preserved(value: Any, preserve_values: set[Any]) -> bool:
    """Membership test that tolerates unhashable cells (Parquet list/struct columns)."""
    try:
        return value in preserve_values
    except TypeError:
        return False


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
        if _is_preserved(value, preserve_values):
            return value
        # Timestamps/dates: keep as-is (ETL filters/groupbys; not PII scrub targets).
        if _is_temporal(value):
            return value

        # Optional connector split: scrub each part; PRESERVE_VALUES keeps enums.
        if isinstance(value, str) and split_composite is not None:
            parts = split_composite(value)
            if parts is not None:
                if len(parts) % 2 == 0:
                    raise ValueError(
                        "split_composite_value must return an odd-length "
                        f"[part, sep, part, ...] list; got {parts!r}"
                    )
                return "".join(
                    part if index % 2 else str(replace(key, part))
                    for index, part in enumerate(parts)
                )

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
    key: str = "",
) -> Any:
    """Scrub JSON leaves (and optionally dict keys via should_scrub_key).

    ``key`` is the owning field name passed to ``replace_fn`` for leaves (and list
    items), so nested values keep identifier/PII field context.
    """
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for child_key, value in obj.items():
            key_str = child_key if isinstance(child_key, str) else str(child_key)
            new_key: Any = child_key
            if (
                scrub_dict_keys
                and isinstance(child_key, str)
                and child_key not in preserve_keys
                and child_key not in token_keys
                and should_scrub_key(child_key)
            ):
                new_key = replace_fn(key_str, child_key)

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
                    key=key_str,
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
                key=key,
            )
            for item in obj
        ]
    return replace_fn(key, obj)


def _maybe_parse_nested_cell(value: Any) -> Any:
    """
    Parse nested cell value;
    Try JSON first;
    Fall back to ast.literal_eval (same approach as gluestick's parse_objs());
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        obj = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return value
    return obj if isinstance(obj, (dict, list)) else value


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
        if _is_preserved(value, preserve_values):
            return value
        parsed = _maybe_parse_nested_cell(value)
        if hasattr(parsed, "tolist") and not isinstance(parsed, (str, bytes)):
            parsed = parsed.tolist()
        if isinstance(parsed, (dict, list)):
            scrubbed = scrub_json(
                parsed,
                replace_fn=replace_fn,
                preserve_keys=preserve_keys,
                token_keys=token_keys,
                should_scrub_key=should_scrub_key,
                # Nested payload keys are schema — scrub values only.
                scrub_dict_keys=False,
                key=column,
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
        # Keep raw cell text (empty/"NA"/"001"); numerics scrub as strings via PRESERVE_*.
        df = pd.read_csv(path, dtype=str, na_filter=False)
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
    skip_scrub_names: set[str],
    should_scrub_key: ShouldScrubKey,
    split_composite: SplitComposite | None = None,
    cache: dict | None = None,
) -> None:
    """Scrub parquet/csv/json under root in place (deterministic)."""
    cache = {} if cache is None else cache
    replace_fn = make_deterministic_replace_fn(
        preserve_values=preserve_values,
        cache=cache,
        split_composite=split_composite,
    )
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in skip_scrub_names:
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

