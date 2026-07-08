"""Assert-based self-check for smoke test artifact helpers. Run: python -m hotglue_smoke_test.self_check"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hotglue_smoke_test.artifacts import (
    output_path,
    validate_generate,
    validate_record,
    validate_run,
    wipe_generate_artifacts,
    wipe_record_artifacts,
)


def _assert_raises_system_exit(fn) -> None:
    try:
        fn()
    except SystemExit as exc:
        assert exc.code != 0
        return
    raise AssertionError("expected SystemExit")


def main() -> None:

    with tempfile.TemporaryDirectory() as tmp:
        case = Path(tmp) / "orders_test"
        case.mkdir()
        (case / "fixtures").mkdir()
        (case / "fixtures" / "vcr.yaml").write_text("cassette\n")
        (case / "expected_output").mkdir()
        (case / "expected_output" / "data.singer").write_text("{}\n")

        assert output_path(case, "generate", False) == case / "expected_output" / "data.singer"
        assert output_path(case, "run", False) == case / "test_runtime" / "data.singer"

        _assert_raises_system_exit(lambda: validate_record(case, force=False))

        (case / "config.json").write_text('{"api_key": "API_KEY"}\n')
        _assert_raises_system_exit(lambda: validate_record(case, force=True))

        (case / "config.json").write_text('{"api_key": "shpca_live_token"}\n')
        validate_record(case, force=True)

        _assert_raises_system_exit(lambda: validate_generate(case, False, force=False))
        validate_run(case, False)

        wipe_record_artifacts(case)
        assert not (case / "fixtures").exists()
        assert not (case / "expected_output").exists()

        (case / "fixtures").mkdir()
        (case / "fixtures" / "vcr.yaml").write_text("cassette\n")
        (case / "expected_output").mkdir()
        (case / "expected_output" / "data.singer").write_text("{}\n")
        (case / "test_runtime").mkdir()
        (case / "test_runtime" / "data.singer").write_text("{}\n")

        wipe_generate_artifacts(case)
        assert (case / "fixtures" / "vcr.yaml").is_file()
        assert not (case / "expected_output").exists()
        assert not (case / "test_runtime").exists()

    print("self_check: ok")


if __name__ == "__main__":
    main()
