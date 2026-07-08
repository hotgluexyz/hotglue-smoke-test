# hotglue-smoke-test

Smoke-test harness for hotglue taps and targets with **colocated** `__tests__/` fixtures in connector repos.

Three explicit phases: record HTTP → generate data.singer/state.json output → run (replay + compare).

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
# 1. Record VCR cassette (live API; discards Singer output)
hotglue-smoke-test record  shopify-v2 orders_test --tap-directory .

# 2. Replay cassette → write expected_output/
hotglue-smoke-test generate shopify-v2 orders_test --tap-directory .

# 3. Replay cassette → test_runtime/ → compare (CI uses this)
hotglue-smoke-test run     shopify-v2 '*'           --tap-directory .
hotglue-smoke-test run     shopify-v2 orders_test   --tap-directory .
```

Add `--target` for target repos. Add `--force` on `record` or `generate` to overwrite existing artifacts.

### `--force` semantics

| Command | Without `--force` | With `--force` |
|---------|-------------------|----------------|
| `record` | Fails if `fixtures/vcr.yaml` exists | Wipes `fixtures/`, `expected_output/`, `test_runtime/`, then live-records |
| `generate` | Fails if data.singer/state.json output exists | Wipes `expected_output/`, `test_runtime/`, then regenerates from cassette |

`run` never mutates committed artifacts.

### Typical workflow

```bash
record  orders_test           # live → fixtures/vcr.yaml
generate orders_test           # replay → expected_output/
run orders_test                # replay → test_runtime/ → diff

record  --force orders_test    # full re-record (start over)
generate orders_test
run orders_test

generate --force orders_test   # refresh data.singe/state.json after connector change (HTTP unchanged)
run orders_test
```

## Breaking changes (v0.2)

- `run` no longer auto-records when the cassette is missing.
- Recording and generating are separate steps; `record` does not produce `expected_output/`.

Connector `__tests__/record-vcr.py`:

```python
from hotglue_smoke_test.vcr.tap import VCRTapTestRunner
```

Self-check: `python -m hotglue_smoke_test.self_check`
