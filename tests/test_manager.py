"""SPDX-License-Identifier: GPL-3.0-or-later"""
import base64
import hashlib
import json
from pathlib import Path
import stat
import zipfile
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from grocyste.manager import (PackageManager, ManagerError, canonical, verify_archive,
                             safe_member, satisfies)


def package(root, key, identifier="sample", version="1.0.0", dependencies=None,
            modifications=None, extra=None):
    data = b"window.demo = true;"
    manifest = {"schema": 1, "id": identifier, "version": version,
        "core": ">=1.0.0 <2.0.0", "grocy": ["4.7.1"],
        "dependencies": dependencies or {}, "capabilities": ["grocy.read"],
        "entrypoints": {"browser": "dist/addon.js"},
        "files": {"dist/addon.js": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}}
    manifest.update(modifications or {})
    path = root / f"{identifier}-{version}.zip"
    with zipfile.ZipFile(path, "w") as out:
        out.writestr("manifest.json", canonical(manifest))
        out.writestr("manifest.sig", base64.b64encode(key.sign(canonical(manifest))))
        out.writestr("dist/addon.js", data)
        for entry in extra or []:
            out.writestr(*entry)
    return path


@pytest.fixture
def manager(tmp_path):
    key = Ed25519PrivateKey.generate()
    service = PackageManager(tmp_path, key.public_key())
    return service, key


def test_signed_package_includes_declared_stylesheet(manager):
    service, key = manager
    script = b"window.demo = true;"
    css = b".grocyste-addon { color: #123; }"
    archive = package(service.root / "incoming", key, modifications={
        "entrypoints": {"browser": "dist/addon.js", "styles": "dist/addon.css"},
        "files": {"dist/addon.js": {"sha256": hashlib.sha256(script).hexdigest(), "size": len(script)},
                  "dist/addon.css": {"sha256": hashlib.sha256(css).hexdigest(), "size": len(css)}}
    }, extra=[("dist/addon.css", css)])
    manifest = verify_archive(archive, key.public_key())
    assert manifest["entrypoints"]["styles"] == "dist/addon.css"
    with zipfile.ZipFile(archive) as package_zip:
        assert package_zip.read("dist/addon.css") == css


def test_install_replay_disable_and_rollback(manager):
    service, key = manager
    package(service.root / "incoming", key)
    assert service.install("sample", "1.0.0")["status"] == "installed"
    assert service.install("sample", "1.0.0")["status"] == "noop"
    package(service.root / "incoming", key, version="1.1.0")
    service.install("sample", "1.1.0")
    assert service.rollback("sample")["version"] == "1.0.0"
    assert service.disable("sample")["status"] == "disabled"
    assert service.disable("sample")["status"] == "noop"


def test_dependency_install_is_atomic_and_cannot_disable_required(manager):
    service, key = manager
    package(service.root / "incoming", key, "main", dependencies={"dep": ">=1.0.0 <2.0.0"})
    with pytest.raises(ManagerError, match="introuvable"):
        service.install("main", "1.0.0")
    assert service.current()["generation"] == 0
    package(service.root / "incoming", key, "dep")
    service.install("main", "1.0.0")
    assert set(service.current()["addons"]) == {"main", "dep"}
    with pytest.raises(ManagerError) as error:
        service.disable("dep")
    assert error.value.code == "required_dependency"


def test_upgrade_cannot_break_other_enabled_addon(manager):
    service, key = manager
    package(service.root / "incoming", key, "dep")
    package(service.root / "incoming", key, "main", dependencies={"dep": "1.0.0"})
    service.install("main", "1.0.0")
    before = service.registry.read_bytes()
    package(service.root / "incoming", key, "dep", "2.0.0")
    with pytest.raises(ManagerError) as exc:
        service.install("dep", "2.0.0")
    assert exc.value.code == "dependency_conflict"
    assert service.registry.read_bytes() == before


@pytest.mark.parametrize("target", ["", "all", "../bad", "Sample", None, 7, "x/y"])
def test_unknown_target_never_expands_to_all(manager, target):
    service, key = manager
    package(service.root / "incoming", key)
    with pytest.raises(ManagerError):
        service.install(target)
    assert service.current()["addons"] == {}


@pytest.mark.parametrize("path", ["../outside", "/root/key", "C:/x", "x\\y", "a/./b", "a//b", "a/../b", "a. ", "a\x00b"])
def test_traversal_and_ambiguous_members_rejected(manager, path):
    service, key = manager
    archive = package(service.root / "incoming", key, extra=[(path, b"attack")])
    with pytest.raises(ManagerError):
        verify_archive(archive, key.public_key())
    assert not (service.root.parent / "outside").exists()


def test_signature_substitution_and_undeclared_file_rejected(manager):
    service, key = manager
    archive = package(service.root / "incoming", Ed25519PrivateKey.generate())
    with pytest.raises(ManagerError) as error:
        verify_archive(archive, key.public_key())
    assert error.value.code == "invalid_signature"
    archive = package(service.root / "incoming", key, extra=[("hidden.js", b"x")])
    with pytest.raises(ManagerError) as error:
        verify_archive(archive, key.public_key())
    assert error.value.code == "undeclared_file"


def test_checksum_mismatch_and_symlink_rejected(manager):
    service, key = manager
    archive = package(service.root / "incoming", key, modifications={"files": {
        "dist/addon.js": {"sha256": "0" * 64, "size": 19}}})
    with pytest.raises(ManagerError) as error:
        verify_archive(archive, key.public_key())
    assert error.value.code == "checksum_mismatch"
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = package(service.root / "incoming", key, extra=[(link, b"../../outside")])
    with pytest.raises(ManagerError) as error:
        verify_archive(archive, key.public_key())
    assert error.value.code == "unsafe_archive"


def test_duplicate_archive_and_dependency_cycle_rejected(manager):
    service, key = manager
    archive = package(service.root / "incoming", key, extra=[("dist/addon.js", b"new")])
    with pytest.raises(ManagerError):
        verify_archive(archive, key.public_key())
    package(service.root / "incoming", key, "a", dependencies={"b": "1.0.0"})
    package(service.root / "incoming", key, "b", dependencies={"a": "1.0.0"})
    with pytest.raises(ManagerError) as exc:
        service.install("a", "1.0.0")
    assert exc.value.code == "dependency_cycle"
    assert service.current()["addons"] == {}


def test_corrupt_registry_never_resets_to_empty(manager):
    service, key = manager
    service.registry.write_text("{bad")
    with pytest.raises(ManagerError):
        service.current()
    assert service.registry.read_text() == "{bad"


def test_existing_installed_bytes_are_reverified(manager):
    service, key = manager
    package(service.root / "incoming", key)
    service.install("sample", "1.0.0")
    current = service.current()
    Path(current["addons"]["sample"]["packageDir"]).joinpath("dist/addon.js").write_text("altered")
    with pytest.raises(ManagerError) as error:
        service.install("sample", "1.0.0")
    assert error.value.code == "installed_corrupt"


def test_version_ranges_and_stale_generation(manager):
    service, key = manager
    assert satisfies("1.3.0", ">=1.0.0 <2.0.0")
    assert not satisfies("2.0.0", ">=1.0.0 <2.0.0")
    package(service.root / "incoming", key)
    with pytest.raises(ManagerError) as error:
        service.install("sample", "1.0.0", expected_generation=1)
    assert error.value.code == "generation_conflict"


def test_interprocess_install_no_lost_generation(manager):
    import multiprocessing
    service, key = manager
    package(service.root / "incoming", key, "one")
    package(service.root / "incoming", key, "two")
    processes = [multiprocessing.Process(target=service.install, args=(name, "1.0.0"))
                 for name in ("one", "two")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert service.current()["generation"] == 2
    assert set(service.current()["addons"]) == {"one", "two"}
