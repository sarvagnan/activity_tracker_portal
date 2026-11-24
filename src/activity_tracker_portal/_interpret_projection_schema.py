"""The closed projection/v1 JSON Schema subset, with model-order diagnostics.

The generated schemas preserve model.json's property order, including referenced
objects and variants. The root visits metadata before record. This validator is
intentionally limited to the keywords in that frozen schema family; it is not a
general JSON Schema implementation and never resolves remote references.
"""

from functools import lru_cache
import json
import math
from pathlib import Path
import re
from typing import Any

_SCHEMA_DIRECTORY = Path(__file__).resolve().parents[2] / "projection/v1/schemas"
_FIELD = re.compile(r"[a-z0-9_]+")
_INDEX = re.compile(r"0|[1-9][0-9]*")


@lru_cache(maxsize=41)
def projection_schema(record_type: str, version: int) -> dict[str, Any] | None:
    if version != 1 or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record_type) is None:
        return None
    path = _SCHEMA_DIRECTORY / (record_type + ".schema.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _resolved(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise ValueError("non-local projection schema reference")
        schema = root
        for part in reference[2:].split("/"):
            schema = schema[part.replace("~1", "/").replace("~0", "~")]
    return schema


def _child(path: str, name: str | int) -> str:
    return path + "/" + str(name).replace("~", "~0").replace("/", "~1")


def _equal(value: Any, constant: Any) -> bool:
    # Python's bool is numeric; JSON's boolean is a different primitive type.
    return isinstance(value, bool) == isinstance(constant, bool) and value == constant


@lru_cache(maxsize=3)
def _pattern(pattern: str) -> re.Pattern[str]:
    # All three patterns in the frozen schema family use '$' only as an anchor.
    # ECMA-262 requires end of input; Python '$' also permits a final newline.
    return re.compile(pattern.replace("$", r"\Z"))


def _issue(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> str | None:
    schema = _resolved(schema, root)
    if "oneOf" in schema:
        matches = sum(_issue(value, branch, root, path) is None for branch in schema["oneOf"])
        if matches != 1:
            return path
    kind = schema.get("type")
    if kind is not None:
        valid = {
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": type(value) is bool,
            "integer": type(value) is int or type(value) is float and value.is_integer(),
            "number": type(value) in (int, float),
        }[kind]
        if not valid:
            return path
    if "const" in schema and not _equal(value, schema["const"]):
        return path
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        return path
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return path
        if "pattern" in schema and _pattern(schema["pattern"]).search(value) is None:
            return path
    if type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            return path
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        order = list(properties)
        if not path:
            order = [name for name in ("metadata", "record") if name in properties] + [
                name for name in order if name not in ("metadata", "record")
            ]
        for name in order:
            if name not in value:
                if name in schema.get("required", ()):
                    return _child(path, name)
            else:
                issue = _issue(value[name], properties[name], root, _child(path, name))
                if issue is not None:
                    return issue
        if schema.get("additionalProperties") is False:
            for name in sorted(value):
                if name not in properties:
                    return _child(path, name)
    if isinstance(value, list):
        for index, item in enumerate(value):
            issue = _issue(item, schema.get("items", {}), root, _child(path, index))
            if issue is not None:
                return issue
    return None


def schema_issue(value: Any, schema: dict[str, Any]) -> str | None:
    return _issue(value, schema, schema, "")


def schema_pointer(schema: dict[str, Any], pointer: str) -> bool:
    """A safe field pointer into any declared branch, including absent fields."""
    if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
        return False
    parts = pointer[1:].split("/") if pointer else []
    if any(_FIELD.fullmatch(part) is None for part in parts):
        return False

    def contains(node: dict[str, Any], remaining: list[str]) -> bool:
        node = _resolved(node, schema)
        if not remaining:
            return True
        if "oneOf" in node:
            return any(contains(branch, remaining) for branch in node["oneOf"])
        head, *tail = remaining
        if node.get("type") == "array":
            return _INDEX.fullmatch(head) is not None and contains(node["items"], tail)
        properties = node.get("properties", {})
        return head in properties and contains(properties[head], tail)

    return contains(schema, parts)
