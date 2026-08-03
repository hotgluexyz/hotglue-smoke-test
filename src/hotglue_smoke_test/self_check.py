"""Assert-based self-check for smoke test artifact helpers. Run: python -m hotglue_smoke_test.self_check"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from faker import Faker

from hotglue_smoke_test.artifacts import (
    output_path,
    validate_etl_generate,
    validate_etl_run,
    validate_generate,
    validate_record,
    validate_run,
    wipe_etl_generate_artifacts,
    wipe_etl_record_artifacts,
    wipe_generate_artifacts,
    wipe_record_artifacts,
)
from hotglue_smoke_test.vcr.base import VCRBaseTestRunner
from hotglue_smoke_test.vcr.sanitize import (
    load_cassette,
    sanitize_cassette_file,
    sanitize_config_credentials,
    scrub_response_body,
    write_cassette,
)
from hotglue_smoke_test.etl.scrub import make_deterministic_replace_fn


def _assert_raises_system_exit(fn) -> None:
    try:
        fn()
    except SystemExit as exc:
        assert exc.code != 0
        return
    raise AssertionError("expected SystemExit")


def _check_etl_deterministic_scrub() -> None:
    preserve_values = {"PENDING", "USD"}

    def split_composite(value: str):
        if "--" in value:
            left, right = value.rsplit("--", 1)
            return left, right
        return None

    a = {}
    b = {}
    ra = make_deterministic_replace_fn(
        preserve_values=preserve_values, cache=a, split_composite=split_composite
    )
    rb = make_deterministic_replace_fn(
        preserve_values=preserve_values, cache=b, split_composite=split_composite
    )
    assert ra("id", "entity_abc123") == rb("externalId", "entity_abc123")
    assert ra("status", "PENDING") == "PENDING"
    # With split: each side through replace; PRESERVE_VALUES keeps USD
    assert ra("bank_account_id", "entity_abc123--USD") == (
        f"{ra('id', 'entity_abc123')}--USD"
    )
    assert ra("x", "USD--secret_id") == f"USD--{ra('id', 'secret_id')}"
    # same process cache reuse
    assert a[(str, "entity_abc123")] == ra("InputId", "entity_abc123")
    # without split hook, whole string is one scrub unit
    plain = make_deterministic_replace_fn(preserve_values=preserve_values, cache={})
    assert plain("bank_account_id", "entity_abc123--USD") != "entity_abc123--USD"
    assert not str(plain("bank_account_id", "entity_abc123--USD")).endswith("--USD")
    # CSV stores datetimes as strings — keep ISO values (parquet Timestamps already kept)
    iso = "2026-07-03T13:00:00"
    assert plain("settled_at", iso) == iso
    assert plain("due_date", "2026-07-15") == "2026-07-15"

def _swallow_success_system_exit(fn) -> None:
    """Same contract as VCRBaseTestRunner.run_test around launch()."""
    try:
        fn()
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise


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
            "quantity": 42,
            "enabled": True,
            "nested": {
                "email": "real@example.com",
                "phone": "+15551234",
                "access_token": "nested-secret",
            },
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
    preserve_keys = {"updatedAt"}
    token_keys = set(VCRBaseTestRunner.TOKEN_KEYS)

    sanitize_cassette_file(
        cassette_path,
        scrub_response=lambda b: scrub_response_body(
            b, preserve_keys, faker, cache, token_keys
        ),
    )

    data = load_cassette(cassette_path)
    scrubbed = json.loads(data["interactions"][0]["response"]["body"]["string"])
    assert scrubbed["access_token"] == "sec***"
    assert scrubbed["nested"]["access_token"] == "nes***"
    assert scrubbed["updatedAt"] == "2026-07-07T15:00:00Z"
    assert scrubbed["email"] != "real@example.com"
    assert "@" in scrubbed["Email"] and scrubbed["Email"] != "Alias@Example.com"
    assert scrubbed["first_name"] != "Ada"
    assert scrubbed["nested"]["email"] == scrubbed["email"]
    assert scrubbed["nested"]["phone"] != "+15551234"
    assert scrubbed["quantity"] != 42 and isinstance(scrubbed["quantity"], int)
    assert isinstance(scrubbed["enabled"], bool)
    # hasNextPage-style keys stay real when preserved (tap owns pagination allowlist)
    Faker.seed(7)
    page = json.loads(
        scrub_response_body(
            json.dumps({"hasNextPage": True, "closed": True}),
            {"hasNextPage"},
            Faker(),
            {},
            token_keys,
        )
    )
    assert page["hasNextPage"] is True
    assert isinstance(page["closed"], bool)


    # same seed + empty cache → stable fake for same payload on re-run
    Faker.seed(42)
    again = scrub_response_body(body, preserve_keys, Faker(), {}, token_keys)
    assert json.loads(again) == scrubbed

    # dotted Intacct-style keys use last segment for faker type
    Faker.seed(11)
    dotted = scrub_response_body(
        json.dumps({"BILLTO.FIRSTNAME": "Ada"}),
        set(),
        Faker(),
        {},
        token_keys,
    )
    dotted_data = json.loads(dotted)
    assert dotted_data["BILLTO.FIRSTNAME"] != "Ada"
    assert dotted_data["BILLTO.FIRSTNAME"].isalpha()

    # array-rooted responses must still redact TOKEN_KEYS (not skip / preserve live)
    Faker.seed(13)
    arr = json.loads(
        scrub_response_body(
            json.dumps([{"api_key": "secret-live-key", "name": "Ada"}]),
            set(),
            Faker(),
            {},
            token_keys,
        )
    )
    assert arr[0]["api_key"] == "sec***"
    assert arr[0]["name"] != "Ada"

    try:
        scrub_response_body("<html>nope</html>", set(), Faker(), {}, token_keys)
    except NotImplementedError:
        pass
    else:
        raise AssertionError("expected NotImplementedError for non-JSON body")


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

        (case / "config.json").write_text('{"api_key": "API***"}\n')
        _assert_raises_system_exit(lambda: validate_record(case, force=True))

        (case / "config.json").write_text('{"api_key": "shpca_live_token"}\n')
        validate_record(case, force=True)
        sanitize_config_credentials(case, VCRBaseTestRunner.TOKEN_KEYS)
        assert json.loads((case / "config.json").read_text())["api_key"] == "shp***"

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

        etl_case = Path(tmp) / "bank_transactions_test"
        etl_case.mkdir()
        (etl_case / "test-config.json").write_text('{"flow": "x"}\n')
        day1 = etl_case / "20260701T120000"
        day1.mkdir()
        (day1 / "fixtures").mkdir()
        (day1 / "expected_output" / "etl-output").mkdir(parents=True)
        (day1 / "test_runtime").mkdir()
        _assert_raises_system_exit(lambda: validate_etl_generate(etl_case, force=False))
        validate_etl_run(etl_case)
        wipe_etl_generate_artifacts(etl_case)
        assert (day1 / "fixtures").is_dir()
        assert not (day1 / "expected_output").exists()
        assert not (day1 / "test_runtime").exists()
        _assert_raises_system_exit(lambda: validate_etl_run(etl_case))
        wipe_etl_record_artifacts(etl_case)
        assert not day1.exists()
        assert (etl_case / "test-config.json").is_file()
        _assert_raises_system_exit(lambda: validate_etl_generate(etl_case, force=False))

        _check_sanitize_round_trip(Path(tmp) / "sanitize_check")
        _check_etl_deterministic_scrub()

        def _exit(code):
            raise SystemExit(code)

        _swallow_success_system_exit(lambda: None)
        _swallow_success_system_exit(lambda: _exit(0))
        _swallow_success_system_exit(lambda: _exit(None))
        _assert_raises_system_exit(lambda: _swallow_success_system_exit(lambda: _exit(1)))

    print("self_check: ok")


if __name__ == "__main__":
    main()
