#!/usr/bin/env python3
"""SPDX-License-Identifier: GPL-3.0-or-later

Local, reversible installation. Secrets are accepted by protected file or by the
authenticated browser pairing, never shell arguments. No Grocy DB writes.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from grocyste.hostutils import ManagerError, atomic_bytes, canonical, load_json, file_lock
from grocyste.legacy_services import retire
from grocyste.live_migration import migrate as migrate_live
from grocyste.migration import prepare, activate

ADDONS = ("recipe-scaling", "equivalents", "shared-timers", "recipe-live", "budgets", "safe-import",
          "statnerd", "producthelper", "receipt-scanner")


def command(args, *, timeout=600, output=False):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # Do not dump configs, process arguments, HTTP credentials or build logs.
        raise RuntimeError("La commande " + args[0] + " a échoué ; consulter le journal privé local")
    if output:
        return result.stdout
    return None


def directory(path, private=False):
    if path.is_symlink():
        raise RuntimeError("Le répertoire d'installation ne peut pas être un lien")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise RuntimeError("Le chemin d'installation doit être un répertoire")
    path.chmod(0o700 if private else 0o750)
    if os.geteuid() == 0:
        os.chown(path, 1000, 1000)


def owned_bytes(path, content, mode=0o600):
    if path.is_symlink():
        raise RuntimeError("Le fichier d'installation ne peut pas être un lien")
    atomic_bytes(path, content, mode, owner=(1000, 1000) if os.geteuid() == 0 else None)


def private_file(path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Un fichier privé régulier est requis")
    path.chmod(0o600)
    if os.geteuid() == 0:
        os.chown(path, 1000, 1000)


def private_key_bytes(path):
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("Le fichier de clé doit être privé (mode 0600)")
    content = path.read_bytes()
    if not content.strip() or len(content) > 4096:
        raise RuntimeError("Fichier de clé invalide")
    return content


def instance_file(home):
    """Create the file before Docker binds the configuration directory."""
    path = home / "state/config/instance.json"
    previous = home / "state/instance.json"
    if not path.exists():
        if previous.exists():
            if previous.is_symlink() or not previous.is_file():
                raise RuntimeError("L'ancien fichier instance.json n'est pas un fichier régulier")
            content = previous.read_bytes()
            if not isinstance(load_json(content), dict):
                raise RuntimeError("Configuration d'instance invalide")
        else:
            content = canonical({})
        owned_bytes(path, content)
    elif not path.is_file() or not isinstance(load_json(path.read_bytes()), dict):
        raise RuntimeError("Configuration d'instance invalide")
    private_file(path)
    secret = home / "state/secrets/grocy.json"
    if secret.exists():
        private_file(secret)
    return path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_health(url, base_path, timeout=30):
    """Check the exact same-origin API and loader without cookies or credentials."""
    opener = build_opener(ProxyHandler({}), NoRedirect())
    end = time.monotonic() + timeout
    while True:
        try:
            request = Request(url.rstrip("/") + base_path + "/v1/public-config",
                              headers={"Accept": "application/json", "Accept-Encoding": "identity"})
            with opener.open(request, timeout=3) as response:
                if response.status != 200:
                    raise ValueError()
                value = load_json(response.read(1024 * 1024 + 1))
            if (not isinstance(value, dict) or value.get("ok") is not True
                    or value.get("name") != "Grocyste - Seasonings enabler"
                    or value.get("coreVersion") != "1.0.0" or value.get("apiVersion") != 1
                    or value.get("basePath") != base_path or not isinstance(value.get("addons"), list)):
                raise ValueError()
            with opener.open(url.rstrip("/") + base_path + "/assets/core.js", timeout=3) as response:
                script = response.read(16 * 1024 * 1024 + 1)
                if response.status != 200 or script != (ROOT / "web/core.js").read_bytes():
                    raise ValueError()
            return value
        except (HTTPError, URLError, OSError, ValueError, ManagerError):
            if time.monotonic() >= end:
                raise RuntimeError("Le contrôle HTTP Grocyste a échoué ; chargeur non activé") from None
            time.sleep(0.5)


def service_health(compose):
    """Probe manager socket and live bridge from the unprivileged core container."""
    code = """import json,os,socket,time,urllib.request
deadline=time.monotonic()+30
while True:
 try:
  with socket.socket(socket.AF_UNIX) as client:
   client.settimeout(2);client.connect(os.environ['MANAGER_SOCKET'])
  opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  with opener.open(os.environ['LIVE_URL'].rstrip('/')+'/health',timeout=2) as response:
   assert response.status==200 and json.loads(response.read(4096))['status']=='ok'
  break
 except (OSError,ValueError,AssertionError,KeyError):
  if time.monotonic()>=deadline: raise SystemExit(1)
  time.sleep(.5)
"""
    command(compose + ["exec", "-T", "grocyste-core", "python", "-c", code], timeout=40)


def mounted_caddy_file(mounts, host_file):
    matches = []
    target = PurePosixPath("/etc/caddy/Caddyfile")
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "bind":
            continue
        source, destination = mount.get("Source"), mount.get("Destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            continue
        try:
            relative = target.relative_to(PurePosixPath(destination))
        except ValueError:
            continue
        actual = Path(source).joinpath(*relative.parts)
        if actual.resolve() == host_file.resolve():
            matches.append("file" if not relative.parts else "directory")
    if len(matches) != 1:
        raise RuntimeError("Le Caddyfile indiqué ne correspond pas à un montage Docker unique")
    return matches[0]


def caddy_plan(metadata, host_file, container):
    """Resolve one existing service, never infer a project from the current cwd."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise RuntimeError("Nom du conteneur Caddy invalide")
    if metadata.get("Name") != "/" + container or host_file.is_symlink() or not host_file.is_file():
        raise RuntimeError("Conteneur ou fichier Caddy différent de la cible")
    mode = mounted_caddy_file(metadata.get("Mounts", []), host_file)
    if mode == "directory":
        return {"mode": "reload", "container": container}
    manual = {"mode": "manual", "container": container}
    configuration = metadata.get("Config")
    labels = configuration.get("Labels") if isinstance(configuration, dict) else None
    if not isinstance(labels, dict):
        return manual
    prefix = "com.docker.compose."
    project, service = labels.get(prefix + "project"), labels.get(prefix + "service")
    working = labels.get(prefix + "project.working_dir")
    files = labels.get(prefix + "project.config_files")
    if (not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project)
            or not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", service)
            or not isinstance(working, str) or not isinstance(files, str)
            or not isinstance(metadata.get("Id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", metadata["Id"])):
        return manual
    paths = files.split(",")
    environment = labels.get(prefix + "project.environment_file")
    if environment is not None and not isinstance(environment, str):
        return manual
    environment_files = environment.split(",") if environment else []
    if (not Path(working).is_absolute() or not Path(working).is_dir()
            or not paths or any(not path or any(ord(c) < 32 for c in path)
                or not Path(path).is_absolute() or not Path(path).is_file() for path in paths + environment_files)):
        return manual
    if not environment_files:
        project_env = Path(working) / ".env"
        # An explicit file prevents an unrelated .env in the installer's cwd.
        environment_files = [str(project_env) if project_env.is_file() else "/dev/null"]
    compose = ["docker", "compose", "--project-directory", working, "--project-name", project]
    for path in environment_files:
        compose += ["--env-file", path]
    for path in paths:
        compose += ["-f", path]
    return {"mode": "recreate", "container": container, "project": project,
            "service": service, "compose": compose, "oldId": metadata["Id"]}


def inspect_caddy(host_file, container):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise RuntimeError("Nom du conteneur Caddy invalide")
    value = load_json(command(["docker", "inspect", container], output=True, timeout=30).encode())
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError("Inspection du conteneur Caddy ambiguë")
    plan = caddy_plan(value[0], host_file, container)
    if plan["mode"] != "recreate":
        return plan
    try:
        config = load_json(command(plan["compose"] + ["config", "--format", "json"], output=True, timeout=30).encode())
        service = config.get("services", {}).get(plan["service"])
        if (config.get("name") != plan["project"] or not isinstance(service, dict)
                or service.get("container_name", container) != container):
            raise ValueError()
        mounts = [{"Type": row.get("type"), "Source": row.get("source"), "Destination": row.get("target")}
                  for row in service.get("volumes", []) if isinstance(row, dict)]
        if mounted_caddy_file(mounts, host_file) != "file":
            raise ValueError()
        # A replicated service would recreate more than the one requested container.
        ids = command(plan["compose"] + ["ps", "--all", "--quiet", plan["service"]], output=True, timeout=30).split()
        if ids != [plan["oldId"]]:
            raise ValueError()
    except (RuntimeError, ValueError, ManagerError, AttributeError, OSError, subprocess.TimeoutExpired):
        return {"mode": "manual", "container": container}
    return plan


def update_caddy(host_file, container, origin, base_path, home):
    from grocyste.proxy import migrate_caddy
    plan = inspect_caddy(host_file, container)
    original = host_file.read_bytes()
    migrated = migrate_caddy(original, origin, base_path)
    backup = home / "receipts/Caddyfile.before-grocyste"
    if not backup.exists():
        atomic_bytes(backup, original)
    candidate = host_file.with_name(host_file.name + ".grocyste-candidate")
    atomic_bytes(candidate, migrated, 0o600)
    # Validation is always before host persistence and service recreation.
    container_candidate = "/tmp/grocyste-Caddyfile"
    command(["docker", "cp", str(candidate), container + ":" + container_candidate])
    command(["docker", "exec", container, "caddy", "validate", "--config", container_candidate, "--adapter", "caddyfile"])
    if host_file.is_symlink() or host_file.read_bytes() != original:
        raise RuntimeError("Le Caddyfile a changé pendant la validation ; remplacement refusé")
    if migrated != original:
        value = host_file.stat()
        owner = None if os.name == "nt" else (value.st_uid, value.st_gid)
        atomic_bytes(host_file, migrated, value.st_mode & 0o777, owner=owner)
    if plan["mode"] == "recreate":
        command(plan["compose"] + ["up", "-d", "--no-deps", "--no-build", "--pull", "never",
                                   "--force-recreate", plan["service"]])
    elif plan["mode"] == "reload":
        command(["docker", "exec", container, "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
    return plan["mode"]


def main():
    parser = argparse.ArgumentParser(description="Installer Grocyste dans une instance Grocy existante")
    parser.add_argument("--home", type=Path, default=Path("/opt/grocyste"))
    parser.add_argument("--data", type=Path, help="Répertoire data de Grocy, détecté dans le conteneur grocy")
    parser.add_argument("--grocy-url", help="URL interne fixe, détectée pour le conteneur grocy")
    parser.add_argument("--origin", help="Origine HTTPS du site Grocy")
    parser.add_argument("--network", default="grocy_default")
    parser.add_argument("--base-path", default="/__grocyste")
    parser.add_argument("--caddyfile", type=Path, help="Caddyfile existant ; sauvegardé et modifié dans le bloc de ce site seulement")
    parser.add_argument("--caddy-container", default="grocy-caddy")
    parser.add_argument("--packages", type=Path, default=ROOT / "bootstrap-packages")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--admin-key-file", type=Path, help="Fichier privé contenant la clé admin de provisionnement")
    credentials.add_argument("--existing-key-file", type=Path, help="Fichier privé contenant une clé de service déjà créée")
    parser.add_argument("--prepare-only", action="store_true", help="Préparer sauvegarde et configuration sans activation")
    args = parser.parse_args()
    if os.name == "nt":
        raise RuntimeError("Exécuter cette commande sur le serveur Linux Grocy")
    if not shutil.which("docker"):
        raise RuntimeError("Docker avec Compose est requis sur le serveur")
    home = args.home.resolve()
    if home == Path("/") or not home.is_absolute() or any(c in str(home)[len(home.drive):] for c in "\r\n:$"):
        raise RuntimeError("Répertoire d'installation invalide")
    directory(home, private=True)
    with file_lock(home / ".installation.lock"):
        return install(args, home)


def install(args, home):
    data = args.data
    grocy_url = args.grocy_url
    if data is None or grocy_url is None:
        metadata = json.loads(command(["docker", "inspect", "grocy"], output=True))[0]
        mount = next((row for row in metadata["Mounts"] if row["Destination"] == "/config"), None)
        if not mount:
            raise RuntimeError("Répertoire Grocy indétectable ; préciser --data et --grocy-url")
        data = data or Path(mount["Source"]) / "data"
        grocy_url = grocy_url or "http://grocy"
    if not data.is_dir() or not (data / "grocy.db").is_file():
        raise RuntimeError("Répertoire data Grocy invalide")
    parsed = urlsplit(grocy_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or "$" in grocy_url
            or any(ord(c) < 32 for c in grocy_url)):
        raise RuntimeError("URL interne Grocy invalide")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.network):
        raise RuntimeError("Nom de réseau Docker invalide")
    origin = args.origin or input("Origine HTTPS du site Grocy : ").strip()
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", origin):
        raise RuntimeError("Une origine HTTPS sans chemin est requise")
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", args.base_path):
        raise RuntimeError("Chemin Grocyste invalide")
    for relative in ("state", "state/secrets", "state/live", "state/config", "packages", "packages/incoming", "run", "receipts", "trust"):
        directory(home / relative, private=relative.startswith(("state", "receipts")))
    # The live service mounts this directory read-only; atomic updates remain visible.
    (home / "state/config").chmod(0o750)
    instance_file(home)
    if not (ROOT / "trust/catalog.pub").exists():
        raise RuntimeError("Clé publique de la release absente")
    public = home / "trust/catalog.pub"
    if public.exists() and public.read_bytes() != (ROOT / "trust/catalog.pub").read_bytes():
        raise RuntimeError("La clé de confiance a changé ; rotation explicite requise")
    owned_bytes(public, (ROOT / "trust/catalog.pub").read_bytes(), 0o644)
    if args.packages.exists():
        for package in args.packages.glob("*.zip"):
            owned_bytes(home / "packages/incoming" / package.name, package.read_bytes(), 0o644)
    for name in ("catalog.signed.json", "catalog.signed.sig"):
        source = ROOT / name
        if source.exists():
            owned_bytes(home / "packages" / name, source.read_bytes(), 0o644)
        elif not args.prepare_only:
            raise RuntimeError("Catalogue de release signé incomplet")
    values = {"GROCY_URL": grocy_url, "PUBLIC_ORIGIN": origin, "GROCYSTE_HOME": str(home),
              "GROCYSTE_BASE_PATH": args.base_path, "GROCY_DOCKER_NETWORK": args.network}
    env = "".join(key + "=" + json.dumps(value, ensure_ascii=False) + "\n" for key, value in values.items())
    owned_bytes(home / ".env", env.encode(), 0o600)
    if args.prepare_only:
        result = prepare(data.resolve(), home / "receipts", args.base_path)
        print(json.dumps({"status": "prepared", "migration": result}, ensure_ascii=False))
        return
    compose = ["docker", "compose", "--env-file", str(home / ".env"), "-f", str(ROOT / "compose.yaml")]
    command(compose + ["build"])
    common = ["docker", "run", "--rm", "--network", args.network,
              "-v", f"{home}/state:/state", "-v", f"{home}/packages:/packages",
              "-v", f"{home}/trust:/trust:ro", "--user", "1000:1000", "local/grocyste:1.0.0"]
    # Signature verification belongs in the container, never in a host pip install.
    # Version selection uses the signed catalogue even when the local ZIP cache is empty.
    manager_setup = ("from pathlib import Path;import json;"
        "from grocyste.manager import PackageManager,public_key_from_file,version_tuple;"
        "m=PackageManager(Path('/packages'),public_key_from_file(Path('/trust/catalog.pub')),Path('/packages/catalog.signed.json'));"
    )
    selection = manager_setup + "catalog=m._catalog()['packages'];" + (
        f"print(json.dumps({{i:max(catalog[i],key=version_tuple) for i in {ADDONS!r}}}))"
    )
    versions = load_json(command(common + ["python", "-c", selection], output=True).encode("utf-8"))
    if (not isinstance(versions, dict) or set(versions) != set(ADDONS)
            or any(not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
                   for version in versions.values())):
        raise RuntimeError("Versions des addons du catalogue invalides")
    for identifier in ADDONS:
        command(common + ["python", "-c", manager_setup + f"m.install({identifier!r},{versions[identifier]!r})"])
    if args.admin_key_file or args.existing_key_file:
        key_path = (args.admin_key_file or args.existing_key_file).resolve()
        # A root-owned 0600 source is unreadable to uid 1000 in the container.
        # Stage a short-lived copy with service ownership, then always remove it.
        staging = home / "state/secrets" / ("bootstrap-" + uuid.uuid4().hex)
        owned_bytes(staging, private_key_bytes(key_path))
        bootstrap = common[:-1] + ["-v", f"{staging}:/run/bootstrap/key:ro", common[-1],
            "python", "-m", "grocyste.bootstrap", "--url", grocy_url,
            "--secret-file", "/state/secrets/grocy.json", "--instance-file", "/state/config/instance.json",
            "--existing-key-file" if args.existing_key_file else "--admin-key-file", "/run/bootstrap/key"]
        try:
            command(bootstrap)
        finally:
            staging.unlink(missing_ok=True)
        private_file(home / "state/secrets/grocy.json")
        private_file(home / "state/config/instance.json")
    # Copy legacy runtime JSON before replacing its server. The runtime importer
    # validates rows individually and records rejected rows without losing files.
    directory(home / "state/legacy", private=True)
    for name in ("producthelper-receipt-memory.json", "receiptscanner-receipt-memory.json", "producthelper-courseu-import-state.json"):
        source = data / name
        target = home / "state/legacy" / name
        if source.exists() and not target.exists():
            owned_bytes(target, source.read_bytes())
        elif target.exists():
            private_file(target)
    live_migration = migrate_live(home, command)
    command(compose + ["up", "-d"])
    public_health("http://127.0.0.1:8788", args.base_path)
    service_health(compose)
    caddy_action = None
    if args.caddyfile:
        caddy_action = update_caddy(args.caddyfile, args.caddy_container, origin, args.base_path, home)
    else:
        fragment = f"handle {args.base_path}/* {{\n    reverse_proxy grocyste-core:8788\n}}\n"
        owned_bytes(home / "proxy-fragment.caddy", fragment.encode(), 0o644)
        print("Fragment proxy préparé : " + str(home / "proxy-fragment.caddy"))
    # Preparation is intentionally late: builds/provisioning can take minutes and
    # users can change stock meanwhile. Activation still rejects concurrent drift.
    if caddy_action == "manual":
        result = prepare(data.resolve(), home / "receipts", args.base_path)
        print(json.dumps({"status": "prepared-awaiting-caddy-recreate", "origin": origin,
                          "basePath": args.base_path, "migration": result}, ensure_ascii=False))
        print("Métadonnées Compose Caddy insuffisantes : recréer ce conteneur depuis sa configuration réelle puis réexécuter l'installation ; chargeur non activé.")
        return
    proxy_ready = True
    try:
        public_health(origin, args.base_path)
    except RuntimeError:
        if args.caddyfile:
            raise
        proxy_ready = False
    if not proxy_ready:
        result = prepare(data.resolve(), home / "receipts", args.base_path)
        print(json.dumps({"status": "prepared-awaiting-proxy", "origin": origin,
                          "basePath": args.base_path, "migration": result}, ensure_ascii=False))
        print("Configurer le proxy de même origine puis réexécuter l'installation ; chargeur non activé.")
        return
    retirement = retire(data.resolve(), home / "receipts", origin, command)
    # The final snapshot is made after all old writers have stopped.
    result = prepare(data.resolve(), home / "receipts", args.base_path)
    if result.get("receipt"):
        result = {**result, **activate(Path(result["receipt"]))}
    print(json.dumps({"status": "installed", "origin": origin, "basePath": args.base_path,
        "paired": (home / "state/secrets/grocy.json").exists(), "migration": result, "legacy": retirement,
        "liveMigration": live_migration}, ensure_ascii=False))
    if not (home / "state/secrets/grocy.json").exists():
        print("Ouvrir Grocy avec un compte administrateur puis Appairer dans les paramètres Grocyste.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, ManagerError, subprocess.TimeoutExpired) as error:
        print("Installation interrompue : " + str(error))
        raise SystemExit(1)
