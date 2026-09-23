"""Value-first scrub for target cassettes (Singer input + requests + responses).

Targets feed values from the Singer input into requests, then reuse values from
earlier responses to build later requests (lookup before write). Scrubbing per
field name breaks that: the same value is named differently on each side, is
embedded in strings like ``gid://shopify/Customer/123``, or lives in a URI
instead of a JSON body.

So every surface is walked twice: collect all values first, build one
value -> fake map, then replace that map everywhere. Mapping is by value, never
by field name, and fakes are derived from the original value, so the map is
in-memory only and never has to be committed.

Structurally shaped values (numbers, UUIDs, GIDs, URLs, timestamps) are kept real:
they aren't PII and rewriting them is what usually breaks replay. Anything else the
connector needs real — opaque cursors, tokens it decodes — goes in ``PRESERVE_KEYS``,
same as the tap.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from faker import Faker

from hotglue_smoke_test.vcr.sanitize import (
    is_typed_field,
    load_cassette,
    make_faker_replace_fn,
    scrub_json_tree,
    scrub_tokens_in_json,
    stable_seed,
    write_cassette,
)

# Structural values are recognised by shape, never by field name: a name-based rule
# ("…Ref", "…Id") keeps live PII whenever an API returns an email as a reference.
# Field names only preserve through the connector's explicit PRESERVE_KEYS, as in the tap.
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_NUMERIC_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_TEMPORAL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_BOOLEAN_VALUES = {"true", "false", "null", "none"}
# Output of an earlier scrub (data.singer is rewritten in place, so a re-record reads
# fakes back). Re-faking them would give a new fake per record and churn fixtures.
_ALREADY_FAKE_RE = re.compile(r"^(?:-Fallback-scrubbed-|Fake-|fake\.[^@\s]+@example\.com$)")
_CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")
_OIDC_WELL_KNOWN_RE = re.compile(r"\.well-known/", re.IGNORECASE)
_OAUTH_TOKEN_URI_RE = re.compile(r"/oauth2/v\d+/tokens", re.IGNORECASE)
# Fixed route segments — never substitute even when a mapped value matches exactly.
_STRUCTURAL_URI_PATH_SEGMENTS = frozenset(
    {
        "bearer",
        "batch",
        "company",
        "connect",
        "jwks",
        "oauth2",
        "openid_sandbox_configuration",
        "op",
        "revoke",
        "sandbox-configuration",
        "tokens",
        "userinfo",
        "v1",
        "v2",
        "v3",
        "well-known",
    }
)

# Formats recognisable from the value, for when the field name says nothing useful
# ("primaryContactRef" holding an email). Maps to the generator's field name.
_VALUE_FORMATS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"), "email"),
    (re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$"), "ip"),
)

# Shorter values are too collision-prone to swap inside a larger string, unless
# they are reused across surfaces (then they are references and must stay consistent).
MIN_EMBEDDED_LEN = 5
MIN_REFERENCE_LEN = 2
# Characters that can sit against a mapped value without starting a longer token.
_EMBED_EDGE_RE = r"A-Za-z0-9_"


def _replace_bounded(text: str, value: str, fake: str) -> str:
    """Replace ``value`` only when it is not part of a longer identifier."""
    if value not in text:
        return text
    pattern = re.compile(
        rf"(?<![{_EMBED_EDGE_RE}]){re.escape(value)}(?![{_EMBED_EDGE_RE}])"
    )
    return pattern.sub(lambda _match: fake, text)


def _iter_leaves(obj: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for child_key, value in obj.items():
            name = child_key if isinstance(child_key, str) else str(child_key)
            yield from _iter_leaves(value, name)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_leaves(item, key)
    else:
        yield key, obj


class TargetValueScrubber:
    """Collect values from every surface, then replace them consistently everywhere."""

    def __init__(
        self,
        preserve_keys: set[str] | None = None,
        token_keys: set[str] | None = None,
        preserve_values: set[str] | None = None,
    ):
        self.preserve_keys = set(preserve_keys or ())
        self.token_keys = set(token_keys or ())
        self.preserve_values = set(preserve_values or ())
        self._names: dict[str, set[str]] = defaultdict(set)
        self._counts: Counter[str] = Counter()
        self._map: dict[str, str] = {}
        self._embedded: list[str] = []
        self._input_values: set[str] = set()
        self._embedded_input: list[str] = []

    # -- collect -----------------------------------------------------------

    def collect(
        self,
        obj: Any,
        key: str = "",
        *,
        typed_only: bool = False,
        from_input: bool = False,
    ) -> None:
        """Record scrubbable leaves of a JSON tree (Singer record, response body, …).

        ``typed_only`` is for surfaces the connector builds rather than reads — request
        bodies hold GraphQL documents and SOAP envelopes that must survive intact, so
        only PII-named fields there introduce new values. Anything real in them already
        came from the Singer input or an earlier response.

        ``from_input`` marks Singer input values: data.singer is rewritten, so the
        replayed target sends their fakes, and they must be replaced even under
        PRESERVE_KEYS.
        """
        for leaf_key, value in _iter_leaves(obj, key):
            self._note(leaf_key, value, typed_only=typed_only)
            if from_input and value in self._names:
                self._input_values.add(value)

    def collect_uri(self, uri: str) -> None:
        """Count values the URI reuses; only PII-named query params add new ones.

        Route names (``/customers``) and target-built params (``?limit=50``) come from
        connector code, not from the data — faking them would break replay. A query
        param named like PII (``?email=…``) is the exception worth scrubbing.
        """
        parts = urlsplit(uri)
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            self._note(key, unquote(value), typed_only=True)
        for segment in parts.path.split("/"):
            self._count_known(unquote(segment))

    def _note(self, key: str, value: Any, *, typed_only: bool = False) -> None:
        if not self._is_scrubbable(key, value):
            return
        if typed_only and not is_typed_field(key):
            self._count_known(value)
            return
        self._names[value].add(key)
        self._counts[value] += 1

    def _count_known(self, value: str) -> None:
        """Mark an already-collected value as reused (reference), without adding one."""
        if value in self._names:
            self._counts[value] += 1

    def _is_scrubbable(self, key: str, value: Any) -> bool:
        # Non-strings are structural for replay purposes (ids, amounts, flags).
        if not isinstance(value, str):
            return False
        text = value.strip()
        if len(text) < MIN_REFERENCE_LEN or value in self.preserve_values:
            return False
        if key in self.preserve_keys or key in self.token_keys:
            return False
        if text.lower() in _BOOLEAN_VALUES or _ALREADY_FAKE_RE.match(text):
            return False
        return not (
            _NUMERIC_RE.match(text)
            or _UUID_RE.match(text)
            or _SCHEME_RE.match(text)
            or _TEMPORAL_RE.match(text)
            or _CURRENCY_CODE_RE.match(text)
        )

    # -- build -------------------------------------------------------------

    @property
    def scrubbed_values(self) -> int:
        return len(self._map)

    @property
    def references(self) -> set[str]:
        """Values seen in two or more places — lookup keys that must stay consistent."""
        return {value for value, count in self._counts.items() if count > 1}

    def build(self) -> None:
        """Freeze the value -> fake map. Call once, after every surface is collected."""
        for value in self._names:
            self._map[value] = self._fake(value)
        references = self.references
        # Longest first so a value embedded in another is not partially replaced.
        self._embedded = sorted(
            (
                value
                for value in self._map
                if len(value) >= MIN_EMBEDDED_LEN or value in references
            ),
            key=len,
            reverse=True,
        )
        self._embedded_input = [v for v in self._embedded if v in self._input_values]

    def _fake(self, value: str) -> str:
        """Format-preserving fake, derived from the value so it needs no stored map."""
        names = sorted(self._names[value])
        # Any field name that pins a format wins: an email stays an email even when
        # the response calls it "contactRef". Failing that, read the format off the
        # value, so a name that says nothing still yields an email-shaped fake.
        name = next((n for n in names if is_typed_field(n)), "")
        if not name:
            name = next(
                (field for pattern, field in _VALUE_FORMATS if pattern.match(value)),
                next(iter(names), ""),
            )
        faker = Faker()
        faker.seed_instance(stable_seed(value))
        return str(make_faker_replace_fn(faker, {})(name, value))

    # -- replace -----------------------------------------------------------

    def scrub_json(self, obj: Any) -> Any:
        data = scrub_tokens_in_json(obj, self.token_keys)
        # Token keys stay redacted; other PRESERVE_KEYS still run replace_fn so
        # embedded Singer values inside Query/filters are rewritten.
        return scrub_json_tree(
            data,
            preserve_keys=self.token_keys,
            replace_fn=self._replace_leaf,
        )

    def _replace_leaf(self, key: str, value: Any) -> Any:
        # Replacement is purely by value: a value collected as PII stays scrubbed even
        # when a response hands it back under an id/ref-shaped name. Values that were
        # never collected (ids, cursors, numbers) are not in the map and stay real.
        if not isinstance(value, str):
            return value
        # PRESERVE_KEYS keep the field (QBO Query SQL, bId, …) but still swap Singer
        # values embedded inside, so lookup-before-write replay matches data.singer.
        if key in self.preserve_keys:
            return self.scrub_text(value, input_only=True)
        if value in self._map:
            return self._map[value]
        return self.scrub_text(value)

    def scrub_text(self, text: str, *, input_only: bool = False) -> str:
        """Replace mapped values embedded in a larger string (notes, filters, SQL).

        Match the whole value only. A singer name like ``Advertising`` must not
        rewrite a longer token that merely starts with it (``AdvertisingPromotional``).
        """
        if _SCHEME_RE.match(text.strip()):
            return self.scrub_uri(text.strip())
        values = self._embedded_input if input_only else self._embedded
        for value in values:
            text = _replace_bounded(text, value, self._map[value])
        return text

    def scrub_uri(self, uri: str) -> str:
        """Scrub query values; replace path segments only on full-segment match.

        Substring replacement across the whole URI breaks fixed routes (e.g. Intuit
        ``/.well-known/openid_sandbox_configuration/`` when a mapped value is
        ``openid``). Bodies and JSON still use embedded replacement.
        """
        parts = urlsplit(uri)
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                self._scrub_uri_path(parts.path),
                self._scrub_uri_query(parts.query),
                parts.fragment,
            )
        )

    def _scrub_uri_path(self, path: str) -> str:
        if not path:
            return path
        trailing_slash = path.endswith("/")
        segments = path.split("/")
        scrubbed: list[str] = []
        for segment in segments:
            if not segment:
                scrubbed.append(segment)
                continue
            decoded = unquote(segment)
            if (
                decoded.lower() not in _STRUCTURAL_URI_PATH_SEGMENTS
                and decoded in self._map
            ):
                scrubbed.append(quote(self._map[decoded], safe=""))
            else:
                scrubbed.append(segment)
        new_path = "/".join(scrubbed)
        if trailing_slash and not new_path.endswith("/"):
            new_path += "/"
        return new_path

    def _scrub_uri_query(self, query: str) -> str:
        if not query:
            return query
        pairs: list[tuple[str, str]] = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            decoded = unquote(value)
            if decoded in self._map:
                pairs.append((key, self._map[decoded]))
            else:
                pairs.append((key, self.scrub_text(decoded)))
        return urlencode(pairs, doseq=True, quote_via=quote)

    def scrub_body(self, body: str) -> str:
        """JSON bodies scrub as trees; anything else (XML, form, GraphQL) by value."""
        if _is_oauth_form_body(body):
            return body
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return self.scrub_text(body)
        return json.dumps(self.scrub_json(data))


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


def _interaction_request_uri(interaction: dict) -> str:
    return (interaction.get("request") or {}).get("uri") or ""


def _is_oidc_discovery(interaction: dict) -> bool:
    return bool(_OIDC_WELL_KNOWN_RE.search(_interaction_request_uri(interaction)))


def _is_oauth_token_exchange(interaction: dict) -> bool:
    return bool(_OAUTH_TOKEN_URI_RE.search(_interaction_request_uri(interaction)))


def _is_oauth_form_body(body: str) -> bool:
    text = body.strip()
    return text.startswith("grant_type=") and "=" in text


def _singer_messages(path: Path) -> list[tuple[str, dict]]:
    """Parse data.singer; non-JSON lines are kept verbatim."""
    messages = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            messages.append((line, json.loads(line)))
        except json.JSONDecodeError:
            messages.append((line, {}))
    return messages


def _body_string(container: dict) -> str | None:
    body = container.get("body")
    if isinstance(body, dict):
        body = body.get("string")
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) and body.strip() else None


def _set_body_string(container: dict, scrubbed: str) -> None:
    body = container.get("body")
    if isinstance(body, dict):
        body["string"] = (
            scrubbed.encode("utf-8") if isinstance(body.get("string"), bytes) else scrubbed
        )
    else:
        container["body"] = scrubbed

    headers = container.get("headers") or {}
    if "Content-Length" in headers:
        headers["Content-Length"] = [str(len(scrubbed.encode("utf-8")))]


def _collect_surfaces(
    scrubber: TargetValueScrubber,
    messages: list[tuple[str, dict]],
    interactions: list[dict],
) -> None:
    """Singer input, then responses, then requests, then URIs — before any replacing.

    A value first seen in the input must map the same way when it comes back under
    another name in a response, so nothing is replaced until all of them are seen.
    """
    for _, message in messages:
        if message.get("type") == "RECORD":
            scrubber.collect(message.get("record"), from_input=True)

    for interaction in interactions:
        skip_oidc_response = _is_oidc_discovery(interaction)
        skip_oauth_token = _is_oauth_token_exchange(interaction)
        for container, typed_only in (
            (interaction.get("response"), False),
            (interaction.get("request"), True),
        ):
            if container is interaction.get("response") and (
                skip_oidc_response or skip_oauth_token
            ):
                continue
            raw = _body_string(container or {})
            if raw is None:
                continue
            if skip_oauth_token and _is_oauth_form_body(raw):
                continue
            try:
                scrubber.collect(json.loads(raw), typed_only=typed_only)
            except json.JSONDecodeError:
                continue

    for interaction in interactions:
        uri = (interaction.get("request") or {}).get("uri")
        if uri:
            scrubber.collect_uri(uri)


def _apply_to_singer(
    scrubber: TargetValueScrubber, path: Path, messages: list[tuple[str, dict]]
) -> None:
    """Rewrite RECORD payloads only; SCHEMA and STATE are structure, not data."""
    lines = []
    for line, message in messages:
        if message.get("type") == "RECORD" and "record" in message:
            message["record"] = scrubber.scrub_json(message["record"])
            lines.append(json.dumps(message))
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n")


def _apply_to_interaction(
    scrubber: TargetValueScrubber, interaction: dict, scrub_uri: Any
) -> None:
    request = interaction.get("request") or {}
    if request.get("uri"):
        request["uri"] = scrubber.scrub_uri(request["uri"])
        if scrub_uri is not None:
            request["uri"] = scrub_uri(request["uri"])
    skip_oidc_response = _is_oidc_discovery(interaction)
    skip_oauth_token = _is_oauth_token_exchange(interaction)
    for container in (request, interaction.get("response") or {}):
        if container is interaction.get("response") and skip_oidc_response:
            continue
        raw = _body_string(container)
        if raw is None:
            continue
        if skip_oauth_token:
            if _is_oauth_form_body(raw):
                continue
            if container is interaction.get("response"):
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    pass
                else:
                    _set_body_string(
                        container,
                        json.dumps(scrub_tokens_in_json(data, scrubber.token_keys)),
                    )
                continue
        _set_body_string(container, scrubber.scrub_body(raw))


def scrub_target_case(
    case_dir: str | Path,
    cassette_path: str | Path,
    *,
    preserve_keys: set[str] | None = None,
    token_keys: set[str] | None = None,
    preserve_values: set[str] | None = None,
    scrub_uri: Any = None,
) -> TargetValueScrubber:
    """Scrub data.singer and the cassette with one shared value map (in memory only)."""
    case_dir = Path(case_dir)
    cassette_path = Path(cassette_path)
    singer_path = case_dir / "data.singer"
    scrubber = TargetValueScrubber(preserve_keys, token_keys, preserve_values)

    messages = _singer_messages(singer_path) if singer_path.is_file() else []
    cassette = load_cassette(cassette_path) if cassette_path.is_file() else {}
    interactions = cassette.get("interactions") or []

    _collect_surfaces(scrubber, messages, interactions)
    scrubber.build()

    if messages:
        _apply_to_singer(scrubber, singer_path, messages)
    for interaction in interactions:
        _apply_to_interaction(scrubber, interaction, scrub_uri)
    if interactions:
        write_cassette(cassette_path, cassette)
    return scrubber
