"""Assert-based self-check for smoke test artifact helpers. Run: python -m hotglue_smoke_test.self_check"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from faker import Faker

from hotglue_smoke_test.artifacts import (
    output_path,
    validate_generate,
    validate_record,
    validate_run,
    wipe_generate_artifacts,
    wipe_record_artifacts,
)
from hotglue_smoke_test.vcr.sanitize import (
    load_cassette,
    sanitize_cassette_file,
    sanitize_config_credentials,
    scrub_response_json,
    write_cassette,
)


def _assert_raises_system_exit(fn) -> None:
    try:
        fn()
    except SystemExit as exc:
        assert exc.code != 0
        return
    raise AssertionError("expected SystemExit")


def _check_sanitize_round_trip(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    cassette_path = tmp / "vcr.yaml"
    body = json.dumps(
        {
            "access_token": "secret-token-value",
            "email": "real@example.com",
            "Email": "Alias@Example.com",
            "first_name": "Ada",
            "updatedAt": "2026-07-07T15:00:00Z",
            "nested": {"email": "real@example.com", "phone": "+15551234"},
        }
    )
    write_cassette(
        cassette_path,
        {
            "interactions": [
                {
                    "request": {"uri": "https://example.com/api"},
                    "response": {
                        "body": {"string": body},
                        "headers": {"Content-Length": [str(len(body))]},
                    },
                }
            ]
        },
    )

    faker = Faker()
    Faker.seed(42)
    cache = {}
    scrub_keys = {"email", "Email", "phone", "first_name"}
    preserve_keys = {"updatedAt"}

    sanitize_cassette_file(
        cassette_path,
        scrub_response=lambda b: scrub_response_json(
            b, scrub_keys, preserve_keys, faker, cache
        ),
    )

    data = load_cassette(cassette_path)
    scrubbed = json.loads(data["interactions"][0]["response"]["body"]["string"])
    assert scrubbed["access_token"] == "access_token"
    assert scrubbed["updatedAt"] == "2026-07-07T15:00:00Z"
    assert scrubbed["email"] != "real@example.com"
    assert "@" in scrubbed["Email"] and scrubbed["Email"] != "Alias@Example.com"
    assert scrubbed["first_name"] != "Ada"
    assert scrubbed["nested"]["email"] == scrubbed["email"]
    assert scrubbed["nested"]["phone"] != "+15551234"

    # same seed + cache empty → stable fake for same input on re-run
    Faker.seed(42)
    again = scrub_response_json(
        json.dumps({"email": "real@example.com", "updatedAt": "keep"}),
        scrub_keys,
        preserve_keys,
        Faker(),
        {},
    )
    again_data = json.loads(again)
    assert again_data["email"] == scrubbed["email"]
    assert again_data["updatedAt"] == "keep"


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
        sanitize_config_credentials(case)
        assert json.loads((case / "config.json").read_text())["api_key"] == "API_KEY"

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

        _check_sanitize_round_trip(Path(tmp) / "sanitize_check")

    print("self_check: ok")


if __name__ == "__main__":
    main()
