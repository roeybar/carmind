#!/usr/bin/env python3
"""Disciplined owner's-manual acquisition + registration.

Downloads a manual PDF, verifies it is a real PDF, records provenance
(source URL, sha256, bytes, page count) into a manifest, and stores it under
manuals_in/<Make>/<Make>_<Model>[_gen].pdf.

Nothing is trusted until a human confirms generation match: every entry lands with
"generation_confirmed": false. Extraction must skip unconfirmed manuals.

Usage:
  acquire_manual.py <make> <model> <url> [--gen "2nd gen 2019-2025"] [--lang en] [--force]
Manifest: knowledge/manuals-manifest.json
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANUALS = f"{ROOT}/manuals_in"
MANIFEST = f"{ROOT}/knowledge/manuals-manifest.json"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0"

def load():
    if os.path.exists(MANIFEST):
        return json.load(open(MANIFEST, encoding="utf-8"))
    return {"_meta": {"note": "acquired owner's manuals; generation_confirmed=false until a human verifies model+generation match. Extraction must skip unconfirmed."}, "manuals": []}

def save(m):
    json.dump(m, open(MANIFEST, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def pdf_pages(path):
    try:
        data = open(path, "rb").read()
        # cheap page count: count /Type /Page (not /Pages) objects
        n = len(re.findall(rb"/Type\s*/Page[^s]", data))
        return n or None
    except Exception:
        return None

def main():
    a = sys.argv[1:]
    if len(a) < 3:
        print(__doc__)
        sys.exit(1)
    make, model, url = a[0], a[1], a[2]
    gen = lang = ""
    force = False
    i = 3
    while i < len(a):
        if a[i] == "--gen":
            gen = a[i+1]
            i += 2
        elif a[i] == "--lang":
            lang = a[i+1]
            i += 2
        elif a[i] == "--force":
            force = True
            i += 1
        else:
            i += 1

    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{make}_{model}").strip("_")
    if gen:
        slug += "_" + re.sub(r"[^A-Za-z0-9]+", "_", gen).strip("_")[:24]
    dstdir = f"{MANUALS}/{make}"
    os.makedirs(dstdir, exist_ok=True)
    dst = f"{dstdir}/{slug}.pdf"

    man = load()
    if any(e["url"] == url for e in man["manuals"]) and not force:
        print(f"already registered: {url}")
        return

    tmp = dst + ".part"
    r = subprocess.run(["curl", "-sSL", "-m", "120", "-A", UA, "-o", tmp, "-w",
                        "%{http_code} %{content_type} %{size_download}", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"curl failed: {r.stderr[:200]}")
        sys.exit(2)
    parts = r.stdout.split()
    code = parts[0] if parts else "?"
    ctype = parts[1] if len(parts) > 1 else "?"
    size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    magic = open(tmp, "rb").read(5) if os.path.exists(tmp) else b""
    if magic != b"%PDF-":
        os.path.exists(tmp) and os.remove(tmp)
        print(f"REJECTED: not a PDF (http={code} type={ctype} magic={magic!r}): {url}")
        sys.exit(3)
    if size < 300_000:
        print(f"WARN: only {size} bytes, likely a brochure/datasheet, not a full handbook.")

    os.rename(tmp, dst)
    sha = hashlib.sha256(open(dst, "rb").read()).hexdigest()
    pages = pdf_pages(dst)
    entry = {
        "make": make, "model": model, "generation": gen or "UNSPECIFIED",
        "lang": lang or "unknown", "url": url, "path": dst.replace(ROOT + "/", ""),
        "bytes": size, "sha256": sha, "pages_est": pages,
        "generation_confirmed": False,
    }
    man["manuals"] = [e for e in man["manuals"] if e["url"] != url] + [entry]
    save(man)
    print(f"SAVED {dst}\n  {size:,} bytes, ~{pages} pages, sha256 {sha[:16]}...\n  generation_confirmed=false (needs human verification)")

if __name__ == "__main__":
    main()
