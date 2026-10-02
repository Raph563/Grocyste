#!/usr/bin/env python3
"""Check declared vendored bytes and query OSV/npm for their exact versions.

This is an advisory inventory, not a binary analysis or a complete transitive
SBOM. No package script runs and no runtime dependency is changed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

UPSTREAM_LOCKS = (
    "https://raw.githubusercontent.com/naptha/tesseract.js/v5.1.1/package-lock.json",
    "https://raw.githubusercontent.com/chartjs/Chart.js/v2.9.4/package-lock.json",
)


def post(url, value):
    request = Request(url, json.dumps(value).encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "Grocyste-vendor-qualification/1.0"})
    with urlopen(request, timeout=30) as response:
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise RuntimeError("Réponse advisory trop volumineuse")
        return json.loads(data)


def upstream_runtime_pairs():
    pairs, evidence = set(), []
    for url in UPSTREAM_LOCKS:
        with urlopen(Request(url, headers={"User-Agent": "Grocyste-vendor-qualification/1.0"}), timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise RuntimeError("Lockfile amont trop volumineux")
        value = json.loads(raw)
        evidence.append({"url": url, "sha256": hashlib.sha256(raw).hexdigest()})
        if "packages" in value:
            pairs.update((path.rsplit("node_modules/", 1)[-1], record["version"])
                         for path, record in value["packages"].items()
                         if path and not record.get("dev") and "version" in record)
        else:
            def walk(records):
                for name, record in records.items():
                    if not record.get("dev"):
                        pairs.add((name, record["version"]))
                    walk(record.get("dependencies", {}))
            walk(value.get("dependencies", {}))
    ordered = sorted(pairs)
    response = post("https://api.osv.dev/v1/querybatch", {"queries": [
        {"package": {"name": name, "ecosystem": "npm"}, "version": version}
        for name, version in ordered]})
    if len(response.get("results", [])) != len(ordered):
        raise RuntimeError("Réponse OSV transitive incomplète")
    return {"sources": evidence, "runtimePairs": [
        {"name": name, "version": version, "osv": result.get("vulns", [])}
        for (name, version), result in zip(ordered, response["results"])]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream-locks", action="store_true",
                        help="Comparer aussi les lockfiles primaires historiques, sans supposer leurs paquets tous embarqués.")
    args = parser.parse_args()
    packages, checked = set(), 0
    for vendor in args.vendor:
        vendor = vendor.resolve(strict=True)
        provenance = json.loads((vendor / "provenance.json").read_bytes())
        for record in provenance["files"]:
            relative = Path(record["path"])
            unresolved = vendor / relative
            path = unresolved.resolve(strict=True)
            if relative.is_absolute() or not path.is_relative_to(vendor) or unresolved.is_symlink():
                raise RuntimeError("Provenance hors du répertoire vendor")
            data = path.read_bytes()
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise RuntimeError("Les octets vendor ne correspondent pas à la provenance")
            checked += 1
            if record.get("source_url", "").startswith("https://registry.npmjs.org/"):
                package, version = record["package"], record["version"]
                if not re.fullmatch(r"(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+", package) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                    raise RuntimeError("Paquet npm de provenance invalide")
                packages.add((package, version))
    packages = sorted(packages)
    query = {"queries": [{"package": {"name": name, "ecosystem": "npm"}, "version": version}
                         for name, version in packages]}
    osv = post("https://api.osv.dev/v1/querybatch", query)
    if len(osv.get("results", [])) != len(packages):
        raise RuntimeError("Réponse OSV incomplète")
    npm_versions = {}
    for name, version in packages:
        npm_versions.setdefault(name, []).append(version)
    npm = post("https://registry.npmjs.org/-/npm/v1/security/advisories/bulk", npm_versions)
    report = {"schema": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
              "checkedVendorFiles": checked, "packages": [
                  {"name": name, "version": version, "osv": result.get("vulns", []),
                   "npm": npm.get(name, [])} for (name, version), result in zip(packages, osv["results"])],
              "sources": ["https://api.osv.dev/v1/querybatch", "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk"],
              "limits": ["Only exact declared npm versions; no complete embedded transitive SBOM",
                         "Native Tesseract/Leptonica code and traineddata require separate review",
                         "Absence of returned advisories is not proof of absence of vulnerability"]}
    if args.upstream_locks:
        report["upstreamLocks"] = upstream_runtime_pairs()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checkedVendorFiles": checked, "packages": len(packages),
                      "osvFindings": sum(len(record["osv"]) for record in report["packages"]),
                      "npmFindings": sum(len(record["npm"]) for record in report["packages"])}))


if __name__ == "__main__":
    main()
