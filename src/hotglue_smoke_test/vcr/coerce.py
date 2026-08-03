"""Normalize recorded HTTP requests so VCR's safe YAML serializer can persist them."""

from __future__ import annotations

from typing import Any


def _coerce_plain_str(value: Any) -> Any:
    """Return a built-in str when value is a str subclass (e.g. SDK SecretString)."""
    if isinstance(value, str) and type(value) is not str:
        return str(value)
    return value


def _coerce_mapping_values(mapping: dict) -> dict:
    return {key: _coerce_plain_str(value) for key, value in mapping.items()}


def coerce_request_plain_strings(request):
    """Coerce str subclasses in a VCR request to built-in str before cassette save."""
    for name, value in list(request.headers.items()):
        if isinstance(value, list):
            request.headers[name] = [_coerce_plain_str(item) for item in value]
        else:
            request.headers[name] = _coerce_plain_str(value)

    body = request.body
    if isinstance(body, dict):
        request.body = _coerce_mapping_values(body)

    return request
