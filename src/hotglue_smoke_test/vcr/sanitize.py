"""VCR cassette sanitization helpers (runs after record by default)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import yaml

# Credential keys scrubbed at any depth in response JSON; extend when new auth patterns appear.
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


def scrub_tokens_in_json(data: Any) -> Any:
    """Replace credential values (any depth) with the key name."""
    return scrub_json_tree(
        data,
        scrub_keys=set(TOKEN_KEYS),
        preserve_keys=set(),
        replace_fn=lambda key, _value: key,
    )


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


# Typed PII generators by normalized field name (casing / snake_case / dotted aliases).
# Dotted keys use the last segment (BILLTO.FIRSTNAME → firstname). Bare "state" omitted.

_EMAIL_FIELDS = {"email", "billemail", "ticketmatchingemails"}
_PHONE_FIELDS = {
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
_IP_FIELDS = {"clientip", "ipaddress", "ip"}
_FIRST_NAME_FIELDS = {"firstname"}
_LAST_NAME_FIELDS = {"lastname"}
_PERSON_NAME_FIELDS = {"name", "displayname", "fullname", "addressee"}
_COMPANY_NAME_FIELDS = {"companyname"}
_STREET_FIELDS = {
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
_CITY_FIELDS = {"city", "shiptocity", "selltocity", "mailingcity", "billingcity", "shippingcity"}
_POSTAL_FIELDS = {
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
_REGION_FIELDS = {
    "province",
    "provincecode",
    "shiptostate",
    "selltostate",
    "mailingstate",
    "billingstate",
    "shippingstate",
}
_LAT_FIELDS = {"latitude", "lat"}
_LON_FIELDS = {"longitude", "lng", "lon"}
_BIRTHDATE_FIELDS = {"birthdate", "dateofbirth", "dob"}
# Fall through to uuid4 when scrubbed: account/payment ids, SSN/tax ids, etc.


def make_faker_replace_fn(faker, cache: dict) -> Callable[[str, Any], Any]:
    """Deterministic Faker replacement: same real value → same fake (any field)."""

    def replace(key: str, value: Any) -> Any:
        if value is None:
            return None
        try:
            cache_key = tuple(value) if isinstance(value, list) else value
            hash(cache_key)
        except TypeError:
            # list/dict values aren't hashable — skip cache (still scrub).
            cache_key = None
        if cache_key is not None and cache_key in cache:
            return cache[cache_key]

        field = key.split(".")[-1].replace("_", "").lower()

        if field in _EMAIL_FIELDS:
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

        if cache_key is not None:
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
            stored = body["string"]
            # Byte length of the body as stored (UTF-8 for str; raw len for bytes).
            nbytes = len(stored) if isinstance(stored, bytes) else len(stored.encode("utf-8"))
            headers["Content-Length"] = [str(nbytes)]

    write_cassette(path, cassette)

def sanitize_config_credentials(case_dir: Path | str) -> None:
    """Replace live credential values in case config.json with a safe placeholder."""
    config_path = Path(case_dir) / "config.json"
    if not config_path.is_file():
        return
    config = json.loads(config_path.read_text())
    live = {}
    for key in TOKEN_KEYS:
        value = config.get(key)
        if isinstance(value, str) and value:
            live[key] = value
            config[key] = value[:3] + "***"
        elif isinstance(value, int):
            live[key] = value
            config[key] = str(value)[:3] + "***"
    if live:
        # so rotated tokens from the live record are not lost when the file is scrubbed
        print("-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-")
        print("Live credentials: ")
        print(json.dumps(live, indent=2))
        print("These credentials were scrubbed.")
        print("Save it to reuse on another test record")
        print("-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-")
    config_path.write_text(json.dumps(config, indent=4) + "\n")

