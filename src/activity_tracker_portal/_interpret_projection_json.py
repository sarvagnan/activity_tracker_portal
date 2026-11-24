"""Strict JSON input and RFC 8785 output for interpret-source-projection.

Integer inputs are already checked for the I-JSON domain by the caller. Finite
binary64 values use Python's shortest round-trip digits with ECMAScript's decimal
and exponent layout. Object keys sort by UTF-16 code units, not code points.
"""

from collections.abc import Mapping
import json
import math
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError()
    return value


def _reject_constant(text: str) -> None:
    raise ValueError()


def _check_strings(value: Any) -> None:
    """I-JSON strings contain Unicode scalar values, including object keys."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
                raise ValueError()
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def parse_object(data: bytes) -> dict[str, Any]:
    """One UTF-8 object with scalar strings, unique keys and finite numbers."""
    value = json.loads(
        data.decode("utf-8"), object_pairs_hook=_unique_object,
        parse_float=_finite_float, parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError()
    _check_strings(value)
    return value


def _number(value: int | float) -> str:
    if type(value) is int:
        return str(value)
    if not math.isfinite(value):
        raise ValueError()
    if value == 0:
        return "0"
    mantissa, _, exponent = repr(abs(value)).partition("e")
    digits = mantissa.replace(".", "").lstrip("0")
    scale = int(exponent or "0") - (len(mantissa.split(".")[1]) if "." in mantissa else 0)
    stripped = digits.rstrip("0")
    scale += len(digits) - len(stripped)
    digits = stripped
    point = len(digits) + scale
    if -6 < point <= 0:
        result = "0." + "0" * -point + digits
    elif 0 < point <= 21:
        result = (digits + "0" * (point - len(digits)) if point >= len(digits)
                  else digits[:point] + "." + digits[point:])
    else:
        power = point - 1
        result = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        result += "e" + ("+" if power >= 0 else "-") + str(abs(power))
    return ("-" if value < 0 else "") + result


def _canonical(value: Any) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return _number(value)
    if isinstance(value, str):
        # UTF-8 encoding below rejects lone surrogates as required by RFC 8785.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(_canonical(key) + ":" + _canonical(value[key]) for key in keys) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    raise ValueError()


def canonical_bytes(value: Any) -> bytes:
    try:
        encoded = _canonical(value).encode("utf-8")
    except UnicodeError:
        encoded = None
    # Leave the catch boundary before raising: encoding exceptions retain their
    # entire input string, which must not survive in args or exception chains.
    if encoded is None:
        raise ValueError()
    return encoded
