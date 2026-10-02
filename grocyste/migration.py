"""SPDX-License-Identifier: GPL-3.0-or-later

One-shot, receipt-backed migration of the customization loader. Grocy business
SQLite is opened read-only for a coherent backup and fingerprints, never edited.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from urllib.parse import quote
import uuid

from .hostutils import ManagerError, atomic_bytes, canonical, file_lock, load_json

PAYLOADS = ("custom_js_nerdcore.html", "custom_js_nerdstats.html",
            "custom_js_product_helper.html", "custom_js_receipt_scanner.html")
HEADERS = (b"<!-- managed by nerdcore-update-api -->\n",
           b"<!-- managed by install.sh (NerdCore) -->\n")
BUSINESS_TABLES = ("stock", "stock_log", "shopping_list", "meal_plan", "recipes",
                  "recipes_pos", "recipes_nestings", "products", "product_barcodes",
                  "quantity_units", "quantity_unit_conversions", "userfield_values")
BUDGET_INCLUDE = re.compile(rb"\n<\?php /\* courses-u-budget-v1 \*/ include '/config/data/courses-u-budget-([a-f0-9]{64})\.html'; \?>\n")
BEGIN = b"<!-- grocyste-loader-v1:begin -->"
END = b"<!-- grocyste-loader-v1:end -->"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def target_metadata(target: Path, data_directory: Path):
    """Keep Grocy's ownership when a privileged installer replaces its loader."""
    if target.is_symlink():
        raise ManagerError("unsafe_target", "Le chargeur ne peut pas être un lien")
    reference = target if target.exists() else next((data_directory / name
        for name in ("config.php", "grocy.db") if (data_directory / name).is_file()
        and not (data_directory / name).is_symlink()), data_directory)
    value = reference.stat()
    return {"mode": target.stat().st_mode & 0o777 if target.exists() else 0o644,
            "uid": getattr(value, "st_uid", None), "gid": getattr(value, "st_gid", None)}


def publish_target(target: Path, data: bytes, metadata: dict):
    if target.is_symlink():
        raise ManagerError("unsafe_target", "Le chargeur ne peut pas être un lien")
    owner = None if os.name == "nt" else (metadata["uid"], metadata["gid"])
    atomic_bytes(target, data, metadata.get("mode", 0o644), owner=owner)


def loader(base_path: str) -> bytes:
    if (not isinstance(base_path, str) or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", base_path)
            or base_path.endswith("/")):
        raise ManagerError("invalid_base_path", "Chemin de chargeur invalide")
    return BEGIN + b"\n" + (
        f'<script src="{base_path}/assets/core.js" defer data-grocyste-loader="1"></script>\n'
    ).encode("ascii") + END + b"\n"


def rewrite(current: bytes, sources: dict[str, bytes], budget_files: dict[str, bytes], base_path: str):
    expected_loader = loader(base_path)
    if current.count(BEGIN) or current.count(END):
        if current.count(BEGIN) != 1 or current.count(END) != 1:
            raise ManagerError("ambiguous_loader", "Chargeur dupliqué ou incomplet", 409)
        a, b = current.index(BEGIN), current.index(END) + len(END)
        found = current[a:b]
        if found != expected_loader.rstrip(b"\n"):
            raise ManagerError("loader_drift", "Le chargeur a été modifié", 409)
        return current, {"status": "noop", "removedSources": [], "removedBudget": None}
    remaining, removed = current, []
    matched = next((header for header in HEADERS if current.startswith(header)), None)
    if matched:
        prefix = matched
        for name in PAYLOADS:
            source = sources.get(name)
            if not source:
                continue
            prefix += f"\n<!-- source: {name} -->\n".encode() + source + b"\n"
            removed.append({"name": name, "sha256": digest(source)})
        if not current.startswith(prefix):
            raise ManagerError("legacy_drift", "Composition NerdCore différente des sources ; réconciliation requise", 409)
        remaining = current[len(prefix):]
    elif any(f"<!-- source: {name} -->".encode() in current for name in PAYLOADS):
        raise ManagerError("ambiguous_legacy", "Composition legacy non reconnue", 409)
    removed_budget = None
    matches = list(BUDGET_INCLUDE.finditer(remaining))
    if b"courses-u-budget-v1" in remaining:
        if len(matches) != 1 or remaining.count(b"courses-u-budget-v1") != 1:
            raise ManagerError("ambiguous_budget", "Inclusion de budget ambiguë", 409)
        match = matches[0]
        filename = "courses-u-budget-" + match[1].decode("ascii") + ".html"
        if filename not in budget_files:
            raise ManagerError("budget_missing", "Source de budget absente", 409)
        if digest(budget_files[filename]) != match[1].decode("ascii"):
            raise ManagerError("budget_drift", "Source de budget altérée", 409)
        removed_budget = {"name": filename, "sha256": digest(budget_files[filename])}
        remaining = remaining[:match.start()] + remaining[match.end():]
    if b"window.NerdCore" in remaining or b"NERDCORE.registerAddon" in remaining:
        raise ManagerError("ambiguous_customization", "Personnalisation legacy restante ; revue nécessaire", 409)
    return remaining + (b"\n" if remaining and not remaining.endswith(b"\n") else b"") + expected_loader, {
        "status": "prepared", "removedSources": removed, "removedBudget": removed_budget,
        "preservedBytes": len(remaining), "preservedSha256": digest(remaining)}


def readonly_db(database: Path):
    return sqlite3.connect("file:" + quote(str(database.resolve()), safe="/") + "?mode=ro", uri=True, timeout=30)


def fingerprints(database: Path):
    result = {}
    with readonly_db(database) as connection:
        connection.execute("BEGIN")
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in BUSINESS_TABLES:
            if table not in existing:
                continue
            rows = []
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                encoded = canonical([{"binarySha256": digest(v)} if isinstance(v, bytes) else v for v in row])
                rows.append(encoded)
            hasher = hashlib.sha256()
            for encoded in sorted(rows):
                hasher.update(len(encoded).to_bytes(8, "big"))
                hasher.update(encoded)
            result[table] = {"sha256": hasher.hexdigest(), "rows": len(rows)}
    return result


def prepare(data_directory: Path, receipt_root: Path, base_path: str):
    data_directory = data_directory.resolve()
    target = data_directory / "custom_js.html"
    if target.is_symlink():
        raise ManagerError("unsafe_target", "Le chargeur ne peut pas être un lien")
    current = target.read_bytes() if target.exists() else b""
    ownership = target_metadata(target, data_directory)
    sources = {name: (data_directory / name).read_bytes() for name in PAYLOADS
               if (data_directory / name).is_file() and not (data_directory / name).is_symlink()}
    budget_files = {}
    for match in BUDGET_INCLUDE.finditer(current):
        name = "courses-u-budget-" + match[1].decode() + ".html"
        path = data_directory / name
        if path.is_file() and not path.is_symlink():
            budget_files[name] = path.read_bytes()
    replacement, metadata = rewrite(current, sources, budget_files, base_path)
    if replacement == current:
        return {"status": "noop", "sha256": digest(current)}
    receipt = receipt_root / str(uuid.uuid4())
    receipt.mkdir(parents=True, mode=0o700)
    receipt.chmod(0o700)
    backups = receipt / "files"
    backups.mkdir(mode=0o700)
    database = data_directory / "grocy.db"
    before = fingerprints(database) if database.exists() else {}
    if database.exists():
        with readonly_db(database) as source, sqlite3.connect(receipt / "grocy.db") as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ManagerError("backup_invalid", "Sauvegarde SQLite invalide", 503)
        (receipt / "grocy.db").chmod(0o600)
        if fingerprints(receipt / "grocy.db") != before:
            raise ManagerError("concurrent_business_change", "Données modifiées pendant la sauvegarde", 409)
    names = {"custom_js.html", "custom_css.html", "config.php", *sources, *budget_files,
             "producthelper-receipt-memory.json", "receiptscanner-receipt-memory.json",
             "producthelper-courseu-import-state.json"}
    saved = {}
    for name in sorted(names):
        path = data_directory / name
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            atomic_bytes(backups / name, content)
            saved[name] = {"sha256": digest(content), "mode": path.stat().st_mode & 0o777}
    # Images are independent files, not SQLite blobs. Save active storage trees;
    # exclude caches/logs/historical backup files from this migration receipt.
    for directory in ("storage", "recipepictures", "productpictures"):
        path = data_directory / directory
        if not path.is_dir() or path.is_symlink():
            continue
        for file in path.rglob("*"):
            if file.is_file() and not file.is_symlink():
                relative = str(file.relative_to(data_directory)).replace("\\", "/")
                content = file.read_bytes()
                atomic_bytes(backups / relative, content)
                saved[relative] = {"sha256": digest(content), "mode": file.stat().st_mode & 0o777}
    if (target.read_bytes() if target.exists() else b"") != current:
        raise ManagerError("concurrent_loader_change", "Le chargeur a changé pendant la préparation", 409)
    atomic_bytes(receipt / "new-custom_js.html", replacement)
    payload = {"schema": 1, "id": receipt.name, "status": "prepared",
        "dataDirectory": str(data_directory), "basePath": base_path,
        "oldSha256": digest(current), "newSha256": digest(replacement),
        "targetExisted": target.exists(), "targetMetadata": ownership,
        "protectedBefore": before, "files": saved, **metadata}
    atomic_bytes(receipt / "receipt.json", canonical(payload))
    return {"status": "prepared", "receipt": str(receipt), "oldSha256": digest(current),
            "newSha256": digest(replacement), "preservedBytes": metadata.get("preservedBytes", 0)}


def activate(receipt_directory: Path):
    receipt_file = receipt_directory / "receipt.json"
    value = load_json(receipt_file.read_bytes())
    data = Path(value["dataDirectory"])
    target = data / "custom_js.html"
    with file_lock(data / ".grocyste-migration.lock"):
        ownership = target_metadata(target, data)
        current = target.read_bytes() if target.exists() else b""
        if digest(current) == value["newSha256"] and value["status"] == "active":
            return {"status": "noop", "receiptId": value["id"]}
        if value["status"] == "activating":
            # A crash may occur on either side of the atomic loader replacement.
            # Reconcile observed bytes and business state instead of blindly writing.
            restored = (receipt_directory / "new-custom_js.html").read_bytes()
            observed = fingerprints(data / "grocy.db") if (data / "grocy.db").exists() else {}
            if digest(restored) != value["newSha256"] or observed != value["protectedBefore"]:
                raise ManagerError("reconciliation_required", "Activation interrompue ; les empreintes exigent une réconciliation", 409)
            if digest(current) == value["newSha256"]:
                value.update(status="active", protectedAfter=observed, reconciledAfterInterruption=True)
                atomic_bytes(receipt_file, canonical(value))
                return {"status": "reconciled", "receiptId": value["id"], "protectedUnchanged": True}
            if digest(current) == value["oldSha256"]:
                value["status"] = "prepared"
                value["reconciledAfterInterruption"] = True
                atomic_bytes(receipt_file, canonical(value))
        if value["status"] != "prepared" or digest(current) != value["oldSha256"]:
            raise ManagerError("loader_drift", "Le chargeur a changé ; activation refusée", 409)
        before = fingerprints(data / "grocy.db") if (data / "grocy.db").exists() else {}
        if before != value["protectedBefore"]:
            raise ManagerError("business_drift", "Les données métier ont changé ; nouvelle préparation requise", 409)
        replacement = (receipt_directory / "new-custom_js.html").read_bytes()
        if digest(replacement) != value["newSha256"]:
            raise ManagerError("receipt_drift", "Reçu de migration altéré", 409)
        value["status"] = "activating"
        atomic_bytes(receipt_file, canonical(value))
        publish_target(target, replacement, value.get("targetMetadata", ownership))
        after = fingerprints(data / "grocy.db") if (data / "grocy.db").exists() else {}
        value["protectedAfter"] = after
        value["status"] = "active" if after == before else "needs-reconciliation"
        atomic_bytes(receipt_file, canonical(value))
        if after != before:
            raise ManagerError("business_drift", "Données modifiées concurremment ; réconciliation requise", 409)
    return {"status": "active", "receiptId": value["id"], "protectedUnchanged": True}


def rollback(receipt_directory: Path):
    receipt_file = receipt_directory / "receipt.json"
    value = load_json(receipt_file.read_bytes())
    data = Path(value["dataDirectory"])
    target = data / "custom_js.html"
    with file_lock(data / ".grocyste-migration.lock"):
        ownership = target_metadata(target, data)
        if value["status"] == "rolled-back":
            return {"status": "noop"}
        if digest(target.read_bytes()) != value["newSha256"]:
            raise ManagerError("loader_drift", "Le chargeur a été modifié ; retour refusé", 409)
        old = receipt_directory / "files" / "custom_js.html"
        original = old.read_bytes() if old.exists() else b""
        if digest(original) != value["oldSha256"]:
            raise ManagerError("backup_drift", "Sauvegarde du chargeur altérée", 409)
        # Initial migration failure returns native Grocy with foreign customization
        # intact, rather than bringing the vulnerable NerdCore runtime back online.
        neutral, _ = rewrite(original, {name: (receipt_directory / "files" / name).read_bytes()
            for name in PAYLOADS if (receipt_directory / "files" / name).exists()},
            {name: (receipt_directory / "files" / name).read_bytes()
             for name in value["files"] if re.fullmatch(r"courses-u-budget-[a-f0-9]{64}\.html", name)},
            value["basePath"])
        block = loader(value["basePath"])
        neutral = neutral.replace(block, b"", 1)
        publish_target(target, neutral, value.get("targetMetadata", ownership))
        value["status"] = "rolled-back"
        value["rollbackSha256"] = digest(neutral)
        atomic_bytes(receipt_file, canonical(value))
    return {"status": "rolled-back", "legacyRouteMustRemainClosed": True}


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    first = commands.add_parser("prepare")
    first.add_argument("--data", required=True, type=Path)
    first.add_argument("--receipts", required=True, type=Path)
    first.add_argument("--base-path", default="/__grocyste")
    for command in ("activate", "rollback"):
        sub = commands.add_parser(command)
        sub.add_argument("receipt", type=Path)
    check = commands.add_parser("fingerprints")
    check.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            value = prepare(args.data, args.receipts, args.base_path)
        elif args.action == "fingerprints":
            value = fingerprints(args.database)
        else:
            value = globals()[args.action](args.receipt)
        print(json.dumps(value, ensure_ascii=False))
    except ManagerError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
