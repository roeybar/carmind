"""
Test configuration.

Points CARMIND_KB_DIR at the repo root (two levels up) unless it is already set, and
defaults LUNA_MODE to off. This runs before any app import so the lazy KB loader sees it.
"""
import os
from pathlib import Path

if "CARMIND_KB_DIR" not in os.environ:
    os.environ["CARMIND_KB_DIR"] = str(Path(__file__).resolve().parents[2])

os.environ.setdefault("LUNA_MODE", "off")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


_KB_STATE = ("_KB_ROOT", "_INDICATORS_CACHE", "_PROCEDURES_CACHE", "_FLEET_CACHE",
             "_CONTEXT_RULES_CACHE", "_DESK_CACHE")


@pytest.fixture
def real_indicators():
    """A deep copy of the shipped indicator records."""
    import copy
    import json
    root = Path(os.environ["CARMIND_KB_DIR"])
    path = root / "knowledge" / "extracted" / "skoda_gas.indicators.json"
    return copy.deepcopy(json.loads(path.read_text(encoding="utf-8"))["indicators"])


@pytest.fixture
def synthetic_kb(tmp_path, monkeypatch):
    """
    Return a function that writes a throwaway KB (a copy of the real one, with the
    given indicator records and/or context rules swapped in) and points the loader at it.

    The loader's cached state is reset now and restored after the test.
    """
    import json
    import shutil

    from app import kb

    real = Path(os.environ["CARMIND_KB_DIR"])

    def make(indicators=None, context_rules=None):
        root = tmp_path / "kb"
        shutil.copytree(real / "knowledge", root / "knowledge")
        shutil.copytree(real / "schema", root / "schema")
        if indicators is not None:
            path = root / "knowledge" / "extracted" / "skoda_gas.indicators.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["indicators"] = indicators
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        if context_rules is not None:
            path = root / "knowledge" / "context-rules.json"
            path.write_text(json.dumps({"context_rules": context_rules}), encoding="utf-8")
        monkeypatch.setenv("CARMIND_KB_DIR", str(root))
        for name in _KB_STATE:
            monkeypatch.setattr(kb, name, None, raising=False)
        return root

    return make
