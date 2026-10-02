#!/usr/bin/env python3
"""Qualification against private laboratory fixtures; no production path allowed.

The result contains hashes and outcomes only. Receipts, database and customization
content remain in the laboratory's private security directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grocyste.hostutils import ManagerError
from grocyste.migration import activate, digest, fingerprints, loader, prepare, rollback

LAB = Path("/home/wwadmin/grocyste-work/lab")
LOADER_SHA = "87b88f2ac0ee463a68bbc4ad0fde5921450bee77d4749d939e139f03a45c43e6"
BUDGET_SHA = "37d0902fd23131d30a65e004b9ecf79c11a90716f67332e52c1dc0dc48cc14ab"


def confined(path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(LAB.resolve()) or resolved == LAB:
        raise RuntimeError("Les fixtures et les résultats doivent rester dans le laboratoire")
    return resolved


def schema_hash(database):
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    return digest(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=LAB / "private/migration-source")
    parser.add_argument("--clone", type=Path, default=LAB / "clone/data")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    fixtures, clone = confined(args.fixtures), confined(args.clone)
    work = confined(args.output or LAB / "security" / ("migration-" + uuid.uuid4().hex))
    work.mkdir(parents=True, mode=0o700)
    work.chmod(0o700)
    data = work / "data"
    data.mkdir(mode=0o700)
    originals = {path.name: path.read_bytes() for path in fixtures.iterdir() if path.is_file() and not path.is_symlink()}
    assert digest(originals["custom_js.html"]) == LOADER_SHA
    budget = "courses-u-budget-" + BUDGET_SHA + ".html"
    assert digest(originals[budget]) == BUDGET_SHA
    fixture_hashes = {name: digest(content) for name, content in originals.items()}
    for name, content in originals.items():
        (data / name).write_bytes(content)
        (data / name).chmod(0o600)
    source_db = clone / "grocy.db"
    clone_before = fingerprints(source_db)
    clone_schema = schema_hash(source_db)
    with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as source, sqlite3.connect(data / "grocy.db") as destination:
        source.backup(destination)
        assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    (data / "grocy.db").chmod(0o600)
    for name in ("config.php", "custom_css.html"):
        if (clone / name).is_file() and not (clone / name).is_symlink():
            shutil.copy2(clone / name, data / name)
            (data / name).chmod(0o600)
    if (clone / "storage").is_dir():
        shutil.copytree(clone / "storage", data / "storage")
    before = fingerprints(data / "grocy.db")
    schema_before = schema_hash(data / "grocy.db")
    result = prepare(data, work / "receipts", "/__grocyste")
    receipt = Path(result["receipt"])
    record = json.loads((receipt / "receipt.json").read_bytes())
    assert record["removedBudget"]["sha256"] == BUDGET_SHA
    assert len(record["removedSources"]) == 4
    assert fingerprints(receipt / "grocy.db") == before
    with sqlite3.connect(receipt / "grocy.db") as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    published = activate(receipt)
    assert published["protectedUnchanged"] is True
    active = (data / "custom_js.html").read_bytes()
    assert active.count(loader("/__grocyste")) == 1 and b"courses-u-budget-v1" not in active
    assert fingerprints(data / "grocy.db") == before and schema_hash(data / "grocy.db") == schema_before
    assert activate(receipt)["status"] == "noop"
    assert prepare(data, work / "receipts", "/__grocyste")["status"] == "noop"
    # A user edit must refuse rollback, without touching the new bytes.
    altered = active + b"\n<!-- synthetic user customization -->\n"
    (data / "custom_js.html").write_bytes(altered)
    try:
        rollback(receipt)
        raise AssertionError("Le retour a écrasé une personnalisation")
    except ManagerError as error:
        assert error.code == "loader_drift"
    assert (data / "custom_js.html").read_bytes() == altered
    (data / "custom_js.html").write_bytes(active)
    assert rollback(receipt)["status"] == "rolled-back"
    neutral = (data / "custom_js.html").read_bytes()
    assert loader("/__grocyste") not in neutral
    assert b"window.NerdCore" not in neutral and b"courses-u-budget-v1" not in neutral
    assert rollback(receipt)["status"] == "noop"
    assert fingerprints(data / "grocy.db") == before and schema_hash(data / "grocy.db") == schema_before
    # Backup restoration is exercised only in this isolated copy, not a running
    # Grocy instance; the vulnerable historical loader is never served.
    shutil.copyfile(receipt / "grocy.db", data / "restored-grocy.db")
    assert fingerprints(data / "restored-grocy.db") == before
    assert schema_hash(data / "restored-grocy.db") == schema_before
    assert fingerprints(source_db) == clone_before and schema_hash(source_db) == clone_schema
    assert all(digest((fixtures / name).read_bytes()) == sha for name, sha in fixture_hashes.items())
    report = {"createdAt": datetime.now(timezone.utc).isoformat(), "status": "passed",
        "fixtureLoaderSha256": LOADER_SHA, "fixtureBudgetSha256": BUDGET_SHA,
        "sourceCount": len(record["removedSources"]), "backupIntegrity": "ok",
        "businessFingerprintUnchanged": True, "schemaTriggersViewsUnchanged": True,
        "sourceCloneUnchanged": True, "sourceFixturesUnchanged": True,
        "activation": published["status"], "activationReplay": "noop", "prepareReplay": "noop",
        "rollbackForeignChange": "refused", "rollback": "rolled-back", "rollbackReplay": "noop",
        "databaseRestorationFingerprint": "identical", "legacyNotRestoredToRuntime": True,
        "activeLoaderSha256": digest(active), "neutralLoaderSha256": digest(neutral),
        "protectedTables": sorted(before), "schemaSha256": schema_before}
    (work / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
