# hotglue-smoke-test

Smoke-test harness for hotglue taps and targets with **colocated** `__tests__/` fixtures in connector repos.

Three phases: record HTTP (then scrub) → generate data.singer/state.json → run (replay + compare).

Run from the connector repo root (after installing into that venv). Connector name for logs is derived from the directory (`tap-shopify-v2` → `shopify-v2`).

## Layout (per connector repo)

```
tap-foo/
  __tests__/
    record-vcr.py
  __tests__/some_stream_test/
    config.json
    catalog-selected.json
    fixtures/vcr.yaml
    expected_output/data.singer
```

## Install

```bash
pip install "hotglue-smoke-test @ git+https://github.com/hotgluexyz/hotglue-smoke-test.git@main"
```

## Commands

```bash
# 1. Record VCR cassette (live API; discards Singer output), then scrub secrets/PII
hotglue-smoke-test record orders_test

# 2. Replay cassette → write expected_output/
hotglue-smoke-test generate orders_test

# 3. Replay cassette → test_runtime/ → compare (CI uses bare `run` = all cases)
hotglue-smoke-test run
hotglue-smoke-test run orders_test
```

`record` scrubs by default after the live HTTP capture (cassette response bodies + connector `record-vcr.py` rules). Use `--no-scrub` only for local debug; do not commit unsanitized cassettes.

Add `--target` for target repos. Add `--force` on `record` or `generate` to overwrite existing artifacts. Override the repo root with `--connector-directory` only when not running from cwd.

### `--force` semantics

| Command | Without `--force` | With `--force` |
|---------|-------------------|----------------|
| `record` | Fails if `fixtures/vcr.yaml` exists | Wipes `fixtures/`, `expected_output/`, `test_runtime/`, then live-records + scrub |
| `generate` | Fails if data.singer/state.json output exists | Wipes `expected_output/`, `test_runtime/`, then regenerates from cassette |

`run` never mutates committed artifacts.

### Typical workflow

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

Connector `__tests__/record-vcr.py`:

```python
from hotglue_smoke_test.vcr.tap import VCRTapTestRunner
```

Override `sanitize_cassette()` for connector-specific PII rules. Default base scrub only redacts OAuth token keys in response JSON.

Self-check: `python -m hotglue_smoke_test.self_check`
