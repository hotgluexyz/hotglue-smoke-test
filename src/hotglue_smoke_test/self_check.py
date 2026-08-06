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
from hotglue_smoke_test.vcr.base import VCRBaseTestRunner
from hotglue_smoke_test.vcr.sanitize import (
    load_cassette,
    sanitize_cassette_file,
    sanitize_config_credentials,
    scrub_response_body,
    write_cassette,
)


def _assert_raises_system_exit(fn) -> None:
    try:
        fn()
    except SystemExit as exc:
        assert exc.code != 0
        return
    raise AssertionError("expected SystemExit")


def _swallow_success_system_exit(fn) -> None:
    """Same contract as VCRBaseTestRunner.run_test around launch()."""
    try:
        fn()
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise


class _StubVCRRunner(VCRBaseTestRunner):
    """Minimal concrete runner for unit checks (no launch)."""

    required_files = []

    @property
    def output_basename(self) -> str:
        return "data.singer"

    def module(self) -> str:
        return "stub"

    def run_launch(self):
        pass

    def launch(self):
        pass

    def argv(self) -> list[str]:
        return []


def _check_filter_response_headers(tmp: Path) -> None:
    runner = _StubVCRRunner("case_test", str(tmp))

    response = {
        "status": {"code": 200, "message": "OK"},
        "headers": {
            "Set-Cookie": ["session=abc; Path=/"],
            "WWW-Authenticate": ["Basic realm=test"],
            "X-CSRF-Token": ["csrf-value"],
            "Content-Type": ["application/json"],
            "X-Request-Id": ["keep-me"],
        },
        "body": {"string": "{}"},
    }
    out = runner.before_record_response(response)
    headers = out["headers"]
    assert "Set-Cookie" not in headers
    assert "WWW-Authenticate" not in headers
    assert "X-CSRF-Token" not in headers
    assert headers["Content-Type"] == ["application/json"]
    assert headers["X-Request-Id"] == ["keep-me"]
    assert out["body"] == {"string": "{}"}

    # Matching is case-insensitive on both the allowlist and recorded keys.
    mixed = runner.before_record_response(
        {"headers": {"set-cookie": ["a=b"], "x-api-key": ["k"], "Accept": ["*/*"]}}
    )
    assert "set-cookie" not in mixed["headers"]
    assert "x-api-key" not in mixed["headers"]
    assert mixed["headers"]["Accept"] == ["*/*"]

    assert runner.before_record_response({}) == {}
    assert runner.before_record_response({"headers": None}) == {"headers": None}
    assert runner.before_record_response({"headers": {}}) == {"headers": {}}

    class _Extended(_StubVCRRunner):
        FILTER_RESPONSE_HEADERS = [
            *VCRBaseTestRunner.FILTER_RESPONSE_HEADERS,
            "X-Custom-Secret",
        ]

    extended = _Extended("case_test", str(tmp))
    custom = extended.before_record_response(
        {
            "headers": {
                "X-Custom-Secret": ["s3cret"],
                "Set-Cookie": ["session=abc"],
                "Content-Type": ["application/json"],
            }
        }
    )
    assert "X-Custom-Secret" not in custom["headers"]
    assert "Set-Cookie" not in custom["headers"]
    assert custom["headers"]["Content-Type"] == ["application/json"]

    # vcr_use_cassette wires before_record_response onto the cassette.
    cassette_path = tmp / "fixtures" / "vcr.yaml"
    cassette_path.parent.mkdir(parents=True, exist_ok=True)
    with runner.vcr_use_cassette([]) as cassette:
        assert cassette.before_record_response is runner.before_record_response


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

        _check_sanitize_round_trip(Path(tmp) / "sanitize_check")
        _check_filter_response_headers(Path(tmp) / "filter_response_headers")

        def _exit(code):
            raise SystemExit(code)

        _swallow_success_system_exit(lambda: None)
        _swallow_success_system_exit(lambda: _exit(0))
        _swallow_success_system_exit(lambda: _exit(None))
        _assert_raises_system_exit(lambda: _swallow_success_system_exit(lambda: _exit(1)))

    print("self_check: ok")


if __name__ == "__main__":
    main()
