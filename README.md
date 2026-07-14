# hotglue-smoke-test

Smoke-test harness for hotglue taps and targets with **colocated** `__tests__/` fixtures in connector repos.

Four phases: record HTTP → (optional) sanitize cassette → generate data.singer/state.json → run (replay + compare).

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
hotglue-smoke-test record   shopify-v2 orders_test --tap-directory .

# 2. Optional: scrub secrets/PII from fixtures/vcr.yaml (+ connector-specific rules)
hotglue-smoke-test sanitize shopify-v2 orders_test --tap-directory .

# 3. Replay cassette → write expected_output/
hotglue-smoke-test generate shopify-v2 orders_test --tap-directory .

# 4. Replay cassette → test_runtime/ → compare (CI uses this)
hotglue-smoke-test run      shopify-v2 '*'           --tap-directory .
hotglue-smoke-test run      shopify-v2 orders_test   --tap-directory .
```

`sanitize` is optional for local testing. Use it before committing cassettes so response bodies (and connector `record-vcr.py` rules) do not leak PII. `generate` / `run` work without it.

Add `--target` for target repos. Add `--force` on `record` or `generate` to overwrite existing artifacts.

### `--force` semantics

| Command | Without `--force` | With `--force` |
|---------|-------------------|----------------|
| `record` | Fails if `fixtures/vcr.yaml` exists | Wipes `fixtures/`, `expected_output/`, `test_runtime/`, then live-records |
| `sanitize` | Requires cassette; rewrites in place | Same (re-scrub in place) |
| `generate` | Fails if data.singer/state.json output exists | Wipes `expected_output/`, `test_runtime/`, then regenerates from cassette |

`run` never mutates committed artifacts.

### Typical workflow

```bash
record  orders_test           # live → fixtures/vcr.yaml
sanitize orders_test          # scrub cassette (optional but recommended before commit)
generate orders_test           # replay → expected_output/
run orders_test                # replay → test_runtime/ → diff

record  --force orders_test    # full re-record (start over)
sanitize orders_test
generate orders_test
run orders_test

generate --force orders_test   # refresh data.singer/state.json after connector change (HTTP unchanged)
run orders_test
```

## Breaking changes (v0.2)

- `run` no longer auto-records when the cassette is missing.
- Recording and generating are separate steps; `record` does not produce `expected_output/`.

Connector `__tests__/record-vcr.py`:

```python
from hotglue_smoke_test.vcr.tap import VCRTapTestRunner
```

Override `sanitize_cassette()` for connector-specific PII rules. Default base scrub only redacts OAuth token keys in response JSON.

Self-check: `python -m hotglue_smoke_test.self_check`
