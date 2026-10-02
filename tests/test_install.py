"""Installer checks on synthetic files only; never run a Docker deployment."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import pytest

from grocyste.hostutils import canonical
from grocyste.migration import activate, prepare, rollback


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("grocyste_host_installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_host_commands_import_without_site_packages():
    for arguments in ([str(ROOT / "scripts/install.py"), "--help"],
                      ["-m", "grocyste.migration", "--help"]):
        result = subprocess.run([sys.executable, "-S", *arguments], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def fake_install(tmp_path, monkeypatch, *, key=False, caddy=False):
    source = tmp_path / "source"
    (source / "trust").mkdir(parents=True)
    (source / "trust/catalog.pub").write_bytes(b"synthetic-public-key")
    (source / "web").mkdir()
    (source / "web/core.js").write_bytes(b"window.syntheticCore=true;")
    (source / "compose.yaml").write_text("services: {}", encoding="utf-8")
    (source / "catalog.signed.json").write_bytes(canonical({"schema": 1, "packages": {
        identifier: {"1.0.0": {"url": "https://github.com/Raph563/Synthetic/releases/download/v1/fixture.zip"}}
        for identifier in installer.ADDONS}}))
    (source / "catalog.signed.sig").write_bytes(b"synthetic-signature")
    data = tmp_path / "grocy"
    data.mkdir()
    original = b"<style>.synthetic-foreign{color:red}</style>\n"
    (data / "custom_js.html").write_bytes(original)
    with sqlite3.connect(data / "grocy.db") as connection:
        connection.executescript("CREATE TABLE stock(id INTEGER,amount REAL);INSERT INTO stock VALUES(1,1);")
    home = tmp_path / "home"
    monkeypatch.setattr(installer, "ROOT", source)
    # Keep the actual platform's pathlib implementation and atomic helpers.
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="posix", geteuid=lambda: 1000))
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/usr/bin/docker")
    arguments = ["install.py", "--home", str(home), "--data", str(data),
                 "--grocy-url", "http://grocy", "--origin", "https://grocy.example"]
    events = []
    caddy_metadata = None
    if key:
        key_path = tmp_path / "admin-key"
        key_path.write_bytes(b"synthetic-private-bootstrap-key")
        key_path.chmod(0o600)
        arguments += ["--admin-key-file", str(key_path)]
        if os.name == "nt":
            # Windows cannot represent POSIX file modes; real checks run on Linux.
            monkeypatch.setattr(installer, "private_key_bytes", lambda path: path.read_bytes())
    if caddy:
        caddy_path = tmp_path / "Caddyfile"
        caddy_path.write_bytes(b"grocy.example {\n reverse_proxy grocy:80\n}\n")
        arguments += ["--caddyfile", str(caddy_path)]
        caddy_metadata = {"Name": "/grocy-caddy", "Id": "c" * 64,
            "Mounts": [{"Type": "bind", "Source": str(caddy_path), "Destination": "/etc/caddy/Caddyfile"}],
            "Config": {"Labels": {"com.docker.compose.project": "synthetic",
                "com.docker.compose.service": "caddy", "com.docker.compose.project.working_dir": str(source),
                "com.docker.compose.project.config_files": str(source / "compose.yaml")}}}
    monkeypatch.setattr(sys, "argv", arguments)

    def command(args, **kwargs):
        if args[-1] == "build":
            events.append("build")
            command.projects.append(args[args.index("--project-name") + 1])
            # Normal user activity while the image builds must not stale the final receipt.
            with sqlite3.connect(data / "grocy.db") as connection:
                connection.execute("UPDATE stock SET amount=2")
        elif args[-2:] == ["up", "-d"]:
            assert (home / "state/config/instance.json").is_file()
            assert json.loads((home / "state/config/instance.json").read_bytes()) == {}
            events.append("up")
        elif "grocyste.bootstrap" in args:
            events.append("bootstrap")
            staged = [argument for argument in args if argument.endswith(":/run/bootstrap/key:ro")][0]
            staging = Path(staged.removesuffix(":/run/bootstrap/key:ro"))
            assert staging.read_bytes() == b"synthetic-private-bootstrap-key"
            assert args[args.index("--instance-file") + 1] == "/state/config/instance.json"
            (home / "state/secrets/grocy.json").write_bytes(canonical({"apiKey": "synthetic-service-key"}))
        elif args[:2] == ["docker", "ps"]:
            return ""
        elif args[1] == "inspect":
            return json.dumps([caddy_metadata])
        elif args[-3:] == ["config", "--format", "json"]:
            return json.dumps({"name": "synthetic", "services": {"caddy": {"container_name": "grocy-caddy",
                "volumes": [{"type": "bind", "source": str(tmp_path / "Caddyfile"), "target": "/etc/caddy/Caddyfile"}]}}})
        elif "ps" in args:
            return caddy_metadata["Id"] + "\n"
        elif "--force-recreate" in args:
            events.append("caddy-recreate")
            command.recreations.append(args)
        elif kwargs.get("output"):
            assert "m._catalog()['packages']" in args[-1]
            return json.dumps({identifier: "1.0.0" for identifier in installer.ADDONS})
        elif "m.install(" in args[-1]:
            assert ",'1.0.0')" in args[-1]
            command.installations.append(next(identifier for identifier in installer.ADDONS
                if f"m.install({identifier!r},'1.0.0')" in args[-1]))
        return None

    command.installations = []
    command.recreations = []
    command.projects = []

    monkeypatch.setattr(installer, "command", command)
    monkeypatch.setattr(installer, "public_health", lambda url, base_path:
                        events.append("health-internal" if url.startswith("http://127.") else "health-public"))
    monkeypatch.setattr(installer, "service_health", lambda _: events.append("health-services"))
    real_prepare, real_activate = installer.prepare, installer.activate

    def tracked_prepare(*args):
        events.append("prepare")
        return real_prepare(*args)

    def tracked_activate(*args):
        events.append("activate")
        return real_activate(*args)

    monkeypatch.setattr(installer, "prepare", tracked_prepare)
    monkeypatch.setattr(installer, "activate", tracked_activate)
    return home, data, original, events


def test_final_prepare_after_build_and_health_before_activation(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch)
    installer.main()
    assert events == ["build", "up", "health-internal", "health-services", "health-public", "prepare", "activate"]
    assert installer.command.installations == list(installer.ADDONS)
    assert not list((home / "packages/incoming").glob("*.zip"))
    with sqlite3.connect(data / "grocy.db") as connection:
        assert connection.execute("SELECT amount FROM stock").fetchone() == (2.0,)
    receipt = next((home / "receipts").glob("*/receipt.json"))
    value = json.loads(receipt.read_bytes())
    assert value["status"] == "active" and value["protectedBefore"] == value["protectedAfter"]
    assert (data / "custom_js.html").read_bytes().startswith(original)
    assert '"status": "installed"' in capsys.readouterr().out


def test_internal_health_failure_never_prepares_or_activates_loader(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch)
    monkeypatch.setattr(installer, "public_health", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    with pytest.raises(RuntimeError):
        installer.main()
    assert events == ["build", "up"]
    assert (data / "custom_js.html").read_bytes() == original
    assert not list((home / "receipts").glob("*/receipt.json"))


def test_relocated_release_reuses_the_same_compose_project_and_loader(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch)
    installer.main()
    before = (data / "custom_js.html").read_bytes()
    moved = tmp_path / "different-release-folder"
    installer.shutil.copytree(installer.ROOT, moved)
    monkeypatch.setattr(installer, "ROOT", moved)
    installer.main()
    assert len(installer.command.projects) == 2
    assert installer.command.projects[0] == installer.command.projects[1]
    assert (data / "custom_js.html").read_bytes() == before


def test_unconfigured_proxy_prepares_only_and_preserves_loader(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch)

    def health(url, base_path):
        events.append("health-internal" if url.startswith("http://127.") else "health-public")
        if url.startswith("https:"):
            raise RuntimeError("synthetic absent route")

    monkeypatch.setattr(installer, "public_health", health)
    installer.main()
    assert events[-1] == "prepare" and "activate" not in events
    assert (data / "custom_js.html").read_bytes() == original
    value = json.loads(next((home / "receipts").glob("*/receipt.json")).read_bytes())
    assert value["status"] == "prepared"
    output = capsys.readouterr().out
    assert '"status": "prepared-awaiting-proxy"' in output and '"status": "installed"' not in output


def test_configured_proxy_health_failure_refuses_activation(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)

    def health(url, base_path):
        if url.startswith("https:"):
            raise RuntimeError("synthetic proxy failure")

    monkeypatch.setattr(installer, "public_health", health)
    with pytest.raises(RuntimeError):
        installer.main()
    assert "prepare" not in events and "activate" not in events
    assert "caddy-recreate" in events
    assert (data / "custom_js.html").read_bytes() == original


def test_caddy_file_bind_recreates_only_known_service_then_checks_https(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    installer.main()
    assert events.index("caddy-recreate") < events.index("health-public") < events.index("prepare")
    assert len(installer.command.recreations) == 1
    arguments = installer.command.recreations[0]
    assert arguments[:2] == ["docker", "compose"]
    assert arguments[arguments.index("--project-name") + 1] == "synthetic"
    assert arguments[arguments.index("--project-directory") + 1] == str(installer.ROOT)
    assert arguments[arguments.index("-f") + 1] == str(installer.ROOT / "compose.yaml")
    assert arguments[-8:] == ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "caddy"]
    assert "--remove-orphans" not in arguments and "grocy" not in arguments
    assert b"reverse_proxy grocyste-core:8788" in (tmp_path / "Caddyfile").read_bytes()
    assert (home / "receipts/Caddyfile.before-grocyste").read_bytes() == b"grocy.example {\n reverse_proxy grocy:80\n}\n"
    assert '"status": "installed"' in capsys.readouterr().out


def test_caddy_missing_compose_labels_reports_manual_and_keeps_loader(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command

    def command(args, **kwargs):
        if args[1] == "inspect":
            value = json.loads(original_command(args, **kwargs))
            value[0]["Config"]["Labels"] = {}
            return json.dumps(value)
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    installer.main()
    assert "caddy-recreate" not in events and "activate" not in events and "health-public" not in events
    assert (data / "custom_js.html").read_bytes() == original
    assert b"reverse_proxy grocyste-core:8788" in (tmp_path / "Caddyfile").read_bytes()
    assert '"status": "prepared-awaiting-caddy-recreate"' in capsys.readouterr().out


def test_caddy_replay_accepts_only_its_empty_environment_sentinel(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command

    def command(args, **kwargs):
        if args[1] == "inspect":
            value = json.loads(original_command(args, **kwargs))
            value[0]["Config"]["Labels"]["com.docker.compose.project.environment_file"] = "/dev/null"
            return json.dumps(value)
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    installer.main()
    assert "caddy-recreate" in events and "activate" in events
    assert '"status": "installed"' in capsys.readouterr().out


def test_caddy_replay_refuses_other_environment_devices(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command

    def command(args, **kwargs):
        if args[1] == "inspect":
            value = json.loads(original_command(args, **kwargs))
            value[0]["Config"]["Labels"]["com.docker.compose.project.environment_file"] = "/dev/random"
            return json.dumps(value)
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    installer.main()
    assert "caddy-recreate" not in events and "activate" not in events
    assert (data / "custom_js.html").read_bytes() == original
    assert '"status": "prepared-awaiting-caddy-recreate"' in capsys.readouterr().out


def test_caddy_recreation_failure_leaves_no_active_loader(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command

    def command(args, **kwargs):
        if "--force-recreate" in args:
            raise RuntimeError("synthetic failed recreation")
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    with pytest.raises(RuntimeError):
        installer.main()
    assert "prepare" not in events and "activate" not in events
    assert (data / "custom_js.html").read_bytes() == original
    assert '"status": "installed"' not in capsys.readouterr().out


def test_caddy_invalid_candidate_is_not_persisted(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command
    before = (tmp_path / "Caddyfile").read_bytes()

    def command(args, **kwargs):
        if "validate" in args:
            raise RuntimeError("synthetic invalid caddy")
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    with pytest.raises(RuntimeError):
        installer.main()
    assert (tmp_path / "Caddyfile").read_bytes() == before
    assert (data / "custom_js.html").read_bytes() == original
    assert "caddy-recreate" not in events


def test_caddy_directory_bind_reloads_without_recreating(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command
    reloads = []

    def command(args, **kwargs):
        if args[1] == "inspect":
            value = json.loads(original_command(args, **kwargs))
            value[0]["Mounts"] = [{"Type": "bind", "Source": str(tmp_path), "Destination": "/etc/caddy"}]
            value[0]["Config"]["Labels"] = {}
            return json.dumps(value)
        if "reload" in args:
            reloads.append(args)
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    installer.main()
    assert "caddy-recreate" not in events and "activate" in events
    assert len(reloads) == 1 and reloads[0][reloads[0].index("--config") + 1] == "/etc/caddy/Caddyfile"


def test_caddy_multiple_replicas_refuses_automatic_recreation(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command

    def command(args, **kwargs):
        if args[:2] == ["docker", "compose"] and "ps" in args:
            return "c" * 64 + "\n" + "d" * 64 + "\n"
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    installer.main()
    assert "caddy-recreate" not in events and "activate" not in events
    assert (data / "custom_js.html").read_bytes() == original
    assert '"status": "prepared-awaiting-caddy-recreate"' in capsys.readouterr().out


def test_caddy_foreign_mount_is_never_changed(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch, caddy=True)
    original_command = installer.command
    before = (tmp_path / "Caddyfile").read_bytes()

    def command(args, **kwargs):
        if args[1] == "inspect":
            value = json.loads(original_command(args, **kwargs))
            value[0]["Mounts"][0]["Source"] = str(tmp_path / "foreign-Caddyfile")
            return json.dumps(value)
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    with pytest.raises(RuntimeError, match="montage"):
        installer.main()
    assert (tmp_path / "Caddyfile").read_bytes() == before
    assert (data / "custom_js.html").read_bytes() == original


def test_bootstrap_key_staging_is_removed_and_secret_is_not_output(tmp_path, monkeypatch, capsys):
    home, data, original, events = fake_install(tmp_path, monkeypatch, key=True)
    installer.main()
    assert events.index("bootstrap") < events.index("up")
    assert not list((home / "state/secrets").glob("bootstrap-*"))
    output = capsys.readouterr().out
    assert "synthetic-private-bootstrap-key" not in output and "synthetic-service-key" not in output


def test_failed_bootstrap_also_removes_staged_key(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch, key=True)
    original_command = installer.command

    def command(args, **kwargs):
        if "grocyste.bootstrap" in args:
            raise RuntimeError("synthetic bootstrap failure")
        return original_command(args, **kwargs)

    monkeypatch.setattr(installer, "command", command)
    with pytest.raises(RuntimeError):
        installer.main()
    assert not list((home / "state/secrets").glob("bootstrap-*"))
    assert (data / "custom_js.html").read_bytes() == original
    assert "up" not in events


def test_missing_signed_catalog_refuses_build_and_activation(tmp_path, monkeypatch):
    home, data, original, events = fake_install(tmp_path, monkeypatch)
    (installer.ROOT / "catalog.signed.sig").unlink()
    with pytest.raises(RuntimeError, match="Catalogue"):
        installer.main()
    assert events == [] and (data / "custom_js.html").read_bytes() == original


@pytest.mark.parametrize("problem", [None, "redirect", "api-version", "loader-bytes"])
def test_real_public_health_validates_api_and_exact_loader(tmp_path, monkeypatch, problem):
    source = tmp_path / "source"
    (source / "web").mkdir(parents=True)
    script = b"window.syntheticCore=true;"
    (source / "web/core.js").write_bytes(script)
    monkeypatch.setattr(installer, "ROOT", source)
    visits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visits.append(self.path)
            if problem == "redirect":
                self.send_response(302)
                self.send_header("Location", "/redirect-must-not-be-followed")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            if self.path.endswith("public-config"):
                self.wfile.write(canonical({"ok": True, "name": "Grocyste - Seasonings enabler",
                    "coreVersion": "1.0.0", "apiVersion": 2 if problem == "api-version" else 1,
                    "basePath": "/__grocyste", "addons": []}))
            else:
                self.wfile.write(b"corrupt" if problem == "loader-bytes" else script)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        if problem:
            with pytest.raises(RuntimeError, match="non activé"):
                installer.public_health(url, "/__grocyste", timeout=0)
        else:
            assert installer.public_health(url, "/__grocyste", timeout=5)["ok"]
        assert "/redirect-must-not-be-followed" not in visits
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_instance_configuration_moves_legacy_copy_without_losing_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="posix", geteuid=lambda: 1000))
    home = tmp_path / "home"
    (home / "state/config").mkdir(parents=True)
    (home / "state/secrets").mkdir()
    metadata = canonical({"sharedTimerEntityId": 17, "familyProfiles": [{"name": "Synthetic"}]})
    previous = home / "state/instance.json"
    previous.write_bytes(metadata)
    path = installer.instance_file(home)
    assert path.read_bytes() == metadata and previous.read_bytes() == metadata
    assert installer.instance_file(home).read_bytes() == metadata


def test_instance_configuration_directory_and_symlink_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="posix", geteuid=lambda: 1000))
    home = tmp_path / "home"
    (home / "state/config/instance.json").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        installer.instance_file(home)


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() != 0,
                    reason="Vérification des propriétaires réservée au conteneur Linux de test root")
def test_linux_instance_and_secret_owner_is_service_uid(tmp_path):
    home = tmp_path / "home"
    for relative in ("state", "state/config", "state/secrets"):
        installer.directory(home / relative, private=True)
    secret = home / "state/secrets/grocy.json"
    secret.write_bytes(canonical({"apiKey": "synthetic-key"}))
    path = installer.instance_file(home)
    for item in (path, secret):
        value = item.stat()
        assert (value.st_uid, value.st_gid, value.st_mode & 0o777) == (1000, 1000, 0o600)


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() != 0,
                    reason="Vérification des propriétaires réservée au conteneur Linux de test root")
def test_linux_migration_preserves_grocy_loader_owner_and_mode(tmp_path):
    data = tmp_path / "grocy"
    data.mkdir()
    target = data / "custom_js.html"
    target.write_bytes(b"<style>.synthetic{color:red}</style>\n")
    target.chmod(0o640)
    os.chown(target, 1234, 2345)
    receipt = Path(prepare(data, tmp_path / "receipts", "/__grocyste")["receipt"])
    for operation in (activate, rollback):
        operation(receipt)
        value = target.stat()
        assert (value.st_uid, value.st_gid, value.st_mode & 0o777) == (1234, 2345, 0o640)
