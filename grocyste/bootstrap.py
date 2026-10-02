"""One-time API-only Grocy service provisioning, with masked key fallback.

Grocy 4.7.1 implements API-key creation through its official management UI.
This module never writes Grocy SQLite or patches Grocy PHP.
"""

import argparse
from contextlib import contextmanager
import getpass
from html.parser import HTMLParser
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

from .network import NetworkError, internal_request
from .permissions import admin_permission_id, has_admin_permission
from .state import canonical


class BootstrapError(Exception):
    pass


class KeyParser(HTMLParser):
    def __init__(self, selected):
        super().__init__()
        self.selected = str(selected)
        self.value = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("data-apikey-id") == self.selected and values.get("data-apikey-key"):
            if self.value and self.value != values["data-apikey-key"]:
                raise BootstrapError("La page contient des clés incompatibles ; provisionnement interrompu")
            self.value = values["data-apikey-key"]


def write_secret(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_identity_admin(grocy_url, headers, transport=internal_request):
    result = transport(grocy_url.rstrip("/") + "/api/user", headers=headers)
    if result.status != 200:
        raise BootstrapError("La clé API fournie n’est pas valide")
    try:
        value = result.json()
        value = value[0] if isinstance(value, list) and len(value) == 1 else value
        user_id = int(value["id"])
        permissions = transport(grocy_url.rstrip("/") + f"/api/users/{user_id}/permissions", headers=headers)
        hierarchy = transport(grocy_url.rstrip("/") + "/api/objects/permission_hierarchy", headers=headers)
        if hierarchy.status != 200:
            raise BootstrapError("Impossible de vérifier la hiérarchie des droits Grocy")
        if permissions.status != 200 or not has_admin_permission(permissions.json(), admin_permission_id(hierarchy.json())):
            raise BootstrapError("La clé API fournie doit disposer du droit ADMIN Grocy")
        return user_id
    except (TypeError, KeyError, ValueError) as error:
        raise BootstrapError("L’identité ou les droits renvoyés par Grocy sont invalides") from error


def validate_admin(grocy_url, api_key, transport=internal_request):
    return validate_identity_admin(grocy_url, {"GROCY-API-KEY": api_key, "Accept": "application/json", "Accept-Encoding": "identity"}, transport)


def configure_existing_key(grocy_url, api_key, secret_file, transport=internal_request):
    user_id = validate_admin(grocy_url, api_key, transport)
    write_secret(secret_file, {"apiKey": api_key, "userId": user_id, "keyId": None, "mode": "provided"})
    return {"ok": True, "userId": user_id, "mode": "provided"}


def ensure_timer_entity(grocy_url, api_key, transport=internal_request):
    """Provision addon-owned native metadata only, preserving existing object IDs."""
    validate_admin(grocy_url, api_key, transport)
    base = grocy_url.rstrip("/")
    headers = {"GROCY-API-KEY": api_key, "Accept": "application/json", "Content-Type": "application/json", "Accept-Encoding": "identity"}
    entities = transport(base + "/api/objects/userentities", headers=headers)
    if entities.status != 200 or not isinstance(entities.json(), list):
        raise BootstrapError("Impossible de lire les entités natives Grocy")
    matches = [row for row in entities.json() if row.get("name") == "Mon_Grocy_minuteurs"]
    if len(matches) > 1:
        raise BootstrapError("Plusieurs entités de minuteurs existent ; aucune modification effectuée")
    if matches:
        entity_id = int(matches[0]["id"])
    else:
        response = transport(base + "/api/objects/userentities", method="POST", headers=headers, body=canonical({"name": "Mon_Grocy_minuteurs", "caption": "Minuteurs partagés", "description": "Minuteurs Grocyste et Mon Grocy Android", "show_in_sidebar_menu": 0, "icon_css_class": "fa-solid fa-clock"}).encode())
        if response.status not in {200, 201}:
            raise BootstrapError("La création des métadonnées de minuteurs a échoué")
        entity_id = int(response.json()["created_object_id"])
    fields = transport(base + "/api/objects/userfields", headers=headers)
    if fields.status != 200 or not isinstance(fields.json(), list):
        raise BootstrapError("Impossible de lire les champs natifs Grocy")
    field_matches = [row for row in fields.json() if row.get("entity") == "userentity-Mon_Grocy_minuteurs" and row.get("name") == "Payload"]
    if len(field_matches) > 1 or (field_matches and field_matches[0].get("type") not in {"text-multi-line", "text-single-line"}):
        raise BootstrapError("Le champ Payload existant est incompatible ; données conservées")
    if not field_matches:
        response = transport(base + "/api/objects/userfields", method="POST", headers=headers, body=canonical({"entity": "userentity-Mon_Grocy_minuteurs", "name": "Payload", "caption": "Payload", "type": "text-multi-line", "config": "", "default_value": "", "show_as_column_in_tables": 0, "input_required": 0}).encode())
        if response.status not in {200, 201}:
            raise BootstrapError("La création du champ de minuteurs a échoué")
    return {"sharedTimerEntityId": entity_id}


@contextmanager
def provisioning_lock(secret_file):
    lock_path = Path(secret_file).with_suffix(".provision.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise BootstrapError("Un appairage est déjà en cours ; attendez son résultat") from error
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise BootstrapError("Un appairage est déjà en cours ; attendez son résultat") from error
        locked = True
        yield
    finally:
        if locked and os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def bootstrap_service(grocy_url, admin_key, secret_file, transport=internal_request, admin_cookie=None):
    with provisioning_lock(secret_file):
        return _bootstrap_service(grocy_url, admin_key, secret_file, transport, admin_cookie)


def bootstrap_service_from_cookie(grocy_url, admin_cookie, secret_file, transport=internal_request):
    return bootstrap_service(grocy_url, None, secret_file, transport, admin_cookie=admin_cookie)


def _bootstrap_service(grocy_url, admin_key, secret_file, transport, admin_cookie):
    if Path(secret_file).exists():
        try:
            payload = json.loads(Path(secret_file).read_text(encoding="utf-8"))
            user_id = validate_admin(grocy_url, payload["apiKey"], transport)
            return {"ok": True, "userId": user_id, "mode": payload.get("mode", "service"), "existing": True}
        except (OSError, ValueError, KeyError, BootstrapError):
            raise BootstrapError("La clé de service existante est invalide ; réparez-la avant de recréer un compte")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "Accept-Encoding": "identity"}
    if admin_cookie:
        headers["Cookie"] = admin_cookie
    elif admin_key:
        headers["GROCY-API-KEY"] = admin_key
    else:
        raise BootstrapError("Une session administrateur Grocy est nécessaire")
    validate_identity_admin(grocy_url, headers, transport)
    base = grocy_url.rstrip("/")
    hierarchy = transport(base + "/api/objects/permission_hierarchy", headers=headers)
    try:
        if hierarchy.status != 200:
            raise ValueError()
        admin_id = admin_permission_id(hierarchy.json())
    except (TypeError, ValueError) as error:
        raise BootstrapError("Impossible de résoudre le droit ADMIN Grocy") from error
    username = "grocyste_service"
    listing = transport(base + "/api/users", headers=headers)
    if listing.status != 200:
        raise BootstrapError("Impossible de vérifier les noms des comptes de service")
    if any(row.get("username") == username for row in listing.json()):
        raise BootstrapError("Le compte de service existe déjà sans cette clé ; récupérez sa clé avant de poursuivre")
    password = secrets.token_urlsafe(48)
    created_user_id = None
    created_key_id = None
    try:
        result = transport(base + "/api/users", method="POST", headers=headers,
                           body=canonical({"username": username, "first_name": "Grocyste", "last_name": "Service", "password": password, "picture_file_name": ""}).encode())
        if result.status not in {200, 201, 204}:
            raise BootstrapError("La création du compte de service a été refusée")
        users = transport(base + "/api/users", headers=headers).json()
        matches = [row for row in users if row.get("username") == username]
        if len(matches) != 1:
            raise BootstrapError("Impossible d’identifier avec certitude le compte de service créé")
        created_user_id = int(matches[0]["id"])
        # Read the official key page while the new account has no permissions.
        # Granting ADMIN before this page would expose other users' API keys.
        result = transport(base + f"/api/users/{created_user_id}/permissions", method="PUT", headers=headers, body=b'{"permissions":[]}')
        if result.status not in {200, 204}:
            raise BootstrapError("L’attribution des droits du compte de service a été refusée")
        login = transport(base + "/login", method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", "Accept-Encoding": "identity"},
                          body=urlencode({"username": username, "password": password}).encode())
        cookies = SimpleCookie()
        # Grocy's service login requests no remember-me cookie, so one access cookie suffices.
        cookies.load(login.headers.get("set-cookie", ""))
        cookie = "; ".join(f"{key}={morsel.value}" for key, morsel in cookies.items() if key.startswith("grocy"))
        if login.status not in {302, 303} or not cookie:
            raise BootstrapError("La connexion du service a échoué ; ce mode d’authentification peut nécessiter une clé existante")
        cookie_headers = {"Cookie": cookie, "Accept-Encoding": "identity"}
        identity = transport(base + "/api/user", headers=cookie_headers)
        identity_value = identity.json() if identity.status == 200 else None
        identity_value = identity_value[0] if isinstance(identity_value, list) and len(identity_value) == 1 else identity_value
        if not isinstance(identity_value, dict) or int(identity_value.get("id", 0)) != created_user_id:
            raise BootstrapError("L’identité de la session de service ne correspond pas au compte créé")
        result = transport(base + "/manageapikeys/new?" + urlencode({"description": "Grocyste service — server only"}), headers=cookie_headers)
        location = urlsplit(result.headers.get("location", ""))
        if result.status not in {302, 303} or not location.path.endswith("/manageapikeys"):
            raise BootstrapError("La création officielle de la clé API n’a pas renvoyé la page attendue")
        created_key_id = int(parse_qs(location.query).get("key", [""])[0])
        page = transport(base + f"/manageapikeys?key={created_key_id}", headers=cookie_headers)
        if page.status != 200:
            raise BootstrapError("Impossible de lire la nouvelle clé de service")
        parser = KeyParser(created_key_id)
        parser.feed(page.body.decode("utf-8"))
        if not parser.value:
            raise BootstrapError("La clé de service créée est introuvable dans la page officielle")
        grant = transport(base + f"/api/users/{created_user_id}/permissions", method="PUT", headers=headers, body=canonical({"permissions": [admin_id]}).encode())
        if grant.status not in {200, 204}:
            raise BootstrapError("L’attribution du droit ADMIN au service a échoué")
        if not parser.value or validate_admin(base, parser.value, transport) != created_user_id:
            raise BootstrapError("La vérification d’identité de la nouvelle clé de service a échoué")
        write_secret(secret_file, {"apiKey": parser.value, "userId": created_user_id, "keyId": created_key_id, "mode": "service"})
        # End the bootstrap login; the runtime needs only the API key.
        try:
            transport(base + "/logout", headers=cookie_headers)
        except NetworkError:
            pass  # Credential is committed; a logout network failure must not re-provision.
        return {"ok": True, "userId": created_user_id, "keyId": created_key_id, "mode": "service"}
    except BaseException:
        if not Path(secret_file).exists() and created_user_id is not None:
            try:
                transport(base + f"/api/users/{created_user_id}", method="DELETE", headers=headers)
            except (NetworkError, BootstrapError):
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description="Appairer un compte ADMIN Grocy dont la clé reste sur le serveur")
    parser.add_argument("--url", required=True)
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--existing-key-file", help="Mode 0600 file containing a Grocy admin API key")
    parser.add_argument("--admin-key-file", help="Mode 0600 file containing the provisioning admin API key")
    parser.add_argument("--instance-file", help="Persist the discovered native timer ID alongside existing instance configuration")
    args = parser.parse_args()
    try:
        if args.existing_key_file:
            key = Path(args.existing_key_file).read_text(encoding="utf-8").strip()
            result = configure_existing_key(args.url, key, args.secret_file)
        else:
            key = Path(args.admin_key_file).read_text(encoding="utf-8").strip() if args.admin_key_file else getpass.getpass("Clé API administrateur Grocy (masquée) : ")
            try:
                result = bootstrap_service(args.url, key, args.secret_file)
            except BootstrapError:
                if args.admin_key_file:
                    raise
                print("L’appairage automatique est indisponible. Une clé API ADMIN Grocy peut être configurée directement.")
                fallback = getpass.getpass("Clé API ADMIN Grocy existante (masquée ; vide pour annuler) : ")
                if not fallback:
                    raise BootstrapError("Appairage annulé")
                result = configure_existing_key(args.url, fallback, args.secret_file)
        if args.instance_file:
            secret_payload = json.loads(Path(args.secret_file).read_text(encoding="utf-8"))
            metadata = ensure_timer_entity(args.url, secret_payload["apiKey"])
            instance_path = Path(args.instance_file)
            existing = json.loads(instance_path.read_text(encoding="utf-8")) if instance_path.exists() else {}
            write_secret(instance_path, {**existing, **metadata})
            result.update(metadata)
        print(canonical(result))
    except (BootstrapError, NetworkError, OSError, ValueError) as error:
        # Only curated BootstrapError strings are safe for terminal output.
        print(str(error) if isinstance(error, BootstrapError) else "L’appairage a échoué ; aucune clé n’a été affichée")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
