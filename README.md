# hotglue-smoke-test

Smoke-test harness for hotglue taps, ETLs, and targets (pending) scripts with **colocated** `__smoke-tests__/` fixtures.

**Tap:** record HTTP (then scrub) → generate data.singer/state.json → run (replay + compare).  
**ETL:** record (copy raw fixtures + scrub) → generate (`etl.py` → expected_output) → run (replay + compare). 

Run from the connector repo root or on the script directory (after installing into that venv).

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
### ETL

Mimics successive hotglue jobs: each `record` appends a new UTC datetime folder (`YYYYMMDDTHHMMSS`); `generate`/`run` chain snapshots from the previous job’s post-ETL output.

**`fixtures/` is the input** (scrubbed tap `sync-output`, seed `snapshots`, `mapping.json`, `catalog.json`).

```
script-dir/                  # folder with etl.py
  __smoke-tests__/
    record-etl.py            # FLOW, PRESERVE_COLUMNS/VALUES/KEYS
    <case>_test/
      20260528T120000/       # job 1
        fixtures/            # INPUT (committed)
          sync-output/
          snapshots/         # seed only on first day
          mapping.json
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
# 1.2 ETL - Append YYYYMMDDTHHMMSS/fixtures/ (input: sync-output + seed snapshots), scrub
hotglue-smoke-test record orders_test

# 2.1 TAP - Replay cassette → write expected_output/
# 2.2 ETL - Per UTC datetime: etl.py → datetime/expected_output/; skip folders that already have it unless --force
hotglue-smoke-test generate orders_test

# 3.1 TAP - Replay cassette → test_runtime/ → compare (CI uses bare `run` = all cases)
# 3.2 ETL - Per UTC datetime: etl.py → datetime/test_runtime/ → compare to datetime/expected_output/
hotglue-smoke-test run
hotglue-smoke-test run orders_test
```

**Tap:** `record` scrubs by default after the live HTTP capture (cassette response bodies + connector `record-vcr.py` rules). Use `--no-scrub` only for local debug; do not commit unsanitized cassettes.

**ETL:** each `record` creates `<case>/<YYYYMMDDTHHMMSS>/fixtures/` (**input**, folder name is **UTC**). First run seeds `fixtures/snapshots/`; later runs get snapshots from the previous job's `expected_output/snapshots` (or runtime) at `generate`/`run`. `generate` fills only datetime folders missing `expected_output/` unless `--force`. Fakes are hash-seeded. `PRESERVE_*` keep enum/filter literals real.

Auto-detect: `record-etl.py` → ETL; else repo name `target-*` → target, `tap-*` → tap. Folder validate/wipe lives in `artifacts.py` (CLI `_prepare_case`) for both taps and ETLs — same lifecycle as taps. Add `--force` on `record`/`generate` to overwrite. Override the repo root with `--connector-directory` only when not running from cwd.

### `--force` semantics

| Command | Without `--force` | With `--force` |
|---------|-------------------|----------------|
| `record` (tap) | Fails if `fixtures/vcr.yaml` exists | Wipes `fixtures/`, `expected_output/`, `test_runtime/`, then live-records + scrub |
| `record` (ETL) | Always appends a new UTC datetime folder with `fixtures/` | Wipes all UTC datetime dirs (keeps `test-config.json`), then appends a fresh one |
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

**ETL:**

```bash
hotglue-smoke-test record bank_transactions_test              # append datetime/fixtures/ (input)
hotglue-smoke-test generate bank_transactions_test            # → datetime/expected_output/
hotglue-smoke-test run bank_transactions_test                 # → datetime/test_runtime/ → diff

hotglue-smoke-test record bank_transactions_test              # append another fixtures
hotglue-smoke-test generate bank_transactions_test            # only fills new expected_output/
hotglue-smoke-test run bank_transactions_test

hotglue-smoke-test generate --force bank_transactions_test    # refresh all datetime after etl.py change
hotglue-smoke-test run bank_transactions_test
```

Connector `__smoke-tests__/record-vcr.py`:

```python
from hotglue_smoke_test.vcr.tap import VCRTapTestRunner
```

Override `sanitize_cassette()` for connector-specific PII rules. Default base scrub only redacts OAuth token keys in response JSON.

ETL `__smoke-tests__/record-etl.py` subclasses `ETLSmokeRunner` (mirror of
`VCRTapTestRunner`): override `looks_like_id_key` when ids appear as JSON dict keys;
override `split_composite_value` for ``left--right`` values (each side scrubbed;
`PRESERVE_VALUES` keeps enums); override `after_etl` when needed; end with
`YourClass.main()` under `if __name__ == "__main__"`.
One Runner serves many `*_test` case folders; per-case `flow` / `job_type` / `tenant`
go in `<case>/test-config.json`. CLI shells out to `record-etl.py` with `SMOKE_TEST_MODE`
the same way it shells out to `record-vcr.py`.

Self-check: `python -m hotglue_smoke_test.self_check`
