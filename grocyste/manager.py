"""SPDX-License-Identifier: GPL-3.0-or-later

Signed, bounded packages and atomic addon generations. The manager has no Grocy
credentials and never executes an installation script supplied by a package.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socketserver
import stat
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse
import uuid
import zipfile

from .hostutils import ManagerError, canonical, load_json, atomic_bytes, file_lock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CORE_VERSION = "1.0.0"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_FILES = 10000
ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def validate_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64 or not ID_RE.fullmatch(value):
        raise ManagerError("invalid_target", "Identifiant d'addon invalide")
    return value


def version_tuple(value: object):
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise ManagerError("invalid_version", "Version invalide")
    return tuple(map(int, value.split(".")))


def satisfies(version: str, requirement: str) -> bool:
    v = version_tuple(version)
    if not isinstance(requirement, str) or not requirement or len(requirement) > 128:
        raise ManagerError("invalid_dependency", "Contrainte de version invalide")
    for part in requirement.split():
        match = re.fullmatch(r"(>=|<=|>|<|=)?([0-9]+\.[0-9]+\.[0-9]+)", part)
        if not match:
            raise ManagerError("invalid_dependency", "Contrainte de version non prise en charge")
        op, target = match.group(1) or "=", version_tuple(match.group(2))
        if not {">=": v >= target, "<=": v <= target, ">": v > target,
                "<": v < target, "=": v == target}[op]:
            return False
    return True


def safe_member(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ManagerError("unsafe_path", "Chemin de paquet invalide")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value or ":" in value:
        raise ManagerError("unsafe_path", "Chemin de paquet interdit")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise ManagerError("unsafe_path", "Chemin de paquet interdit")
    if any(p.endswith((".", " ")) for p in path.parts):
        raise ManagerError("unsafe_path", "Chemin de paquet ambigu")
    return value


def public_key_from_file(path: Path) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(path.read_bytes().strip(), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, OSError) as exc:
        raise ManagerError("invalid_trust_root", "Clé publique invalide", 503) from exc


def verify_signed(document: bytes, signature: bytes, key: Ed25519PublicKey):
    value = load_json(document)
    try:
        decoded = base64.b64decode(signature.strip(), validate=True)
        key.verify(decoded, canonical(value))
    except (ValueError, InvalidSignature) as exc:
        raise ManagerError("invalid_signature", "Signature de paquet invalide") from exc
    return value


def validate_manifest(manifest: object, grocy_version: str = "4.7.1") -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ManagerError("invalid_manifest", "Manifeste invalide")
    validate_id(manifest.get("id"))
    version_tuple(manifest.get("version"))
    if not satisfies(CORE_VERSION, manifest.get("core", "")):
        raise ManagerError("incompatible_core", "Version du CORE incompatible")
    if grocy_version not in manifest.get("grocy", []):
        raise ManagerError("incompatible_grocy", "Version de Grocy incompatible")
    deps = manifest.get("dependencies", {})
    if not isinstance(deps, dict) or len(deps) > 32:
        raise ManagerError("invalid_manifest", "Dépendances invalides")
    for identifier, requirement in deps.items():
        validate_id(identifier)
        satisfies("1.0.0", requirement)
    caps = manifest.get("capabilities", [])
    if not isinstance(caps, list) or len(caps) > 128 or any(
        not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", v) for v in caps
    ):
        raise ManagerError("invalid_manifest", "Capacités invalides")
    files = manifest.get("files")
    if not isinstance(files, dict) or len(files) > MAX_FILES or not files:
        raise ManagerError("invalid_manifest", "Liste de fichiers invalide")
    normalized = set()
    for name, entry in files.items():
        safe_member(name)
        folded = name.casefold()
        if folded in normalized or name in ("manifest.json", "manifest.sig"):
            raise ManagerError("invalid_manifest", "Fichier dupliqué ou réservé")
        normalized.add(folded)
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE):
            raise ManagerError("invalid_manifest", "Empreinte de fichier invalide")
    points = manifest.get("entrypoints", {})
    if not isinstance(points, dict):
        raise ManagerError("invalid_manifest", "Points d'entrée invalides")
    for kind, path in points.items():
        if kind not in ("browser", "styles", "cli", "runtime") or path not in files:
            raise ManagerError("invalid_manifest", "Point d'entrée non déclaré")
        if kind == "styles" and not path.endswith(".css"):
            raise ManagerError("invalid_manifest", "Feuille de style CSS requise")
    return manifest


def verify_archive(archive_path: Path, key: Ed25519PublicKey, grocy_version: str = "4.7.1"):
    if archive_path.is_symlink() or archive_path.stat().st_size > MAX_ARCHIVE:
        raise ManagerError("archive_too_large", "Archive interdite ou trop volumineuse", 413)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 2:
                raise ManagerError("archive_too_large", "Trop de fichiers", 413)
            entries, folded, total = {}, set(), 0
            for info in infos:
                # Packages contain files only. This also avoids directories hiding
                # case collisions or replacing a subsequently extracted file.
                name = safe_member(info.filename)
                if info.is_dir() or info.flag_bits & 1:
                    raise ManagerError("unsafe_archive", "Entrée d'archive interdite")
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ManagerError("unsafe_archive", "Lien ou fichier spécial interdit")
                if name.casefold() in folded:
                    raise ManagerError("unsafe_archive", "Entrée d'archive dupliquée")
                folded.add(name.casefold())
                if info.file_size > MAX_FILE or (info.file_size > 1024 * 1024
                    and info.file_size > max(1, info.compress_size) * 100):
                    raise ManagerError("archive_too_large", "Décompression excessive", 413)
                total += info.file_size
                if total > MAX_EXPANDED:
                    raise ManagerError("archive_too_large", "Archive décompressée trop volumineuse", 413)
                entries[name] = info
            if "manifest.json" not in entries or "manifest.sig" not in entries:
                raise ManagerError("missing_manifest", "Manifeste signé absent")
            if entries["manifest.json"].file_size > 1024 * 1024 or entries["manifest.sig"].file_size > 128:
                raise ManagerError("invalid_manifest", "Manifeste trop volumineux")
            manifest = validate_manifest(verify_signed(archive.read("manifest.json"),
                archive.read("manifest.sig"), key), grocy_version)
            if set(entries) != set(manifest["files"]) | {"manifest.json", "manifest.sig"}:
                raise ManagerError("undeclared_file", "Fichier absent du manifeste")
            for name, expected in manifest["files"].items():
                data = archive.read(name)
                if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                    raise ManagerError("checksum_mismatch", "Contenu du paquet altéré")
            return manifest
    except (zipfile.BadZipFile, KeyError, OSError, EOFError) as exc:
        raise ManagerError("invalid_archive", "Archive illisible") from exc


class PackageManager:
    def __init__(self, root: Path, key: Ed25519PublicKey, catalog: Path | None = None,
                 grocy_version: str = "4.7.1"):
        self.root = root.resolve()
        self.key, self.catalog, self.grocy_version = key, catalog, grocy_version
        for sub in ("incoming", "installed", "history", "jobs", "staging"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.registry = self.root / "current.json"
        self.lock = self.root / ".manager.lock"

    def current(self):
        if not self.registry.exists():
            return {"schema": 1, "generation": 0, "addons": {}}
        value = load_json(self.registry.read_bytes())
        if (not isinstance(value, dict) or value.get("schema") != 1
                or type(value.get("generation")) is not int or not isinstance(value.get("addons"), dict)):
            raise ManagerError("corrupt_registry", "État des addons corrompu", 503)
        return value

    def _catalog(self):
        if self.catalog is None or not self.catalog.exists():
            return {"schema": 1, "packages": {}}
        value = verify_signed(self.catalog.read_bytes(), self.catalog.with_suffix(".sig").read_bytes(), self.key)
        if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("packages"), dict):
            raise ManagerError("invalid_catalog", "Catalogue invalide", 503)
        return value

    def available(self, identifier):
        versions = self._catalog()["packages"].get(identifier, {})
        if not isinstance(versions, dict):
            raise ManagerError("invalid_catalog", "Versions de catalogue invalides", 503)
        local = [p.name[len(identifier) + 1:-4] for p in (self.root / "incoming").glob(f"{identifier}-*.zip")]
        return sorted({*versions, *local}, key=version_tuple, reverse=True)

    def _download(self, identifier, version, destination):
        import requests
        record = self._catalog()["packages"].get(identifier, {}).get(version)
        if not isinstance(record, dict):
            raise ManagerError("package_missing", "Paquet absent du catalogue", 404)
        url = record.get("url", "")
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.netloc != "github.com"
                or not re.fullmatch(r"/Raph563/[A-Za-z0-9_-]+/releases/download/[^/]+/[^/]+", parsed.path)
                or parsed.query or parsed.fragment):
            raise ManagerError("invalid_catalog_url", "URL de catalogue interdite")
        expected = record.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ManagerError("invalid_catalog", "Empreinte du catalogue absente")
        allowed = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                   "github-releases.githubusercontent.com"}
        started = time.monotonic()
        client = requests.Session()
        client.trust_env = False
        try:
            for _ in range(6):
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.port not in (None, 443):
                    raise ManagerError("invalid_redirect", "Redirection de paquet interdite")
                response = client.get(url, stream=True, allow_redirects=False, timeout=(5, 30))
                if response.status_code in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin
                    url = urljoin(url, response.headers.get("Location", ""))
                    response.close()
                    continue
                response.raise_for_status()
                fd, temporary = tempfile.mkstemp(prefix="download-", dir=self.root / "staging")
                try:
                    digest, count = hashlib.sha256(), 0
                    with os.fdopen(fd, "wb") as output:
                        for chunk in response.iter_content(65536):
                            count += len(chunk)
                            if count > MAX_ARCHIVE or time.monotonic() - started > 120:
                                raise ManagerError("download_limit", "Téléchargement trop long ou volumineux", 413)
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if digest.hexdigest() != expected:
                        raise ManagerError("checksum_mismatch", "Archive téléchargée altérée")
                    os.replace(temporary, destination)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                    response.close()
                return
            raise ManagerError("invalid_redirect", "Trop de redirections")
        except requests.RequestException as exc:
            raise ManagerError("download_failed", "Téléchargement de paquet impossible", 502) from exc
        finally:
            client.close()

    def _stage(self, identifier, version):
        archive = self.root / "incoming" / f"{identifier}-{version}.zip"
        if not archive.exists():
            self._download(identifier, version, archive)
        manifest = verify_archive(archive, self.key, self.grocy_version)
        if manifest["id"] != identifier or manifest["version"] != version:
            raise ManagerError("wrong_package", "Identité de paquet incorrecte")
        destination = self.root / "installed" / identifier / version
        if destination.exists():
            # Reusing a version is safe only if all signed bytes remain identical.
            if destination.is_symlink() or load_json((destination / "manifest.json").read_bytes()) != manifest:
                raise ManagerError("version_conflict", "Version déjà présente avec un contenu différent", 409)
            for name, metadata in manifest["files"].items():
                path = destination / name
                if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != metadata["sha256"]:
                    raise ManagerError("installed_corrupt", "Paquet installé altéré", 409)
            return manifest, destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="extract-", dir=self.root / "staging"))
        try:
            with zipfile.ZipFile(archive) as source:
                for name in ["manifest.json", "manifest.sig", *manifest["files"]]:
                    output = temporary / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("xb") as stream:
                        stream.write(source.read(name))
                    output.chmod(0o644)
            # Persist every file before the generation can reference it.
            for path in temporary.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return manifest, destination

    def _commit(self, old, new):
        if old["addons"] == new["addons"]:
            return False
        new["generation"] = old["generation"] + 1
        atomic_bytes(self.root / "history" / f"{old['generation']}.json", canonical(old), 0o644)
        atomic_bytes(self.registry, canonical(new), 0o644)
        return True

    def install(self, identifier: str, version: str | None = None, *, expected_generation=None):
        validate_id(identifier)
        if version is not None:
            version_tuple(version)
        with file_lock(self.lock):
            old = self.current()
            if expected_generation is not None and old["generation"] != expected_generation:
                raise ManagerError("generation_conflict", "L'état des addons a changé", 409)
            new = load_json(canonical(old))
            visiting, completed = set(), set()

            def resolve(addon_id, target=None, requirement=None):
                if addon_id in visiting:
                    raise ManagerError("dependency_cycle", "Cycle de dépendances")
                if addon_id in completed:
                    if requirement and not satisfies(new["addons"][addon_id]["version"], requirement):
                        raise ManagerError("dependency_conflict", "Conflit de versions dépendantes")
                    return
                present = new["addons"].get(addon_id)
                if target is None and present and present.get("enabled") and requirement and satisfies(present["version"], requirement):
                    return
                candidates = [target] if target is not None else self.available(addon_id)
                if requirement:
                    candidates = [v for v in candidates if satisfies(v, requirement)]
                if not candidates:
                    raise ManagerError("dependency_missing", "Dépendance ou version introuvable", 409)
                chosen = candidates[0]
                visiting.add(addon_id)
                manifest, directory = self._stage(addon_id, chosen)
                for dependency, constraint in manifest.get("dependencies", {}).items():
                    resolve(dependency, requirement=constraint)
                visiting.remove(addon_id)
                entry = {"version": chosen, "enabled": True, "manifest": manifest,
                         "packageDir": str(directory)}
                previous = old["addons"].get(addon_id)
                if previous and previous["version"] != chosen:
                    entry["previousVersion"] = previous["version"]
                elif previous and previous.get("previousVersion"):
                    entry["previousVersion"] = previous["previousVersion"]
                new["addons"][addon_id] = entry
                completed.add(addon_id)

            resolve(identifier, version)
            # An upgrade must not silently break an already enabled dependant.
            for addon_id, entry in new["addons"].items():
                if not entry.get("enabled"):
                    continue
                for dep, requirement in entry["manifest"].get("dependencies", {}).items():
                    target = new["addons"].get(dep)
                    if not target or not target.get("enabled") or not satisfies(target["version"], requirement):
                        raise ManagerError("dependency_conflict", "Une dépendance active serait incompatible", 409)
            changed = self._commit(old, new)
            return {"status": "installed" if changed else "noop", "generation": new["generation"],
                    "addonId": identifier, "version": new["addons"][identifier]["version"]}

    def disable(self, identifier):
        validate_id(identifier)
        with file_lock(self.lock):
            old = self.current()
            if identifier not in old["addons"]:
                raise ManagerError("unknown_target", "Addon inconnu", 404)
            for name, entry in old["addons"].items():
                if entry.get("enabled") and identifier in entry["manifest"].get("dependencies", {}):
                    raise ManagerError("required_dependency", "Un addon actif utilise cette dépendance", 409)
            new = load_json(canonical(old))
            new["addons"][identifier]["enabled"] = False
            changed = self._commit(old, new)
            return {"status": "disabled" if changed else "noop", "generation": new["generation"]}

    def rollback(self, identifier):
        validate_id(identifier)
        current = self.current()
        previous = current["addons"].get(identifier, {}).get("previousVersion")
        if not previous:
            raise ManagerError("no_previous_version", "Aucune version précédente", 409)
        # install performs the signature, installed-byte and dependency checks.
        return self.install(identifier, previous, expected_generation=current["generation"])


class ManagerHandler(BaseHTTPRequestHandler):
    server_version = "GrocysteManager/1.0"

    def log_message(self, format, *args):
        pass  # No user-controlled bodies, headers or request paths in logs.

    def do_POST(self):
        try:
            if self.path not in ("/v1/install", "/v1/disable", "/v1/rollback"):
                raise ManagerError("not_found", "Opération inconnue", 404)
            if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
                raise ManagerError("invalid_content_type", "JSON requis", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ManagerError("invalid_length", "Taille invalide") from exc
            if not 1 <= length <= 8192:
                raise ManagerError("too_large", "Requête trop volumineuse", 413)
            data = load_json(self.rfile.read(length), 8192)
            if not isinstance(data, dict) or set(data) - {"addonId", "version"}:
                raise ManagerError("invalid_request", "Requête invalide")
            identifier = validate_id(data.get("addonId"))
            action = self.path.rsplit("/", 1)[-1]
            job_id = str(uuid.uuid4())
            job_file = self.server.manager.root / "jobs" / f"{job_id}.json"
            atomic_bytes(job_file, canonical({"id": job_id, "addonId": identifier,
                "action": action, "status": "running", "startedAt": int(time.time())}))
            try:
                result = getattr(self.server.manager, action)(identifier, data.get("version")) if action == "install" else getattr(self.server.manager, action)(identifier)
                result["jobId"] = job_id
                atomic_bytes(job_file, canonical({"id": job_id, "status": "complete", "result": result}))
            except ManagerError as exc:
                atomic_bytes(job_file, canonical({"id": job_id, "status": "failed", "code": exc.code}))
                raise
            self._respond(200, result)
        except ManagerError as exc:
            self._respond(exc.status, {"error": exc.code, "message": str(exc)})
        except Exception:
            self._respond(503, {"error": "manager_unavailable", "message": "Gestionnaire indisponible"})

    def _respond(self, status, data):
        body = canonical(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("PACKAGES_DIR", "/packages")))
    parser.add_argument("--key", type=Path, default=Path(os.environ.get("CATALOG_PUBLIC_KEY", "/trust/catalog.pub")))
    parser.add_argument("--socket", type=Path, default=Path(os.environ.get("MANAGER_SOCKET", "/run/grocyste/manager.sock")))
    parser.add_argument("--catalog", type=Path, default=Path(os.environ.get("SIGNED_CATALOG", "/packages/catalog.signed.json")))
    args = parser.parse_args()
    manager = PackageManager(args.root, public_key_from_file(args.key), args.catalog,
                             os.environ.get("GROCY_VERSION", "4.7.1"))
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    if args.socket.exists():
        if not stat.S_ISSOCK(args.socket.lstat().st_mode):
            raise RuntimeError("Le chemin du socket est occupé")
        args.socket.unlink()
    class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
    with UnixServer(str(args.socket), ManagerHandler) as server:
        server.manager = manager
        os.chmod(args.socket, 0o600)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
