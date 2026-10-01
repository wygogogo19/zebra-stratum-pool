"""A small JSON Schema (2020-12 subset) validator.

The engine and its tests deliberately avoid third-party dependencies, so the telemetry contracts in
`schemas/` are validated with this ~130-line subset instead of the `jsonschema` package. It supports the
keywords those schemas actually use: `$ref` (relative file + JSON pointer), `type`, `required`,
`properties`, `additionalProperties`, `items`, `enum`, `const`, numeric/length/array bounds, `pattern`,
`allOf`/`anyOf`/`oneOf`/`not` and `$defs`.

`tests/test_telemetry_schemas.py` also exercises this validator with deliberately broken schemas and
payloads, so a validator that silently accepted everything could not pass the suite.
"""
from __future__ import annotations

import json
import os
import re
import sys


class SchemaError(Exception):
    """Raised when a schema (or a $ref inside it) cannot be loaded."""


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        raise SchemaError("cannot load schema %s: %s" % (path, exc))


def _pointer(doc: dict, pointer: str):
    if pointer in ("", "/"):
        return doc
    node = doc
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        else:
            if token not in node:
                raise SchemaError("unresolvable pointer %r" % pointer)
            node = node[token]
    return node


def _matches_type(value, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise SchemaError("unsupported type keyword %r" % name)


def validate(instance, schema: dict, *, base_dir: str = ".", path: str = "$",
             _cache: dict | None = None, _depth: int = 0, _root: dict | None = None) -> list[str]:
    """Return a list of human-readable errors (empty list = valid)."""
    cache = _cache if _cache is not None else {}
    root = _root if _root is not None else schema      # root document (target of same-file "#/..." refs)
    errors: list[str] = []
    if _depth > 40:
        raise SchemaError("$ref nesting too deep at %s" % path)

    if not isinstance(schema, dict):
        raise SchemaError("schema at %s is not an object" % path)

    if "$ref" in schema:
        ref = schema["$ref"]
        file_part, _, pointer = ref.partition("#")
        if file_part:
            target_path = os.path.join(base_dir, file_part)
            if target_path not in cache:
                cache[target_path] = _load(target_path)
            doc = cache[target_path]
            sub_dir = os.path.dirname(target_path) or "."
        else:
            doc, sub_dir = root, base_dir
        resolved = _pointer(doc, pointer)
        return validate(instance, resolved, base_dir=sub_dir, path=path, _cache=cache,
                        _depth=_depth + 1, _root=doc)

    def err(msg: str) -> None:
        errors.append("%s: %s" % (path, msg))

    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_matches_type(instance, n) for n in names):
            err("expected type %s, got %s" % ("/".join(names), type(instance).__name__))
            return errors              # further keyword checks would be noise

    if "enum" in schema and instance not in schema["enum"]:
        err("value %r not in enum %r" % (instance, schema["enum"]))
    if "const" in schema and instance != schema["const"]:
        err("value %r != const %r" % (instance, schema["const"]))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            err("%r < minimum %r" % (instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            err("%r > maximum %r" % (instance, schema["maximum"]))
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            err("%r <= exclusiveMinimum %r" % (instance, schema["exclusiveMinimum"]))
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            err("%r >= exclusiveMaximum %r" % (instance, schema["exclusiveMaximum"]))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            err("string shorter than minLength %d" % schema["minLength"])
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            err("string longer than maxLength %d" % schema["maxLength"])
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            err("string %r does not match pattern %r" % (instance[:40], schema["pattern"]))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            err("fewer than minItems %d" % schema["minItems"])
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            err("more than maxItems %d" % schema["maxItems"])
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                errors += validate(item, item_schema, base_dir=base_dir, path="%s[%d]" % (path, i),
                                   _cache=cache, _depth=_depth + 1, _root=root)

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                err("missing required property %r" % key)
        props = schema.get("properties") or {}
        for key, value in instance.items():
            if key in props:
                errors += validate(value, props[key], base_dir=base_dir, path="%s.%s" % (path, key),
                                   _cache=cache, _depth=_depth + 1, _root=root)
                continue
            extra = schema.get("additionalProperties", True)
            if extra is False:
                err("unexpected property %r" % key)
            elif isinstance(extra, dict):
                errors += validate(value, extra, base_dir=base_dir, path="%s.%s" % (path, key),
                                   _cache=cache, _depth=_depth + 1, _root=root)

    for keyword in ("allOf", "anyOf", "oneOf"):
        for i, sub in enumerate(schema.get(keyword, [])):
            sub_errors = validate(instance, sub, base_dir=base_dir, path=path, _cache=cache, _depth=_depth + 1)
            if keyword == "allOf" and sub_errors:
                errors += sub_errors
            elif keyword == "anyOf" and not sub_errors:
                break
        else:
            if keyword in ("anyOf", "oneOf") and schema.get(keyword):
                if keyword == "oneOf":
                    passing = sum(1 for sub in schema[keyword]
                                  if not validate(instance, sub, base_dir=base_dir, path=path,
                                                  _cache=cache, _depth=_depth + 1, _root=root))
                    if passing != 1:
                        err("expected exactly one oneOf branch to match, %d did" % passing)
                else:
                    err("no anyOf branch matched")

    if "not" in schema and not validate(instance, schema["not"], base_dir=base_dir, path=path,
                                        _cache=cache, _depth=_depth + 1, _root=root):
        err("matched a `not` schema")

    return errors


def load_and_validate(instance_path: str, schema_path: str) -> list[str]:
    """Convenience helper: validate a JSON file against a schema file."""
    instance = _load(instance_path)
    schema = _load(schema_path)
    return validate(instance, schema, base_dir=os.path.dirname(os.path.abspath(schema_path)))


def collect_refs(schema, base_dir: str, _cache: dict | None = None, _seen: set | None = None) -> set[str]:
    """Walk a schema and return every `$ref` target as (file, pointer), verifying each resolves."""
    cache = _cache if _cache is not None else {}
    seen = _seen if _seen is not None else set()
    found: set[str] = set()
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            file_part, _, pointer = ref.partition("#")
            target_path = os.path.join(base_dir, file_part) if file_part else None
            if target_path:
                if target_path not in cache:
                    cache[target_path] = _load(target_path)
                _pointer(cache[target_path], pointer)     # raises if unresolvable
            found.add(ref)
        for value in schema.values():
            found |= collect_refs(value, base_dir, cache, seen)
    elif isinstance(schema, list):
        for value in schema:
            found |= collect_refs(value, base_dir, cache, seen)
    return found


def _main(argv: list[str]) -> int:
    """CLI: `python3 tests/_schema.py <schema.json> <payload.json>`.

    Validates one payload file against one schema file and prints the errors, if any. Exit status is
    0 when the payload conforms and 1 when it does not, so it drops straight into a shell pipeline.
    """
    if len(argv) != 3:
        print("usage: python3 tests/_schema.py <schema.json> <payload.json>", file=sys.stderr)
        return 2
    errors = load_and_validate(argv[2], argv[1])
    if errors:
        for message in errors:
            print("FAIL: %s" % message)
        return 1
    print("OK: %s conforms to %s" % (argv[2], argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
