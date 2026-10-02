"""SPDX-License-Identifier: GPL-3.0-or-later"""
import hashlib
from pathlib import Path
import sqlite3
import pytest
from grocyste.manager import ManagerError
from grocyste.migration import (rewrite, loader, prepare, activate, rollback, fingerprints,
                               HEADERS, PAYLOADS, digest)


def legacy():
    sources = {name: f"<script>/* {name} */</script>\n".encode() for name in PAYLOADS}
    budget = b"<script>/* reviewed recipe bundle */</script>"
    sha = digest(budget)
    name = f"courses-u-budget-{sha}.html"
    composed = HEADERS[0] + b"".join(f"\n<!-- source: {name} -->\n".encode() + sources[name] + b"\n" for name in PAYLOADS)
    include = f"\n<?php /* courses-u-budget-v1 */ include '/config/data/{name}'; ?>\n".encode()
    return sources, {name: budget}, composed + include


def test_exact_managed_sources_replaced_foreign_bytes_preserved():
    sources, budgets, current = legacy()
    foreign = b"\n<style>.my-color{color:red}</style>\n<script>window.myOwn=1;</script>\n"
    replacement, metadata = rewrite(current + foreign, sources, budgets, "/__grocyste")
    assert replacement == foreign + loader("/__grocyste")
    assert len(metadata["removedSources"]) == 4
    assert metadata["preservedSha256"] == digest(foreign)
    assert rewrite(replacement, sources, budgets, "/__grocyste")[0] == replacement


def test_modified_managed_or_budget_source_is_not_overwritten():
    sources, budgets, current = legacy()
    sources[PAYLOADS[1]] += b"modified"
    with pytest.raises(ManagerError) as exc:
        rewrite(current, sources, budgets, "/__grocyste")
    assert exc.value.code == "legacy_drift"
    sources, budgets, current = legacy()
    name = next(iter(budgets))
    budgets[name] += b"modified"
    with pytest.raises(ManagerError) as exc:
        rewrite(current, sources, budgets, "/__grocyste")
    assert exc.value.code == "budget_drift"


def test_ambiguous_duplicate_loader_and_unrecognized_core_rejected():
    with pytest.raises(ManagerError):
        rewrite(loader("/__grocyste") * 2, {}, {}, "/__grocyste")
    with pytest.raises(ManagerError):
        rewrite(b"<script>window.NerdCore = myPrivateSettings</script>", {}, {}, "/__grocyste")
    with pytest.raises(ManagerError):
        rewrite(loader("/__grocyste").replace(b"core.js", b"other.js"), {}, {}, "/__grocyste")


@pytest.mark.parametrize("path", ["https://evil.example/script", "/../bad", "/x\"evil", "", "/x/"])
def test_loader_path_cannot_inject_html(path):
    with pytest.raises(ManagerError):
        loader(path)


def data_directory(tmp_path):
    data = tmp_path / "grocy"
    data.mkdir()
    connection = sqlite3.connect(data / "grocy.db")
    connection.executescript("CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT);"
                            "INSERT INTO products VALUES(1,'Produit synthétique');"
                            "CREATE TABLE stock(id INTEGER PRIMARY KEY, amount REAL);"
                            "INSERT INTO stock VALUES(1,2.5);")
    connection.close()
    sources, budgets, current = legacy()
    for name, content in {**sources, **budgets}.items():
        (data / name).write_bytes(content)
    (data / "custom_js.html").write_bytes(current + b"<style>.foreign{color:red}</style>\n")
    (data / "storage" / "recipepictures").mkdir(parents=True)
    (data / "storage" / "recipepictures" / "synthetic.png").write_bytes(b"fixture")
    return data


def test_backup_integrity_activate_replay_and_safe_rollback(tmp_path):
    data = data_directory(tmp_path)
    before = fingerprints(data / "grocy.db")
    result = prepare(data, tmp_path / "receipts", "/__grocyste")
    receipt = Path(result["receipt"])
    assert fingerprints(receipt / "grocy.db") == before
    assert (receipt / "files/storage/recipepictures/synthetic.png").read_bytes() == b"fixture"
    assert activate(receipt)["protectedUnchanged"]
    assert activate(receipt)["status"] == "noop"
    assert fingerprints(data / "grocy.db") == before
    assert prepare(data, tmp_path / "receipts", "/__grocyste")["status"] == "noop"
    assert rollback(receipt)["legacyRouteMustRemainClosed"]
    assert (data / "custom_js.html").read_bytes() == b"<style>.foreign{color:red}</style>\n"
    assert fingerprints(data / "grocy.db") == before


def test_user_change_during_preparation_refuses_activation(tmp_path):
    data = data_directory(tmp_path)
    result = prepare(data, tmp_path / "receipts", "/__grocyste")
    (data / "custom_js.html").write_bytes(b"user customization")
    with pytest.raises(ManagerError) as exc:
        activate(Path(result["receipt"]))
    assert exc.value.code == "loader_drift"
    assert (data / "custom_js.html").read_bytes() == b"user customization"


def test_new_business_change_requires_new_receipt(tmp_path):
    data = data_directory(tmp_path)
    result = prepare(data, tmp_path / "receipts", "/__grocyste")
    with sqlite3.connect(data / "grocy.db") as connection:
        connection.execute("UPDATE stock SET amount=3")
    with pytest.raises(ManagerError) as exc:
        activate(Path(result["receipt"]))
    assert exc.value.code == "business_drift"


def test_rollback_refuses_overwrite_of_user_customization(tmp_path):
    data = data_directory(tmp_path)
    result = prepare(data, tmp_path / "receipts", "/__grocyste")
    receipt = Path(result["receipt"])
    activate(receipt)
    (data / "custom_js.html").write_bytes(b"new customization")
    with pytest.raises(ManagerError):
        rollback(receipt)
    assert (data / "custom_js.html").read_bytes() == b"new customization"
