#!/usr/bin/env python3
"""Freeze already-qualified Python images with hashes from the primary PyPI API.

This does not install or upgrade dependencies. Review generated files before
rebuilding and auditing; the lock is a record of the supplied image environments.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen


def freeze(image):
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", image, "python", "-m", "pip", "freeze", "--all"],
        text=True, capture_output=True, check=True, timeout=30)
    packages = {}
    for line in result.stdout.splitlines():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+", line):
            raise RuntimeError("Dépendance non figée dans l'image")
        name, version = line.split("==")
        name = re.sub(r"[-_.]+", "-", name).lower()
        packages[name] = version
    return packages


def release(item):
    name, version = item
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urlopen(Request(url, headers={"User-Agent": "grocyste-audited-lock/1.0"}), timeout=20) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("Métadonnées PyPI trop volumineuses")
        record = json.loads(raw)
    if record["info"]["version"] != version:
        raise RuntimeError("Version PyPI différente")
    hashes = set()
    for file in record["urls"]:
        if file.get("packagetype") != "bdist_wheel" or file.get("yanked"):
            continue
        digest = file.get("digests", {}).get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise RuntimeError("Empreinte PyPI invalide")
        hashes.add(digest)
    if not hashes:
        raise RuntimeError("Aucune wheel non retirée pour " + name)
    return name, {"version": version, "url": url, "jsonSha256": hashlib.sha256(raw).hexdigest(),
        "wheelSha256": sorted(hashes)}


def render(names, metadata, header):
    lines = ["# SPDX-License-Identifier: GPL-3.0-or-later", "# " + header,
             "# Frozen from audited images; hashes are non-yanked PyPI wheels only."]
    for name in sorted(names):
        item = metadata[name]
        lines.append(name + "==" + item["version"] + " \\")
        hashes = item["wheelSha256"]
        for index, digest in enumerate(hashes):
            lines.append("    --hash=sha256:" + digest + (" \\" if index < len(hashes) - 1 else ""))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--tests-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    runtime, development = freeze(args.runtime_image), freeze(args.tests_image)
    if any(development.get(name) != version for name, version in runtime.items()):
        raise RuntimeError("Les images runtime et tests ont des versions incohérentes")
    with ThreadPoolExecutor(max_workers=8) as pool:
        metadata = dict(pool.map(release, sorted(development.items())))
    output.mkdir(parents=True, exist_ok=True)
    (output / "requirements.lock.txt").write_text(render(runtime, metadata, "Runtime Python 3.12 ; versions exactes et SHA-256."))
    extras = set(development) - set(runtime)
    (output / "requirements-dev.lock.txt").write_text("-r requirements.lock.txt\n" + render(extras, metadata,
        "Dépendances supplémentaires de qualification ; versions exactes et SHA-256."))
    (output / "requirements-bootstrap.lock.txt").write_text(render(["pip"], metadata, "pip avant installation des dépendances."))
    (output / "provenance.json").write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"runtimeDependencies": len(runtime), "developmentDependencies": len(development),
        "sources": "https://pypi.org/pypi/<name>/<version>/json", "directory": str(output)}))


if __name__ == "__main__":
    main()
