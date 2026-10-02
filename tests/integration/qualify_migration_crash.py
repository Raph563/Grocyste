#!/usr/bin/env python3
"""Real SIGKILL at both sides of loader publication; private synthetic tmpfs only."""
from datetime import datetime, timezone
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0, "/app")
from grocyste import migration
from grocyste.hostutils import ManagerError


def killed_activation(receipt, after_publication):
    receiver, sender = mp.Pipe(duplex=False)
    def worker():
        original = migration.publish_target
        def publish(path, content, metadata):
            if after_publication:
                original(path, content, metadata)
            sender.send("ready")
            time.sleep(30)
            if not after_publication:
                original(path, content, metadata)
        migration.publish_target = publish
        migration.activate(receipt)
    process = mp.Process(target=worker)
    process.start()
    try:
        assert receiver.poll(15), "migration worker never reached expected fault point"
        assert receiver.recv() == "ready"
        os.kill(process.pid, signal.SIGKILL)
        process.join(5)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        receiver.close()
        sender.close()


def fixture(work, suffix):
    data = work / suffix / "data"
    data.mkdir(parents=True)
    with sqlite3.connect(data / "grocy.db") as database:
        database.executescript("CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT);"
            "INSERT INTO products VALUES(1,'Synthetic migration fixture');"
            "CREATE TABLE stock(id INTEGER PRIMARY KEY,amount REAL);"
            "INSERT INTO stock VALUES(1,7.25);")
    (data / "custom_js.html").write_bytes(b"<style>.synthetic{color:red}</style>\n")
    receipt = Path(migration.prepare(data, work / suffix / "receipts", "/__grocyste")["receipt"])
    return data, receipt


def main():
    work = Path("/work")
    assert os.name == "posix" and os.geteuid() == 1000
    assert work.is_dir() and not any(work.iterdir())
    assert any(line.split()[4] == "/work" and " - tmpfs " in line
               for line in Path("/proc/self/mountinfo").read_text().splitlines())
    mp.set_start_method("fork")
    evidence = []
    for after in (False, True):
        data, receipt = fixture(work, "after" if after else "before")
        protected = migration.fingerprints(data / "grocy.db")
        old = (data / "custom_js.html").read_bytes()
        new = (receipt / "new-custom_js.html").read_bytes()
        killed_activation(receipt, after)
        assert json.loads((receipt / "receipt.json").read_bytes())["status"] == "activating"
        assert (data / "custom_js.html").read_bytes() == (new if after else old)
        assert migration.fingerprints(data / "grocy.db") == protected
        result = migration.activate(receipt)
        assert result["status"] == ("reconciled" if after else "active")
        assert migration.activate(receipt)["status"] == "noop"
        assert migration.fingerprints(data / "grocy.db") == protected
        assert migration.rollback(receipt)["status"] == "rolled-back"
        assert (data / "custom_js.html").read_bytes() == old
        evidence.append({"test": "SIGKILL after loader publication" if after else
                         "SIGKILL before loader publication", "passed": True})
    for drift in ("business", "loader"):
        data, receipt = fixture(work, drift)
        killed_activation(receipt, True)
        if drift == "business":
            with sqlite3.connect(data / "grocy.db") as database:
                database.execute("UPDATE stock SET amount=8.5")
        else:
            (data / "custom_js.html").write_bytes(b"<style>.foreign{color:blue}</style>")
        observed = (data / "custom_js.html").read_bytes()
        try:
            migration.activate(receipt)
        except ManagerError as error:
            assert error.code in {"reconciliation_required", "loader_drift"}
        else:
            raise AssertionError("foreign change was silently overwritten on resume")
        assert (data / "custom_js.html").read_bytes() == observed
        evidence.append({"test": "resume refuses foreign " + drift + " change", "passed": True})
    print(json.dumps({"schema": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
                      "scope": "isolated synthetic SQLite and loaders on uid1000 tmpfs; no network",
                      "passed": len(evidence), "failed": 0, "checks": evidence}))


if __name__ == "__main__":
    main()
