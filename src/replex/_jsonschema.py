"""A small JSON Schema validator so the core install needs no extra dependency.

Supports the keywords agents' outputs normally need: ``type``, ``properties``, ``required``,
``additionalProperties``, ``items``, ``enum``, ``const``, ``minimum``, ``maximum``,
``exclusiveMinimum``, ``exclusiveMaximum``, ``minLength``, ``maxLength``, ``pattern``,
``minItems``, ``maxItems``, ``uniqueItems``, ``anyOf``, ``oneOf``, ``allOf`` and ``not``.
If the ``jsonschema`` package is installed it is used instead, for full coverage.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _is_type(value: Any, name: str) -> bool:
    if name in ("number", "integer") and isinstance(value, bool):
        return False
    if name == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, _TYPES.get(name, (object,)))


def _errors(value: Any, schema: Any, path: str) -> list[str]:
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{path}: no value is allowed here"]
    errs: list[str] = []
    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_is_type(value, n) for n in names):
            return [f"{path}: expected type {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: expected {schema['const']!r}, got {value!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} is less than minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} is greater than maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errs.append(f"{path}: {value} must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errs.append(f"{path}: {value} must be < {schema['exclusiveMaximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: does not match pattern {schema['pattern']!r}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(seen) != len(set(seen)):
                errs.append(f"{path}: items are not unique")
        if "items" in schema:
            for i, item in enumerate(value):
                errs += _errors(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errs.append(f"{path}: missing required property {key!r}")
        for key, sub in props.items():
            if key in value:
                errs += _errors(value[key], sub, f"{path}.{key}")
        extra = schema.get("additionalProperties", True)
        for key in value:
            if key in props:
                continue
            if extra is False:
                errs.append(f"{path}: unexpected property {key!r}")
            elif isinstance(extra, dict):
                errs += _errors(value[key], extra, f"{path}.{key}")
    for sub in schema.get("allOf", []):
        errs += _errors(value, sub, path)
    if "anyOf" in schema and not any(not _errors(value, s, path) for s in schema["anyOf"]):
        errs.append(f"{path}: does not match any schema in anyOf")
    if "oneOf" in schema:
        n = sum(1 for s in schema["oneOf"] if not _errors(value, s, path))
        if n != 1:
            errs.append(f"{path}: matches {n} schemas in oneOf (expected exactly 1)")
    if "not" in schema and not _errors(value, schema["not"], path):
        errs.append(f"{path}: must not match the 'not' schema")
    return errs


def validate(value: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of validation errors (empty when ``value`` is valid)."""
    try:
        import jsonschema
    except ImportError:
        return _errors(value, schema, "$")
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"$.{'.'.join(str(p) for p in e.absolute_path)}: {e.message}".replace("$.:", "$:")
        for e in validator.iter_errors(value)
    ]


def parse_json_output(text: str) -> Any:
    """Parse a model's final output as JSON, tolerating a surrounding Markdown code fence."""
    stripped = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1)
    return json.loads(stripped)
