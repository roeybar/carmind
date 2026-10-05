"""
Read-only knowledge-base loader.

CARMIND_KB_DIR is the directory that contains knowledge/ (in the container: /kb).
Without it, the loader walks up from this file to the repo root. Files are loaded
on first access (the app loads them at startup) and never written.

The indicator records and the context rules are validated against
schema/knowledge.schema.json. Any record that does not match, for example one without
provenance.manual_id or provenance.source_page, makes the load fail, so the service
refuses to start rather than serve an unsourced record.

The three call-desk files (customers, company policy, dispatch model) are read but
not schema-validated.
"""
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator

_KB_ROOT: Path | None = None
_INDICATORS_CACHE: dict | None = None
_PROCEDURES_CACHE: dict | None = None
_FLEET_CACHE: list | None = None
_CONTEXT_RULES_CACHE: list | None = None
_DESK_CACHE: dict | None = None


def _resolve_root() -> Path:
    global _KB_ROOT
    if _KB_ROOT is not None:
        return _KB_ROOT

    env = os.environ.get("CARMIND_KB_DIR")
    if env:
        root = Path(env)
    else:
        root = None
        for parent in Path(__file__).resolve().parents:
            if (parent / "knowledge").is_dir():
                root = parent
                break
        if root is None:
            raise RuntimeError(
                "Cannot locate KB root: no ancestor of __file__ contains knowledge/. "
                "Set CARMIND_KB_DIR to the directory containing knowledge/."
            )

    expected = root / "knowledge" / "extracted" / "skoda_gas.indicators.json"
    if not expected.exists():
        raise RuntimeError(
            f"KB root resolved to {root!s} but expected file is missing: {expected}"
        )

    _KB_ROOT = root
    return root


def _validate(doc: dict, path: Path, validator: Draft202012Validator) -> None:
    errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    if not errors:
        return
    lines = []
    for err in errors:
        loc = list(err.absolute_path)
        where = "/".join(str(p) for p in loc)
        if len(loc) >= 2 and isinstance(loc[1], int):
            rec = doc[loc[0]][loc[1]]
            if isinstance(rec, dict) and rec.get("id"):
                where = rec["id"]
        lines.append(f"  {where}: {err.message}")
    raise RuntimeError(f"{path} does not match the knowledge schema:\n" + "\n".join(lines))


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load() -> None:
    global _INDICATORS_CACHE, _PROCEDURES_CACHE, _FLEET_CACHE, _CONTEXT_RULES_CACHE, _DESK_CACHE
    if _INDICATORS_CACHE is not None:
        return

    root = _resolve_root()

    schema_path = root / "schema" / "knowledge.schema.json"
    if not schema_path.exists():
        raise RuntimeError(f"Knowledge schema not found: {schema_path}")
    validator = Draft202012Validator(_read(schema_path))

    indicators_path = root / "knowledge" / "extracted" / "skoda_gas.indicators.json"
    raw = _read(indicators_path)
    _validate(raw, indicators_path, validator)

    rules_path = root / "knowledge" / "context-rules.json"
    rules = _read(rules_path)
    _validate(rules, rules_path, validator)

    # The fleet rulebook and catalog have no matching definition in the schema.
    rulebook = _read(root / "knowledge" / "example-icon-rulebook.json")
    fleet = _read(root / "knowledge" / "fleet-catalog.json")

    _PROCEDURES_CACHE = {p["id"]: p for p in rulebook["procedures"]}
    _FLEET_CACHE = fleet["models"]
    _CONTEXT_RULES_CACHE = rules["context_rules"]
    _DESK_CACHE = {
        "customers": {c["id"]: c for c in _read(root / "knowledge" / "customers.json")["customers"]},
        "policy": _read(root / "knowledge" / "company-policy.json"),
        "dispatch": _read(root / "knowledge" / "dispatch-model.json"),
    }
    # Keyed by qualified id, e.g. skoda_gas.brake_fluid_low. Set last: it marks the load done.
    _INDICATORS_CACHE = {rec["id"]: rec for rec in raw["indicators"]}


def indicators() -> dict:
    _load()
    return _INDICATORS_CACHE  # type: ignore[return-value]


def indicator(qualified_id: str) -> dict | None:
    _load()
    return _INDICATORS_CACHE.get(qualified_id)  # type: ignore[union-attr]


def procedures() -> dict:
    _load()
    return _PROCEDURES_CACHE  # type: ignore[return-value]


def procedure(proc_id: str) -> dict | None:
    _load()
    return _PROCEDURES_CACHE.get(proc_id)  # type: ignore[union-attr]


def fleet_models() -> list:
    _load()
    return _FLEET_CACHE  # type: ignore[return-value]


def context_rules() -> list:
    _load()
    return _CONTEXT_RULES_CACHE  # type: ignore[return-value]


def customers() -> list:
    _load()
    return list(_DESK_CACHE["customers"].values())  # type: ignore[index]


def customer(customer_id: str) -> dict | None:
    _load()
    return _DESK_CACHE["customers"].get(customer_id)  # type: ignore[index]


def company_policy() -> dict:
    _load()
    return _DESK_CACHE["policy"]  # type: ignore[index]


def dispatch_model() -> dict:
    _load()
    return _DESK_CACHE["dispatch"]  # type: ignore[index]
