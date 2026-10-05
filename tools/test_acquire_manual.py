"""
acquire_manual.py run against local file:// URLs in a throwaway copy of the repo layout.

A PDF is stored and registered with generation_confirmed false; a non-PDF is rejected
and leaves nothing behind; a URL that is already registered is skipped. Needs curl.
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "acquire_manual.py"
PDF = (b"%PDF-1.4\n1 0 obj << /Type /Pages >> endobj\n"
       b"2 0 obj << /Type /Page >> endobj\n3 0 obj << /Type /Page >> endobj\n%%EOF\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "knowledge").mkdir()
    shutil.copy(SCRIPT, root / "tools" / "acquire_manual.py")
    return root


def _run(repo, *args):
    return subprocess.run(
        [sys.executable, str(repo / "tools" / "acquire_manual.py"), *args],
        capture_output=True, text=True,
    )


def _manifest(repo):
    return json.loads((repo / "knowledge" / "manuals-manifest.json").read_text(encoding="utf-8"))


def test_pdf_is_saved_and_registered(repo, tmp_path):
    src = tmp_path / "manual.pdf"
    src.write_bytes(PDF)
    r = _run(repo, "Skoda", "Kamiq", src.as_uri(), "--gen", "2nd gen", "--lang", "en")
    assert r.returncode == 0, r.stdout + r.stderr
    [entry] = _manifest(repo)["manuals"]
    assert entry["path"] == "manuals_in/Skoda/Skoda_Kamiq_2nd_gen.pdf"
    assert entry["generation_confirmed"] is False
    assert entry["pages_est"] == 2
    assert entry["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert (repo / entry["path"]).read_bytes() == PDF


def test_non_pdf_is_rejected(repo, tmp_path):
    src = tmp_path / "viewer.html"
    src.write_text("<html>manual viewer</html>")
    r = _run(repo, "Skoda", "Kamiq", src.as_uri())
    assert r.returncode == 3, r.stdout + r.stderr
    assert "REJECTED: not a PDF" in r.stdout
    assert not (repo / "knowledge" / "manuals-manifest.json").exists()
    assert list((repo / "manuals_in" / "Skoda").iterdir()) == []


def test_registered_url_is_skipped_without_force(repo, tmp_path):
    src = tmp_path / "manual.pdf"
    src.write_bytes(PDF)
    assert _run(repo, "Skoda", "Kamiq", src.as_uri()).returncode == 0
    r = _run(repo, "Skoda", "Kamiq", src.as_uri())
    assert r.returncode == 0 and "already registered" in r.stdout
    assert len(_manifest(repo)["manuals"]) == 1
