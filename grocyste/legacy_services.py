"""SPDX-License-Identifier: GPL-3.0-or-later

Retire only the recognized legacy loader writers. This host-only tool keeps
private backups, removes their Compose definitions and their stopped containers.
The previous admin credential consequently has no active verifier. Rollback must
never recreate these services or restore the old public admin route.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler

from .hostutils import ManagerError, atomic_bytes, canonical, file_lock, load_json

SERVICES = ("nerdcore", "nerdstats", "product-helper", "nerdcore-update-api")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strip_services(content: bytes, names: set[str]) -> bytes:
    """Surgical removal; uncommon/ambiguous YAML requires manual reconciliation."""
    lines = content.decode("utf-8").splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == "services:"]
    if len(starts) != 1 or any("\t" in line[:len(line) - len(line.lstrip())] for line in lines):
        raise ManagerError("ambiguous_legacy_compose", "Bloc services Compose non reconnu", 409)
    start = starts[0] + 1
    end = next((i for i in range(start, len(lines)) if lines[i].strip()
                and not lines[i][0].isspace() and not lines[i].startswith("#")), len(lines))
    keys = []
    for i in range(start, end):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#") and line.startswith("  ") and not line.startswith("   "):
            match = re.fullmatch(r"  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*(?:#.*)?", line.rstrip("\r\n"))
            if not match:
                raise ManagerError("ambiguous_legacy_compose", "Service Compose non reconnu", 409)
            keys.append((i, match[1]))
    if len({name for _, name in keys}) != len(keys) or not names <= {name for _, name in keys}:
        raise ManagerError("ambiguous_legacy_compose", "Services absents ou dupliqués", 409)
    removed = set()
    for index, (begin, name) in enumerate(keys):
        stop = keys[index + 1][0] if index + 1 < len(keys) else end
        if name in names:
            removed.update(range(begin, stop))
            continue
        # Remaining services can explicitly depend on the retired writers.
        # Only the plain list form is edited; resolved Compose equality below
        # still proves that every other setting and dependency is preserved.
        for i in range(begin + 1, stop):
            if not re.fullmatch(r"    depends_on:\s*(?:#.*)?", lines[i].rstrip("\r\n")):
                continue
            finish = next((j for j in range(i + 1, stop) if lines[j].strip()
                           and not lines[j].startswith("      ")
                           and not lines[j].lstrip().startswith("#")), stop)
            entries = []
            for j in range(i + 1, finish):
                if not lines[j].strip() or lines[j].lstrip().startswith("#"):
                    continue
                entry = re.fullmatch(r"      - ([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:#.*)?",
                                     lines[j].rstrip("\r\n"))
                if not entry:
                    entries = None
                    break
                entries.append((j, entry[1]))
            if entries is None:
                continue  # Unsupported syntax fails the resolved validation.
            removed.update(j for j, dependency in entries if dependency in names)
            if entries and all(dependency in names for _, dependency in entries):
                removed.add(i)
    return "".join(line for i, line in enumerate(lines) if i not in removed).encode("utf-8")


def remaining_configuration(configuration, names):
    services = {}
    for name, service in configuration["services"].items():
        if name in names:
            continue
        service = dict(service)
        dependencies = service.get("depends_on")
        if isinstance(dependencies, dict):
            kept = {key: value for key, value in dependencies.items() if key not in names}
            if kept:
                service["depends_on"] = kept
            else:
                service.pop("depends_on", None)
        services[name] = service
    return {**configuration, "services": services}


def inspect_optional(command, name):
    # ps distinguishes absence from an inaccessible Docker daemon.
    identifiers = command(["docker", "ps", "--all", "--quiet", "--no-trunc",
                           "--filter", "name=^/" + name + "$"], output=True, timeout=30).split()
    if not identifiers:
        return None
    if len(identifiers) != 1 or not re.fullmatch(r"[a-f0-9]{64}", identifiers[0]):
        raise ManagerError("ambiguous_legacy_service", "Conteneur legacy ambigu", 409)
    rows = load_json(command(["docker", "inspect", identifiers[0]], output=True, timeout=30).encode(), 4 * 1024 * 1024)
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("Name") != "/" + name:
        raise ManagerError("ambiguous_legacy_service", "Conteneur legacy différent", 409)
    return rows[0]


def recognized(container, name, data_directory):
    labels = container.get("Config", {}).get("Labels") or {}
    prefix = "com.docker.compose."
    project, service = labels.get(prefix + "project"), labels.get(prefix + "service")
    working, config = labels.get(prefix + "project.working_dir"), labels.get(prefix + "project.config_files")
    image = container.get("Config", {}).get("Image", "")
    expected_image = image.startswith("nerdstats-updater:") if name != "nerdcore-update-api" else image == "grocy-nerdcore-update-api"
    if (not expected_image or service != name or not isinstance(project, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project)
            or not isinstance(working, str) or not Path(working).is_absolute()
            or not Path(working).is_dir() or not isinstance(config, str) or "," in config
            or not Path(config).is_absolute() or not Path(config).is_file() or Path(config).is_symlink()
            or not any(m.get("Type") == "bind" and m.get("RW") is True
                and Path(m.get("Source", "")).resolve() == data_directory.parent.resolve()
                and m.get("Destination") in {"/grocy-config", "/opt/grocy/config"}
                for m in container.get("Mounts", []))):
        raise ManagerError("unknown_legacy_service", "Service legacy non reconnu ; aucune activation", 409)
    env = labels.get(prefix + "project.environment_file")
    if env is not None and (not isinstance(env, str) or "," in env or not Path(env).is_absolute() or not Path(env).is_file()):
        raise ManagerError("ambiguous_legacy_compose", "Fichier d'environnement Compose ambigu", 409)
    if not env:
        candidate = Path(working) / ".env"
        env = str(candidate) if candidate.is_file() else os.devnull
    compose = ["docker", "compose", "--project-directory", working, "--project-name", project,
               "--env-file", env, "-f", config]
    return {"name": name, "id": container["Id"], "image": image,
            "file": config, "compose": compose, "project": project}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_closed(origin):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(origin.rstrip("/") + "/__nerdcore_update/status", timeout=5) as response:
            status = response.status
    except HTTPError as error:
        status = error.code
    if status != 410:
        raise ManagerError("legacy_route_open", "La route d'administration historique doit répondre 410 avant activation", 503)


def retire(data_directory: Path, receipt_root: Path, origin: str, command, closure=check_closed):
    receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(receipt_root / ".legacy-retirement.lock"):
        directory = receipt_root / "legacy-retirement"
        journal = directory / "receipt.json"
        if journal.exists():
            receipt = load_json(journal.read_bytes())
            if (receipt.get("schema") != 1 or receipt.get("dataDirectory") != str(data_directory.resolve())
                    or receipt.get("origin") != origin or not isinstance(receipt.get("services"), list)):
                raise ManagerError("legacy_receipt_invalid", "Reçu legacy incohérent", 409)
        else:
            services = []
            for name in SERVICES:
                item = inspect_optional(command, name)
                if item:
                    # Other Grocy installations may coexist on this host. Their
                    # loader writers belong to a different data directory.
                    if not any(m.get("Type") == "bind" and m.get("RW") is True
                            and Path(m.get("Source", "")).resolve() == data_directory.resolve().parent
                            for m in item.get("Mounts", [])):
                        continue
                    services.append(recognized(item, name, data_directory.resolve()))
            if not services:
                return {"status": "absent", "services": [], "knownLegacyServicesAbsent": True}
            closure(origin)
            files = {}
            for item in services:
                files.setdefault(item["file"], []).append(item)
            prepared = []
            # Validate every candidate before writing files or stopping a container.
            for filename, items in files.items():
                path = Path(filename)
                original = path.read_bytes()
                if len(original) > 4 * 1024 * 1024:
                    raise ManagerError("too_large", "Configuration Compose trop volumineuse")
                names = {item["name"] for item in items}
                configuration = load_json(command(items[0]["compose"] + ["config", "--format", "json"], output=True).encode(), 4 * 1024 * 1024)
                if configuration.get("name") != items[0]["project"] or not names <= set(configuration.get("services", {})):
                    raise ManagerError("legacy_compose_drift", "Configuration Compose différente des services", 409)
                candidate = strip_services(original, names)
                temporary = path.with_name(path.name + ".grocyste-retirement-candidate")
                try:
                    atomic_bytes(temporary, candidate)
                    invocation = items[0]["compose"][:-1] + [str(temporary), "config", "--format", "json"]
                    verified = load_json(command(invocation, output=True).encode(), 4 * 1024 * 1024)
                    expected = remaining_configuration(configuration, names)
                    if verified != expected:
                        raise ManagerError("legacy_compose_drift", "Retrait Compose non vérifié", 409)
                finally:
                    temporary.unlink(missing_ok=True)
                value = path.stat()
                prepared.append({"path": filename, "before": sha(original), "after": sha(candidate),
                                 "mode": value.st_mode & 0o777, "uid": value.st_uid, "gid": value.st_gid,
                                 "original": original, "candidate": candidate})
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
            files = []
            for index, item in enumerate(prepared):
                atomic_bytes(directory / f"compose-{index}.before", item.pop("original"))
                atomic_bytes(directory / f"compose-{index}.after", item.pop("candidate"))
                files.append({**item, "index": index})
            receipt = {"schema": 1, "dataDirectory": str(data_directory.resolve()), "origin": origin,
                       "status": "prepared", "services": services, "files": files}
            atomic_bytes(journal, canonical(receipt))
        closure(origin)
        for item in receipt["files"]:
            path = Path(item["path"])
            if path.is_symlink() or sha(path.read_bytes()) not in {item["before"], item["after"]}:
                raise ManagerError("legacy_compose_drift", "Compose modifié depuis la préparation", 409)
            candidate = (directory / f"compose-{item['index']}.after").read_bytes()
            if sha(candidate) != item["after"]:
                raise ManagerError("legacy_backup_drift", "Retrait Compose altéré", 409)
            if sha(path.read_bytes()) != item["after"]:
                atomic_bytes(path, candidate, item["mode"],
                    owner=(item["uid"], item["gid"]) if os.name != "nt" else None)
        receipt["status"] = "definitions-retired"
        atomic_bytes(journal, canonical(receipt))
        for item in receipt["services"]:
            container = inspect_optional(command, item["name"])
            if container is None:
                continue
            if container["Id"] != item["id"]:
                raise ManagerError("legacy_container_drift", "Le conteneur legacy a changé ; réconciliation requise", 409)
            command(["docker", "update", "--restart=no", item["id"]], timeout=30)
            command(["docker", "stop", "--time", "20", item["id"]], timeout=30)
            command(["docker", "rm", item["id"]], timeout=30)
            if inspect_optional(command, item["name"]) is not None:
                raise ManagerError("legacy_container_active", "Service legacy toujours présent", 503)
        receipt["status"] = "retired"
        receipt["secretVerifierRemoved"] = True
        receipt["rollbackMustNotRestoreLegacyServices"] = True
        atomic_bytes(journal, canonical(receipt))
        return {"status": "retired", "services": [item["name"] for item in receipt["services"]],
                "secretVerifierRemoved": True, "receipt": str(journal)}
