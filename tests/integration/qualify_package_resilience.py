#!/usr/bin/env python3
"""Linux-only fault qualification; /work must be a disposable bounded tmpfs.

No network, Grocy credentials, native database or shared registry is required.
Real SIGKILL, ENOSPC and EACCES failures exercise private synthetic packages.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import errno
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import sys
import time
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, "/app")
from grocyste.hostutils import atomic_bytes, canonical
from grocyste.manager import ManagerError, PackageManager, public_key_from_file, verify_archive
from grocyste.state import State, StateConflict


def archive(root, key, identifier="sample", version="1.0.0", data=None, compress=False):
    data = data if data is not None else os.urandom(32768)
    manifest = {"schema": 1, "id": identifier, "version": version,
                "core": ">=1.0.0 <2.0.0", "grocy": ["4.7.1"],
                "dependencies": {}, "capabilities": ["grocy.read"],
                "entrypoints": {"browser": "dist/addon.js"},
                "files": {"dist/addon.js": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}}
    path = root / f"{identifier}-{version}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED) as output:
        output.writestr("manifest.json", canonical(manifest))
        output.writestr("manifest.sig", base64.b64encode(key.sign(canonical(manifest))))
        output.writestr("dist/addon.js", data)
    return path


def deny(function, code=None):
    try:
        function()
    except ManagerError as error:
        if code is not None:
            assert error.code == code, (error.code, code)
        return error.code
    raise AssertionError("Failure was not refused")


def terminate_before_commit(root, public_key, version=None, during_atomic_publish=False, action="install"):
    receiver, sender = mp.Pipe(duplex=False)
    def worker():
        manager = PackageManager(root, public_key)
        if during_atomic_publish:
            original = os.replace
            def replace(source, destination):
                if Path(destination) == manager.registry:
                    sender.send("ready")
                    time.sleep(30)
                return original(source, destination)
            os.replace = replace
        else:
            def interrupted_commit(old, new):
                sender.send("ready")
                time.sleep(30)
            manager._commit = interrupted_commit
        if action == "uninstall":
            manager.uninstall("sample")
        else:
            manager.install("sample", version)
    process = mp.Process(target=worker)
    process.start()
    try:
        assert receiver.poll(15), "child never reached the intended fault point"
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


def run_workers(function, count=6):
    processes = [mp.Process(target=function, args=(index,)) for index in range(count)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        if process.is_alive():
            process.kill()
            process.join()
        assert process.exitcode == 0, "synthetic worker failed"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signed", type=Path)
    parser.add_argument("--trust", type=Path, default=Path("/app/trust/catalog.pub"))
    args = parser.parse_args()
    assert os.name == "posix" and os.geteuid() == 1000, "run as isolated uid 1000"
    work = Path("/work")
    assert work.is_dir() and not any(work.iterdir()), "/work must be an empty disposable tmpfs"
    # /proc/self/mountinfo is read-only kernel metadata. Refuse a host-directory
    # mistake before creating reservation bytes for the real ENOSPC scenario.
    mount = next((line for line in Path("/proc/self/mountinfo").read_text().splitlines()
                  if line.split()[4] == "/work"), "")
    assert " - tmpfs " in mount, "/work must use tmpfs"
    mp.set_start_method("fork")
    results = []
    key = Ed25519PrivateKey.generate()
    manager = PackageManager(work / "packages", key.public_key())
    incoming = manager.root / "incoming"
    archive(incoming, key)
    manager.install("sample", "1.0.0")

    for version, atomic_phase in (("1.1.0", False), ("1.2.0", True)):
        archive(incoming, key, version=version)
        old = manager.registry.read_bytes()
        generation = manager.current()["generation"]
        terminate_before_commit(manager.root, key.public_key(), version, atomic_phase)
        assert manager.registry.read_bytes() == old
        assert manager.current()["generation"] == generation
        result = manager.install("sample", version)
        assert result["generation"] == generation + 1
        assert manager.install("sample", version)["status"] == "noop"
        results.append({"test": "SIGKILL before atomic registry replacement" if atomic_phase
                        else "SIGKILL after verified extraction", "passed": True})

    for index in range(6):
        archive(incoming, key, identifier=f"parallel-{index}")
    before = manager.current()["generation"]
    def install_worker(index):
        PackageManager(manager.root, key.public_key()).install(f"parallel-{index}", "1.0.0")
    run_workers(install_worker)
    current = manager.current()
    assert current["generation"] == before + 6
    assert len(current["addons"]) == 7
    results.append({"test": "six processes serialize generations without lost addon", "passed": True})

    archive(incoming, key, version="1.3.0")
    before = manager.registry.read_bytes()
    reservation = work / "disk-reservation"
    descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    filled = 0
    try:
        while True:
            filled += os.write(descriptor, b"x" * 65536)
    except OSError as error:
        assert error.errno == errno.ENOSPC
    finally:
        os.close(descriptor)
    try:
        try:
            manager.install("sample", "1.3.0")
        except OSError as error:
            assert error.errno == errno.ENOSPC
        else:
            raise AssertionError("full tmpfs unexpectedly accepted package publication")
        assert manager.registry.read_bytes() == before
        assert manager.current()["addons"]["sample"]["version"] == "1.2.0"
    finally:
        reservation.unlink()
    assert manager.install("sample", "1.3.0")["status"] == "installed"
    results.append({"test": "real ENOSPC preserves old registry and retry succeeds after freeing space",
                    "passed": True, "reservedBytes": filled})

    archive(incoming, key, version="1.4.0")
    before = manager.registry.read_bytes()
    staging = manager.root / "staging"
    staging.chmod(0o500)
    try:
        try:
            manager.install("sample", "1.4.0")
        except PermissionError as error:
            assert error.errno == errno.EACCES
        else:
            raise AssertionError("non-writable staging unexpectedly accepted extraction")
        assert manager.registry.read_bytes() == before
    finally:
        staging.chmod(0o700)
    assert manager.install("sample", "1.4.0")["status"] == "installed"
    results.append({"test": "real EACCES preserves old registry and successful retry", "passed": True})

    before = manager.registry.read_bytes()
    old_generation = manager.current()["generation"]
    terminate_before_commit(manager.root, key.public_key(), during_atomic_publish=True, action="uninstall")
    assert manager.registry.read_bytes() == before
    removed = manager.uninstall("sample")
    assert removed["generation"] == old_generation + 1 and removed["cachedPackagesRetained"]
    assert set(manager.current()["addons"]) == {f"parallel-{index}" for index in range(6)}
    assert deny(lambda: manager.uninstall("all"), "unknown_target")
    assert manager.install("sample", "1.4.0")["generation"] == old_generation + 2
    archive(incoming, key, version="1.5.0")
    assert manager.install("sample", "1.5.0")["generation"] == old_generation + 3
    assert manager.rollback("sample")["version"] == "1.4.0"
    assert manager.current()["generation"] == old_generation + 4
    results.append({"test": "SIGKILL during uninstall preserves generation; targeted removal, verified reinstall and rollback succeed", "passed": True})

    before = manager.registry.read_bytes()
    manager.registry.write_bytes(b"{\"schema\":1,\"schema\":1}")
    damaged = manager.registry.read_bytes()
    assert deny(lambda: manager.install("sample", "1.4.0")) == "invalid_json"
    assert manager.registry.read_bytes() == damaged
    atomic_bytes(manager.registry, before, 0o644)
    results.append({"test": "duplicate-JSON corruption is refused without an empty-registry reset", "passed": True})

    bomb = archive(incoming, key, "compressed", data=b"x" * (2 * 1024 * 1024), compress=True)
    assert deny(lambda: verify_archive(bomb, key.public_key()), "archive_too_large")
    results.append({"test": "signed compression bomb rejected before extraction", "passed": True})

    state = State(work / "state")
    def credential_worker(index):
        State(state.directory).credential(f"synthetic-{index}", f"synthetic-secret-{index}")
    run_workers(credential_worker)
    original_key = (state.directory / "credentials.key").read_bytes()
    for index in range(6):
        assert state.credential(f"synthetic-{index}") == f"synthetic-secret-{index}"
    assert len(original_key) == 44
    assert b"synthetic-secret" not in state.path.read_bytes()
    results.append({"test": "six first-use processes share one complete vault key", "passed": True})
    (state.directory / "credentials.key").write_bytes(b"")
    try:
        state.credential("synthetic-new", "synthetic-secret-new")
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt vault key was silently regenerated")
    assert (state.directory / "credentials.key").read_bytes() == b""
    atomic_bytes(state.directory / "credentials.key", original_key)
    assert state.credential("synthetic-0") == "synthetic-secret-0"
    results.append({"test": "corrupt vault key refuses new writes without replacing existing key", "passed": True})
    (state.directory / "credentials.key").unlink()
    for provider, value in (("synthetic-0", None), ("synthetic-new", "synthetic-secret-new")):
        try:
            state.credential(provider, value)
        except StateConflict:
            pass
        else:
            raise AssertionError("missing key with existing credentials did not fail closed")
    assert not (state.directory / "credentials.key").exists()
    atomic_bytes(state.directory / "credentials.key", original_key)
    assert state.credential("synthetic-0") == "synthetic-secret-0"
    results.append({"test": "missing vault key with existing ciphertext refuses regeneration until explicit restoration", "passed": True})

    def document_worker(index):
        instance = State(state.directory)
        instance.put("synthetic", f"worker-{index}", {"index": index}, quota_documents=7)
        for _ in range(10):
            instance.update("synthetic", "counter", lambda value: value + 1, 0)
    run_workers(document_worker)
    assert state.get("synthetic", "counter")[0] == 60
    results.append({"test": "six concurrent processes perform 60 document updates without loss", "passed": True})

    outcomes = mp.Queue()
    def operation_worker(index):
        try:
            value = State(state.directory).begin_operation(1, "synthetic-operation", {"write": 1})
            assert value is None
            outcomes.put("accepted")
        except StateConflict:
            outcomes.put("refused-pending")
    run_workers(operation_worker)
    statuses = [outcomes.get(timeout=3) for _ in range(6)]
    assert statuses.count("accepted") == 1 and statuses.count("refused-pending") == 5
    state.uncertain_operation(1, "synthetic-operation")
    try:
        state.begin_operation(1, "synthetic-operation", {"write": 1})
    except StateConflict:
        pass
    else:
        raise AssertionError("uncertain mutation was retried blindly")
    results.append({"test": "one of six pending idempotent operations accepted; uncertain replay refused", "passed": True})

    signed = []
    if args.signed:
        trust = public_key_from_file(args.trust)
        archives = sorted(args.signed.glob("*.zip"))
        assert len(archives) == 9, "expected nine public release archives"
        for path in archives:
            manifest = verify_archive(path, trust)
            signed.append({"id": manifest["id"], "version": manifest["version"],
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        sample = min(archives, key=lambda path: path.stat().st_size)
        for part, expected in (("manifest.json", "invalid_signature"), ("payload", "checksum_mismatch")):
            destination = work / f"tampered-{part.replace('.', '-')}.zip"
            with zipfile.ZipFile(sample) as source, zipfile.ZipFile(destination, "w") as output:
                manifest = json.loads(source.read("manifest.json"))
                payload_name = next(iter(manifest["files"]))
                for item in source.infolist():
                    content = source.read(item.filename)
                    if part == "manifest.json" and item.filename == part:
                        modified = dict(manifest)
                        modified["capabilities"] = ["grocy.read", "grocy.write", "credentials"]
                        content = canonical(modified)
                    elif part == "payload" and item.filename == payload_name:
                        content += b"unauthorized"
                    output.writestr(item.filename, content)
            assert deny(lambda: verify_archive(destination, trust), expected)
        results.append({"test": "all nine release archives authenticate; unsigned capability/payload edits refused", "passed": True})
    print(json.dumps({"schema": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
                      "scope": "isolated tmpfs, uid1000, synthetic keys, network disabled",
                      "passed": len(results), "failed": 0, "checks": results,
                      "signedPackages": signed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
