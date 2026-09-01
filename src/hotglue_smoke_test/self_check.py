"""Assert-based self-check for smoke test artifact helpers. Run: python -m hotglue_smoke_test.self_check"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from faker import Faker
from vcr.request import Request

from hotglue_smoke_test.artifacts import (
    output_path,
    validate_etl_generate,
    validate_etl_record,
    validate_etl_run,
    validate_generate,
    validate_no_scrub_case_name,
    validate_record,
    validate_run,
    wipe_etl_generate_artifacts,
    wipe_etl_record_artifacts,
    wipe_generate_artifacts,
    wipe_record_artifacts,
)
from hotglue_smoke_test.cli import _preflight_cases
from hotglue_smoke_test.vcr.base import VCRBaseTestRunner
from hotglue_smoke_test.vcr.sanitize import (
    load_cassette,
    make_faker_replace_fn,
    sanitize_cassette_file,
    sanitize_config_credentials,
    scrub_response_body,
    write_cassette,
)
from hotglue_smoke_test.compare.csv_output_comparator import compare_csv_folder
from hotglue_smoke_test.compare.json_output_comparator import JsonOutputComparator
from hotglue_smoke_test.compare.snapshot_output_comparator import compare_snapshots
from hotglue_smoke_test.compare.test_configurer import TestConfigurer
from hotglue_smoke_test.etl.base import ETLSmokeRunner, _snapshot_flow_hint
from hotglue_smoke_test.etl.scrub import make_deterministic_replace_fn, scrub_file, scrub_series


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
            return re.split(r"(--)", value)
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
    # Mixed separators: even indices scrub, odd indices stay literal.
    mixed = make_deterministic_replace_fn(
        preserve_values=preserve_values,
        cache={},
        split_composite=lambda v: re.split(r"([-_])", v) if re.search(r"[-_]", v) else None,
    )
    scrubbed_mixed = mixed("x", "entityabc123-USD_PENDING")
    assert scrubbed_mixed.endswith("-USD_PENDING")
    assert not scrubbed_mixed.startswith("entityabc123")
    # Even-length list means the hook lost a separator; fail loudly.
    even = make_deterministic_replace_fn(
        preserve_values=preserve_values, cache={}, split_composite=lambda v: v.split("|")
    )
    try:
        even("x", "a|b")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for even-length split_composite")
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
    # Numerics scrub by default; keep via PRESERVE_* when ETL needs them.
    assert plain("amount", 12.5) != 12.5
    assert isinstance(plain("amount", 12.5), float)
    assert plain("paid", 100) != 100
    assert isinstance(plain("paid", 100), int)
    # CSV numeric strings stay str but scrub to parseable digit strings.
    amount_str = plain("amount", "1000")
    assert isinstance(amount_str, str)
    assert amount_str != "1000"
    assert not amount_str.startswith("-Fallback-scrubbed-")
    int(amount_str)
    rate_str = plain("rate", "12.5")
    assert isinstance(rate_str, str)
    assert rate_str != "12.5"
    assert not rate_str.startswith("-Fallback-scrubbed-")
    float(rate_str)
    assert plain("label", "Acme Corp").startswith("-Fallback-scrubbed-")
    assert plain("empty", "") == ""

    # Parquet list/struct cells are unhashable; preserve check must not crash.
    import numpy as np
    import pandas as pd

    assert plain("tags", np.array(["secret_a", "PENDING"])) is not None
    scrubbed = scrub_series(
        pd.Series([{"email": "real@example.com"}, np.array(["secret_a", "PENDING"])]),
        "payload",
        replace_fn=plain,
        preserve_columns=set(),
        preserve_values=preserve_values,
        preserve_keys=set(),
        token_keys=set(),
        should_scrub_key=lambda _k: False,
    )
    assert scrubbed.iloc[0]["email"].startswith("fake.") and scrubbed.iloc[0]["email"].endswith(
        "@example.com"
    )
    assert scrubbed.iloc[1][1] == "PENDING"
    assert scrubbed.iloc[1][0] != "secret_a"

    # target-csv stores nested cells as Python repr; scrub nested PII via PRESERVE_KEYS.
    cell = (
        "[{'id': 64935, 'start_date': None, "
        "'subcontractor': {'id': 412017, 'contact_email': 'lwitt@prmech.com'}}]"
    )
    out = scrub_series(
        pd.Series([cell]),
        "insurance_requests",
        replace_fn=plain,
        preserve_columns=set(),
        preserve_values=preserve_values,
        preserve_keys={"id", "start_date"},
        token_keys=set(),
        should_scrub_key=lambda _k: False,
    ).iloc[0]
    parsed = json.loads(out)
    assert parsed[0]["id"] == 64935
    assert parsed[0]["subcontractor"]["id"] == 412017
    assert parsed[0]["start_date"] is None
    assert parsed[0]["subcontractor"]["contact_email"] != "lwitt@prmech.com"

    # Invalid bracket-prefixed string falls back to scalar scrub (no crash).
    invalid = "[not a valid literal"
    invalid_out = scrub_series(
        pd.Series([invalid]),
        "notes",
        replace_fn=plain,
        preserve_columns=set(),
        preserve_values=preserve_values,
        preserve_keys=set(),
        token_keys=set(),
        should_scrub_key=lambda _k: False,
    ).iloc[0]
    assert invalid_out != invalid
    assert invalid_out.startswith("-Fallback-scrubbed-")

    # CSV: do not turn empty/NA into NaN or coerce "001" to int before scrub.
    csv_path = Path(tempfile.mkdtemp()) / "sample.csv"
    csv_path.write_text("code,empty,marker,secret\n001,,NA,live-token\n")
    scrub_file(
        csv_path,
        replace_fn=plain,
        preserve_columns={"empty", "marker"},
        preserve_values=preserve_values,
        preserve_keys=set(),
        token_keys=set(),
        should_scrub_key=lambda _k: False,
    )
    cells = csv_path.read_text().strip().splitlines()[1].split(",")
    assert cells[0] != "001"
    assert cells[1] == ""
    assert cells[2] == "NA"
    assert cells[3] != "live-token"


def _check_etl_compare_noops(tmp: Path) -> None:
    """JSON/CSV compare no-op when etl-output has no json/csv (Singer-only cases)."""
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = TestConfigurer.get_test_config(str(tmp))
    assert "dtypes_config" in cfg
    expected = tmp / "expected"
    actual = tmp / "actual"
    expected.mkdir()
    actual.mkdir()
    (expected / "data.singer").write_text("{}\n")
    (actual / "data.singer").write_text("{}\n")
    JsonOutputComparator("noop", str(expected), str(actual), cfg).compare()
    compare_csv_folder("noop", str(actual), str(expected), {"test_config": cfg})
    compare_snapshots(tmp / "missing_snaps", tmp / "missing_snaps", label="noop", test_config=cfg)


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
        FILTER_HEADERS = [
            *VCRBaseTestRunner.FILTER_HEADERS,
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

    # vcr_use_cassette wires before_record_response; append exercises the hook.
    cassette_path = tmp / "fixtures" / "vcr.yaml"
    cassette_path.parent.mkdir(parents=True, exist_ok=True)
    with runner.vcr_use_cassette([]) as cassette:
        cassette.append(
            Request(method="GET", uri="https://example.com/", body="", headers={}),
            {
                "headers": {
                    "Set-Cookie": ["session=abc"],
                    "Content-Type": ["application/json"],
                }
            },
        )
        assert "Set-Cookie" not in cassette.responses[0]["headers"]
        assert cassette.responses[0]["headers"]["Content-Type"] == ["application/json"]


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
    assert scrubbed["email"].startswith("fake.") and scrubbed["email"].endswith("@example.com")
    assert scrubbed["Email"].startswith("fake.") and scrubbed["Email"].endswith("@example.com")
    assert scrubbed["first_name"].startswith("Fake-") and scrubbed["first_name"] != "Fake-Ada"
    assert scrubbed["nested"]["email"] == scrubbed["email"]
    assert scrubbed["nested"]["phone"].startswith("555-01")
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
    assert dotted_data["BILLTO.FIRSTNAME"].startswith("Fake-")
    assert dotted_data["BILLTO.FIRSTNAME"] != "Fake-Ada"

    # numeric/bool strings must stay coercible (not Fallback)
    Faker.seed(31)
    plain_vcr = make_faker_replace_fn(Faker(), {})
    for sample in (".03", "5.", "12.5"):
        out = plain_vcr("AMOUNT", sample)
        assert isinstance(out, str) and out != sample
        assert not out.startswith("-Fallback-scrubbed-")
        float(out)
    for sample in ("true", "false", "TRUE", "False"):
        out = plain_vcr("billable", sample)
        assert isinstance(out, str)
        assert out in ("true", "false")
    # date / date-time strings stay parseable (not Fallback)
    from dateutil.parser import parse as parse_dt

    for sample, pattern in (
        ("06/24/2026", r"\d{2}/\d{2}/\d{4}"),
        ("06/24/2026 14:57:36", r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}"),
        ("2026-07-15", r"\d{4}-\d{2}-\d{2}"),
        ("2026-07-03 13:00:00", r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"),
        ("2026-08-25T19:32:25+00:00", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00"),
    ):
        out = plain_vcr("WHENCREATED", sample)
        assert isinstance(out, str) and out != sample
        assert not out.startswith("-Fallback-scrubbed-")
        assert re.fullmatch(pattern, out), (sample, out)
        parse_dt(out)

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
    assert arr[0]["name"].startswith("Fake-") and arr[0]["name"] != "Fake-Ada"

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

        validate_no_scrub_case_name("unsanitized_internal_server_error_retry_test")
        validate_no_scrub_case_name("unsanitized_read_test")
        _assert_raises_system_exit(
            lambda: validate_no_scrub_case_name("orders_test")
        )
        _assert_raises_system_exit(
            lambda: validate_no_scrub_case_name("read_test")
        )
        _assert_raises_system_exit(
            lambda: _preflight_cases(
                "record",
                ["orders_test"],
                Path(tmp) / "tap-demo" / "__smoke-tests__",
                Path(tmp) / "tap-demo",
                False,
                False,
                False,
                no_scrub=True,
            )
        )

        tap_root = Path(tmp) / "tap-demo"
        tap_smoke = tap_root / "__smoke-tests__"
        tap_case = tap_smoke / "orders_test"
        tap_case.mkdir(parents=True)
        _assert_raises_system_exit(
            lambda: _preflight_cases(
                "run", ["orders_test"], tap_smoke, tap_root, False, False, False
            )
        )
        (tap_case / "fixtures").mkdir()
        (tap_case / "fixtures" / "vcr.yaml").write_text("cassette\n")
        _assert_raises_system_exit(
            lambda: _preflight_cases(
                "run", ["orders_test"], tap_smoke, tap_root, False, False, False
            )
        )
        (tap_case / "expected_output").mkdir()
        (tap_case / "expected_output" / "data.singer").write_text("{}\n")
        _preflight_cases(
            "run", ["orders_test"], tap_smoke, tap_root, False, False, False
        )

        etl_root = Path(tmp) / "etl-demo"
        etl_root.mkdir()
        (etl_root / "__smoke-tests__").mkdir()
        _assert_raises_system_exit(lambda: validate_etl_record(etl_root))
        _assert_raises_system_exit(
            lambda: _preflight_cases(
                "record",
                ["read_test"],
                etl_root / "__smoke-tests__",
                etl_root,
                False,
                True,
                False,
            )
        )
        (etl_root / "sync-output").mkdir()
        validate_etl_record(etl_root)
        _preflight_cases(
            "record",
            ["read_test"],
            etl_root / "__smoke-tests__",
            etl_root,
            False,
            True,
            False,
        )
        etl_case_dir = etl_root / "__smoke-tests__" / "read_test"
        etl_case_dir.mkdir()
        day = etl_case_dir / "20260803T194829"
        day.mkdir()
        (day / "fixtures").mkdir()
        _assert_raises_system_exit(
            lambda: _preflight_cases(
                "run",
                ["read_test"],
                etl_root / "__smoke-tests__",
                etl_root,
                False,
                True,
                False,
            )
        )

        etl_case = Path(tmp) / "bank_transactions_test"
        etl_case.mkdir()
        (etl_case / "test-config.json").write_text('{"flow": "x"}\n')
        etl_runner = ETLSmokeRunner("bank_transactions_test", Path(tmp))
        assert etl_runner._case_env() == {"FLOW": "x"}
        (etl_case / "test-config.json").unlink()
        assert etl_runner._case_env() == {}
        snapshots = etl_case / "snapshots"
        snapshots.mkdir()
        (snapshots / "contacts_Alv3Avor0.snapshot.csv").touch()
        assert _snapshot_flow_hint(snapshots) == "contacts_Alv3Avor0.snapshot.csv"
        (etl_case / "test-config.json").write_text('{"flow": "x"}\n')
        day1 = etl_case / "20260701T120000"
        day1.mkdir()
        (day1 / "fixtures").mkdir()
        (day1 / "expected_output" / "etl-output").mkdir(parents=True)
        (day1 / "test_runtime").mkdir()
        _assert_raises_system_exit(lambda: validate_etl_generate(etl_case, force=False))
        validate_etl_run(etl_case)
        shutil.rmtree(day1 / "fixtures")
        _assert_raises_system_exit(lambda: validate_etl_run(etl_case))
        _assert_raises_system_exit(lambda: validate_etl_generate(etl_case, force=True))
        (day1 / "fixtures").mkdir()
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

        mapping_root = Path(tmp) / "mapping-etl"
        mapping_fixtures = mapping_root / "__smoke-tests__" / "read_test" / "fixtures"
        mapping_fixtures.mkdir(parents=True)
        (mapping_root / "mapping.json").write_text('{"source": true}\n')
        (mapping_fixtures / "mapping.json").write_text('{"fixture": true}\n')
        mapping_runtime = mapping_root / "test_runtime"
        ETLSmokeRunner(
            "read_test", mapping_root / "__smoke-tests__"
        )._prepare_runtime_from_fixtures(mapping_fixtures, mapping_runtime)
        assert (mapping_runtime / "mapping.json").read_text() == '{"source": true}\n'

        _check_sanitize_round_trip(Path(tmp) / "sanitize_check")
        _check_filter_response_headers(Path(tmp) / "filter_response_headers")
        _check_etl_deterministic_scrub()
        _check_etl_compare_noops(Path(tmp) / "etl_compare_noop")

        def _exit(code):
            raise SystemExit(code)

        _swallow_success_system_exit(lambda: None)
        _swallow_success_system_exit(lambda: _exit(0))
        _swallow_success_system_exit(lambda: _exit(None))
        _assert_raises_system_exit(lambda: _swallow_success_system_exit(lambda: _exit(1)))

    print("self_check: ok")


if __name__ == "__main__":
    main()
