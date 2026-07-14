"""VCR cassette sanitization helpers (runs after record by default)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import yaml

# Top-level response-body credential keys; extend when new auth patterns appear.
TOKEN_KEYS = (
    "access_token",
    "refresh_token",
    "api_key",
    "api_secret",
    "auth_token",
    "client_id",
    "client_secret",
    "password",
    "token",
    "sender_password",
    "user_password",
    "ns_consumer_key",
    "ns_consumer_secret",
    "ns_token_key",
    "ns_token_secret",
)


def load_cassette(path: str | Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def write_cassette(path: str | Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def scrub_tokens_in_json(data: dict) -> dict:
    """Replace top-level credential values with the key name."""
    for key in TOKEN_KEYS:
        if key in data:
            data[key] = key
    return data


def scrub_json_tree(
    obj: Any,
    *,
    scrub_keys: set[str],
    preserve_keys: set[str],
    replace_fn: Callable[[str, Any], Any],
) -> Any:
    """Recursively scrub JSON values by key name. preserve_keys win over scrub_keys."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in preserve_keys:
                out[key] = value
            elif key in scrub_keys:
                out[key] = replace_fn(key, value)
            else:
                out[key] = scrub_json_tree(
                    value,
                    scrub_keys=scrub_keys,
                    preserve_keys=preserve_keys,
                    replace_fn=replace_fn,
                )
        return out
    if isinstance(obj, list):
        return [
            scrub_json_tree(
                item,
                scrub_keys=scrub_keys,
                preserve_keys=preserve_keys,
                replace_fn=replace_fn,
            )
            for item in obj
        ]
    return obj


def _norm_field(key: str) -> str:
    return key.replace("_", "").lower()


# Typed PII generators by normalized field name (casing / snake_case aliases from connectors).
# Bare "state" omitted — too often workflow status, not geography.
_EMAIL_FIELDS = frozenset({"email", "billemail", "ticketmatchingemails"})
_PHONE_FIELDS = frozenset(
    {
        "phone",
        "phonenumber",
        "telephone",
        "mobile",
        "mobilephone",
        "fax",
        "homephone",
        "otherphone",
        "processedphone",
        "processedmobile",
    }
)
_IP_FIELDS = frozenset({"clientip", "ipaddress", "ip"})
_FIRST_NAME_FIELDS = frozenset({"firstname"})
_LAST_NAME_FIELDS = frozenset({"lastname"})
_PERSON_NAME_FIELDS = frozenset({"name", "displayname", "fullname", "addressee"})
_COMPANY_NAME_FIELDS = frozenset({"companyname"})
_STREET_FIELDS = frozenset(
    {
        "address",
        "address1",
        "address2",
        "addressline1",
        "addressline2",
        "street",
        "streetaddress",
        "freeformaddress",
        "shiptoaddressline1",
        "shiptoaddressline2",
        "selltoaddressline1",
        "selltoaddressline2",
        "mailingstreet",
        "billingstreet",
        "shippingstreet",
    }
)
_CITY_FIELDS = frozenset(
    {"city", "shiptocity", "selltocity", "mailingcity", "billingcity", "shippingcity"}
)
_POSTAL_FIELDS = frozenset(
    {
        "zip",
        "zipcode",
        "postalcode",
        "postcode",
        "shiptopostcode",
        "selltopostcode",
        "mailingpostalcode",
        "billingpostalcode",
        "shippingpostalcode",
    }
)
_REGION_FIELDS = frozenset(
    {
        "province",
        "provincecode",
        "shiptostate",
        "selltostate",
        "mailingstate",
        "billingstate",
        "shippingstate",
    }
)
_LAT_FIELDS = frozenset({"latitude", "lat"})
_LON_FIELDS = frozenset({"longitude", "lng", "lon"})
_BIRTHDATE_FIELDS = frozenset({"birthdate", "dateofbirth", "dob"})
# Fall through to uuid4 when scrubbed: account/payment ids, SSN/tax ids, etc.


def make_faker_replace_fn(faker, cache: dict) -> Callable[[str, Any], Any]:
    """Deterministic Faker replacement keyed by (field, original value)."""

    def replace(key: str, value: Any) -> Any:
        if value is None:
            return None
        cache_key = (key, value if not isinstance(value, list) else tuple(value))
        if cache_key in cache:
            return cache[cache_key]

        field = _norm_field(key)

        if field == "formatted":
            fake = (
                [faker.street_address() if isinstance(i, str) else i for i in value]
                if isinstance(value, list)
                else faker.street_address()
            )
        elif field == "formattedarea":
            fake = f"{faker.city()}, {faker.state_abbr()}, {faker.country()}"
        elif field in _EMAIL_FIELDS:
            fake = faker.email()
        elif field in _PHONE_FIELDS:
            fake = faker.phone_number()
        elif field in _IP_FIELDS:
            fake = faker.ipv4()
        elif field in _FIRST_NAME_FIELDS:
            fake = faker.first_name()
        elif field in _LAST_NAME_FIELDS:
            fake = faker.last_name()
        elif field in _COMPANY_NAME_FIELDS:
            fake = faker.company()
        elif field in _PERSON_NAME_FIELDS:
            fake = faker.name()
        elif field in _STREET_FIELDS:
            fake = faker.street_address()
        elif field in _CITY_FIELDS:
            fake = faker.city()
        elif field in _POSTAL_FIELDS:
            fake = faker.postcode()
        elif field in _REGION_FIELDS:
            fake = faker.state()
        elif field in _LAT_FIELDS:
            fake = float(faker.latitude())
        elif field in _LON_FIELDS:
            fake = float(faker.longitude())
        elif field in _BIRTHDATE_FIELDS:
            fake = faker.date()
        elif isinstance(value, str):
            fake = faker.uuid4()
        elif isinstance(value, list):
            fake = [replace(key, item) if isinstance(item, str) else item for item in value]
        else:
            fake = value

        cache[cache_key] = fake
        return fake

    return replace


def scrub_response_json(
    body: str,
    scrub_keys: set[str],
    preserve_keys: set[str],
    faker,
    cache: dict,
) -> str:
    """Parse response JSON, scrub tokens + keyed fields, re-serialize."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body

    if isinstance(data, dict):
        data = scrub_tokens_in_json(data)

    data = scrub_json_tree(
        data,
        scrub_keys=scrub_keys,
        preserve_keys=preserve_keys,
        replace_fn=make_faker_replace_fn(faker, cache),
    )
    return json.dumps(data)


def sanitize_cassette_file(
    path: str | Path,
    *,
    scrub_response: Callable[[str], str] | None = None,
    scrub_uri: Callable[[str], str] | None = None,
) -> None:
    """Load cassette, scrub response bodies (and optional URIs), write back in place."""
    path = Path(path)
    cassette = load_cassette(path)
    interactions = cassette.get("interactions") or []

    for interaction in interactions:
        request = interaction.get("request") or {}
        if scrub_uri and "uri" in request:
            request["uri"] = scrub_uri(request["uri"])

        if scrub_response is None:
            continue

        response = interaction.get("response") or {}
        body = response.get("body") or {}
        raw = body.get("string")
        if raw is None:
            continue
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
            scrubbed = scrub_response(text)
            body["string"] = scrubbed.encode("utf-8")
        else:
            scrubbed = scrub_response(str(raw))
            body["string"] = scrubbed

        headers = response.get("headers") or {}
        if "Content-Length" in headers:
            headers["Content-Length"] = [str(len(scrubbed))]

    write_cassette(path, cassette)
