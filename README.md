# hotglue-smoke-test

Smoke-test harness for hotglue taps and targets with **colocated** `__tests__/` fixtures in connector repos.

Provides VCR record/replay runners, Singer/state output comparison, and a CLI.

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
pip install "hotglue-smoke-test @ git+https://github.com/hotgluexyz/hotglue-smoke-test.git@v0.1.0"
```

## Usage

```bash
pip install .
pip install "hotglue-smoke-test @ git+https://github.com/hotgluexyz/hotglue-smoke-test.git@v0.1.0"

hotglue-smoke-test run shopify-v2 '*' --tap-directory .
hotglue-smoke-test run shopify-v2 variants_stream_test --tap-directory .
```

Connector `__tests__/record-vcr.py`:

```python
from hotglue_smoke_test.vcr.tap import VCRTapTestRunner
```
