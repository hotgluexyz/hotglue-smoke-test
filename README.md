# hotglue-smoke-test

Smoke-test harness for hotglue taps, ETLs, and targets scripts with **colocated** `__smoke-tests__/` fixtures.

**Tap:** record HTTP (then scrub) → generate data.singer/state.json → run (replay + compare).  
**ETL:** record (copy raw fixtures + scrub) → generate (`etl.py` → expected_output) → run (replay + compare).  
**Target:** record HTTP from `data.singer` (then scrub input + cassette together) → generate state.json → run (replay + compare). 

Run from the connector repo root or on the script directory (after installing into that venv).

## Quickstart

Install into the connector/script venv, then run from that directory.

```bash
uv pip install hotglue-smoke-test
```

### Tap

Need `__smoke-tests__/record-vcr.py` and a case folder with live `config.json` and `catalog-selected.json`:

```python
# __smoke-tests__/record-vcr.py
from hotglue_smoke_test.vcr.tap import VCRTapTestRunner

class MyTapSmoke(VCRTapTestRunner):
    # Optional: extra auth headers beyond default "authorization"
    FILTER_HEADERS = [*VCRTapTestRunner.FILTER_HEADERS, "X-Vendor-Access-Token"]
    # Keep values the tap reuses (PK, replication key, pagination / OAuth timing)
    PRESERVE_KEYS = {"id", "updatedAt", "cursor", "expires_in"}

    def module(self) -> str:
        return "tap_example"

    def launch(self):
        from tap_example.tap import TapExample
        TapExample.cli()

if __name__ == "__main__":
    MyTapSmoke.main()
```

```bash
hotglue-smoke-test record orders_test     # live HTTP → fixtures/vcr.yaml → scrub
hotglue-smoke-test generate orders_test   # replay → expected_output/data.singer
hotglue-smoke-test run orders_test        # replay → compare
```

`module` + `launch` are required. Override `scrub_response_body()` for connector-specific response-body rules, or `scrub_uri()` to scrub PII in request paths or query strings:

```python
def scrub_uri(self, uri: str) -> str:
    return uri.replace("person@example.com", "fake@example.com")
```

### Target

Same flow as the tap, with `data.singer` as the input instead of a catalog. Need
`__smoke-tests__/record-vcr.py` and a case folder with live `config.json` and `data.singer`:

```python
# __smoke-tests__/record-vcr.py
from hotglue_smoke_test.vcr.target import VCRTargetTestRunner

class MyTargetSmoke(VCRTargetTestRunner):
    # Same meaning as the tap: values under these keys stay real (opaque cursors,
    # external ids the target decodes). Numbers, UUIDs, GIDs, URLs and timestamps
    # are already kept by shape, so list only what those rules miss.
    PRESERVE_KEYS = {"cursor", "externalId"}
    # Literal values the target branches on — kept real so replay takes the same path
    PRESERVE_VALUES = {"ACTIVE", "USD"}

    def module(self) -> str:
        return "target_example"

    def launch(self):
        from target_example.target import TargetExample
        TargetExample.cli()

if __name__ == "__main__":
    MyTargetSmoke.main()
```

```bash
hotglue-smoke-test record orders_test     # data.singer → live HTTP → scrub input + cassette
hotglue-smoke-test generate orders_test   # replay → expected_output/state.json
hotglue-smoke-test run orders_test        # replay → compare
```

**Why target scrub differs from tap scrub.** A target feeds Singer values into requests
and reuses values from earlier responses to build later ones (lookup before write). The
same value shows up under different field names, embedded in strings like
`gid://shopify/Customer/123`, or in a URI instead of a body — so scrubbing each surface
independently breaks replay. `record` therefore scrubs `data.singer` and the cassette in
one pass:

1. **Collect before replacing.** Singer input, then response bodies, then request bodies,
   then URIs — the full value set is known before anything is rewritten.
2. **Map by value, not field name.** One value → one fake, applied everywhere it appears,
   even when a response hands it back as `contactRef` instead of `email`.
3. **Reused values are references.** A value seen in two or more places is treated as a
   lookup key and is also replaced inside larger strings (notes, GraphQL documents, filters)
   when it is a whole token. A shorter value does not rewrite a longer identifier that
   merely starts with it.
4. **Structural values stay real.** Numbers, UUIDs, GIDs, URLs, and timestamps aren't
   PII, and keeping them is what keeps replay matching. They are recognised by value
   shape, never by field name — a name rule like "…Ref"/"…Id" would keep live PII the
   moment an API returns an email as `primaryContactRef`. Anything else that must stay
   real goes in `PRESERVE_KEYS`, exactly as in the tap.
5. **Fakes are derived from the value** (same markers and `stable_seed` as the ETL
   scrub), so the map lives in memory only — nothing to commit, re-recording yields the
   same fakes, and a value scrubbed in an ETL fixture gets the same fake here. Format
   comes from the field name when it is a known PII name, otherwise from the value, so
   an email stays an email even under a meaningless name.
6. **`config.json` credentials** are redacted in the same pass.

Connector code builds request bodies and URIs, so those surfaces only introduce new values
through PII-named fields (`email`, `phone`, `firstName`, …); everything else there is
matched against values already collected. Use `PRESERVE_VALUES` for enums the target
branches on, `PRESERVE_KEYS` for fields that must stay real, and `scrub_uri()` for
connector-specific URI PII.

### ETL

Need live `sync-output/` next to `etl.py` (seed `snapshots/` optional) and `__smoke-tests__/record-etl.py`:

```python
# __smoke-tests__/record-etl.py
from pathlib import Path
from hotglue_smoke_test.etl import ETLSmokeRunner, promote_external_ids_to_snapshots

class MyETLSmoke(ETLSmokeRunner):
    # Keep enum/filter columns and literal values the ETL compares as constants
    PRESERVE_COLUMNS = {"status", "currency"}
    PRESERVE_VALUES = {"ACTIVE", "USD"}
    PRESERVE_KEYS = {"version"}          # JSON keys whose values stay real
    TOKEN_KEYS = {"ApiKey"}              # force-scrub these values (san***)

    def after_etl(self, root_dir: Path, *, flow: str | None = None) -> None:
        # Optional: simulate target-written id maps between jobs
        if flow:
            promote_external_ids_to_snapshots(root_dir, flow)

if __name__ == "__main__":
    MyETLSmoke.main()
```

```bash
hotglue-smoke-test record orders_test     # sync-output → <UTC>/fixtures/ (scrubbed)
hotglue-smoke-test generate orders_test   # etl.py → <UTC>/expected_output/
hotglue-smoke-test run orders_test        # replay → compare
```

A bare subclass works if scrub defaults are enough. Set `flow` / `job_type` / `tenant` in `<case>/test-config.json` (or on the runner) only when you must override `etl.py` defaults or match `*_<flow>.snapshot.*` names.

## Folders layout
### Tap
```
tap-foo/
  __smoke-tests__/
    record-vcr.py
  __smoke-tests__/some_stream_test/
    config.json
    catalog-selected.json
    fixtures/vcr.yaml
    expected_output/data.singer
```
### Target
```
target-foo/
  __smoke-tests__/
    record-vcr.py
  __smoke-tests__/some_stream_test/
    config.json
    data.singer            # INPUT (scrubbed in place by record)
    fixtures/vcr.yaml
    expected_output/state.json
```
### ETL

Mimics successive hotglue jobs: each `record` appends a new UTC datetime folder (`YYYYMMDDTHHMMSS`); `generate`/`run` chain snapshots from the previous job’s post-ETL output.

**`fixtures/` is the input** (scrubbed tap `sync-output`, seed `snapshots`, `catalog.json`). `mapping.json` stays beside `etl.py` and is copied only into `test_runtime/`.

```
script-dir/                  # folder with etl.py
  __smoke-tests__/
    record-etl.py            # runner + connector-specific scrub rules
    <case>_test/
      20260528T120000/       # job 1
        fixtures/            # INPUT (committed)
          sync-output/
          snapshots/         # seed only on first day
          catalog.json
        expected_output/     # from generate (committed)
          etl-output/
          snapshots/
        test_runtime/        # from generate/run (gitignored)
      20260529T120000/       # job 2 (new record; snapshots chained at generate/run)
        fixtures/
          sync-output/
        expected_output/
        test_runtime/
```

## Install

```bash
uv pip install hotglue-smoke-test
# pin when needed, e.g. uv pip install 'hotglue-smoke-test~=1.0.0'
```

Dev / unreleased branch:

```bash
uv pip install "hotglue-smoke-test @ git+https://github.com/hotgluexyz/hotglue-smoke-test.git@<branch>"
```

## Release (PyPI)

Uses [`uv build` / `uv publish`](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi) with trusted publishing (no `PYPI_API_TOKEN`).

One-time setup:
1. GitHub → Settings → Environments → create `pypi`.
2. PyPI → project → Publishing → add a trusted publisher matching this repo/workflow/`pypi` environment.

Then bump `version` in `pyproject.toml`, commit, tag, push the tag:

```bash
# after merging to main with version bumped
git tag 1.0.0
git push origin 1.0.0
```

## Commands

```bash
# 1.1 TAP - Record VCR cassette (live API; discards Singer output), then scrub secrets/PII
# 1.2 ETL - Append datetime/fixtures/ (input: sync-output + seed snapshots), scrub
hotglue-smoke-test record orders_test

# 2.1 TAP - Replay cassette → write expected_output/
# 2.2 ETL - Per datetime folder: etl.py → datetime/expected_output/; skip folders that already have it unless --force
hotglue-smoke-test generate orders_test

# 3.1 TAP - Replay cassette → test_runtime/ → compare (CI uses bare `run` = all cases)
# 3.2 ETL - Per datetime folder: etl.py → datetime/test_runtime/ → compare to datetime/expected_output/
hotglue-smoke-test run
hotglue-smoke-test run orders_test
```

**Tap:** `record` scrubs by default after the live HTTP capture (cassette response bodies + connector `record-vcr.py` rules). Scrubbed PII is intentionally marked so reviewers/scanners can tell it from live data: `Fake-` on names/addresses, `555-01xx` phones, `fake.*@example.com` emails, `203.0.113.x` IPs (untyped strings use `-Fallback-scrubbed-…`).

**Target:** `record` scrubs the case `data.singer` **in place** together with the cassette, using one shared value map, so the scrubbed input still produces the recorded requests on replay. Commit the scrubbed `data.singer` with the cassette.

**ETL:** each `record` creates `<case>/<YYYYMMDDTHHMMSS>/fixtures/` (**input**, folder name is **UTC**). First run seeds `fixtures/snapshots/`; later runs get snapshots from the previous job's `expected_output/snapshots` (or runtime) at `generate`/`run`. `generate` fills only datetime folders missing `expected_output/` unless `--force`. Fakes are hash-seeded (same obvious markers as tap scrub). `PRESERVE_*` keep enum/filter literals real.

Auto-detect: `record-etl.py` → ETL; elif repo name `target-*` → target; else tap. Validation is CLI `_preflight_cases` (artifacts helpers); `--force` wipes run in `_prepare_case`. Add `--force` on `record`/`generate` to overwrite.

### `--force` semantics

| Command | Without `--force` | With `--force` |
|---------|-------------------|----------------|
| `record` (tap) | Fails if `fixtures/vcr.yaml` exists | Wipes `fixtures/`, `expected_output/`, `test_runtime/`, then live-records + scrub |
| `record` (ETL) | Always appends a new datetime folder with `fixtures/` | Wipes all datetime dirs (keeps `test-config.json`), then appends a fresh one |
| `generate` (tap) | Fails if data.singer/state.json output exists | Wipes `expected_output/` and `test_runtime/`, then regenerates from cassette |
| `generate` (ETL) | Generates only datetime folders missing `expected_output/`; errors if all exist | Wipes each datetime's `expected_output/` + `test_runtime/`, then regenerates all |

`run` never mutates committed artifacts (`fixtures/` / `expected_output/`).

### Typical workflow

**Tap:**

```bash
hotglue-smoke-test record orders_test            # live → fixtures/vcr.yaml → scrub
hotglue-smoke-test generate orders_test          # replay → expected_output/
hotglue-smoke-test run orders_test               # replay → test_runtime/ → diff

hotglue-smoke-test record --force orders_test    # full re-record + scrub (start over)
hotglue-smoke-test generate orders_test
hotglue-smoke-test run orders_test

hotglue-smoke-test generate --force orders_test  # refresh data.singer/state.json after connector change (HTTP unchanged)
hotglue-smoke-test run orders_test
```

For ETL, another `record` appends a job; `generate` fills only the new
`expected_output/`. Use `generate --force <case>` after changing `etl.py` to refresh
all expected outputs, then run the case again.

Connector `__smoke-tests__/record-vcr.py`:

```python
from hotglue_smoke_test.vcr.tap import VCRTapTestRunner
```

Override `scrub_response_body()` for connector-specific response-body rules, or `scrub_uri()` for PII in request paths or query strings. Default base scrub redacts OAuth token keys and other response values.

ETL `__smoke-tests__/record-etl.py` subclasses `ETLSmokeRunner` (mirror of
`VCRTapTestRunner`): override `should_scrub_key` when JSON dict keys must be scrubbed;
override `split_composite_value` for composite values (return an odd-length
``[part, sep, part, ...]`` list, e.g. `re.split(r"(--)", value)`; even indices are
scrubbed, odd ones stay as separators); override `SKIP_SCRUB_NAMES` to scrub or keep
schema files (`catalog.json` / `selectedTables.json` / …); override `after_etl` when needed; end with
`YourClass.main()` under `if __name__ == "__main__"`.
One Runner serves many `*_test` case folders. Optional per-case `flow`, `job_type`,
and `tenant` overrides go in `<case>/test-config.json`; unset values are omitted so
`etl.py` keeps its defaults. CLI shells out to `record-etl.py` with
`SMOKE_TEST_MODE` the same way it shells out to `record-vcr.py`.

ETL `run` compare (per UTC datetime): Singer `data.singer` when present, JSON etl-output,
CSV etl-output, then snapshots (CSV via legacy folder compare; parquet/json pairwise).

Self-check: `python -m hotglue_smoke_test.self_check`
