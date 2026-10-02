"""Authenticated Grocy companion. No host shell, Docker socket or Grocy DB writes."""

import base64
from datetime import datetime, timezone
from functools import wraps
import hashlib
import http.client
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import socket
from io import BytesIO
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from . import __version__
from .bootstrap import BootstrapError, bootstrap_service_from_cookie, ensure_timer_entity, write_secret
from .network import HttpResult, NetworkError, external_request, internal_request
from .messages import public_message
from .permissions import admin_permission_id, has_admin_permission
from .pictures import PictureError, validate_picture
from .state import State, StateConflict, canonical

ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PROVIDERS = {
    "openai": ("api.openai.com", "Authorization", "Bearer "),
    "gemini": ("generativelanguage.googleapis.com", "x-goog-api-key", ""),
    "anthropic": ("api.anthropic.com", "x-api-key", ""),
    "cohere": ("api.cohere.com", "Authorization", "Bearer "),
    "mistral": ("api.mistral.ai", "Authorization", "Bearer "),
    "groq": ("api.groq.com", "Authorization", "Bearer "),
    "together": ("api.together.xyz", "Authorization", "Bearer "),
    "openrouter": ("openrouter.ai", "Authorization", "Bearer "),
    "deepseek": ("api.deepseek.com", "Authorization", "Bearer "),
    "xai": ("api.x.ai", "Authorization", "Bearer "),
    "perplexity": ("api.perplexity.ai", "Authorization", "Bearer "),
    "fireworks": ("api.fireworks.ai", "Authorization", "Bearer "),
    "huggingface": ("router.huggingface.co", "Authorization", "Bearer "),
    "cerebras": ("api.cerebras.ai", "Authorization", "Bearer "),
    "github-models": ("models.inference.ai.azure.com", "Authorization", "Bearer "),
}
PROVIDER_ROUTES = {
    "openai": {"GET": r"/v1/models(?:/[A-Za-z0-9_.:-]+)?", "POST": r"/v1/(?:chat/completions|responses|embeddings)"},
    "gemini": {"GET": r"/v1(?:beta)?/models(?:/[A-Za-z0-9_.-]+)?", "POST": r"/v1(?:beta)?/models/[A-Za-z0-9_.-]+:(?:generateContent|countTokens)"},
    "anthropic": {"GET": r"/v1/models(?:/[A-Za-z0-9_.:-]+)?", "POST": r"/v1/messages(?:/count_tokens)?"},
    "cohere": {"GET": r"/v[12]/models", "POST": r"/v[12]/chat"},
    "groq": {"GET": r"/openai/v1/models", "POST": r"/openai/v1/chat/completions"},
    "openrouter": {"GET": r"/api/v1/models", "POST": r"/api/v1/chat/completions"},
    "perplexity": {"GET": r"/models", "POST": r"/chat/completions"},
    "fireworks": {"GET": r"/inference/v1/models", "POST": r"/inference/v1/chat/completions"},
    "github-models": {"GET": r"/models", "POST": r"/chat/completions"},
}
DEFAULT_EXTERNAL_HOSTS = {
    "world.openfoodfacts.org", "world.openproductsfacts.org", "images.openfoodfacts.org",
    "static.openfoodfacts.org", "static.openproductsfacts.org", "images.openproductsfacts.org",
    "api.github.com", "raw.githubusercontent.com", "coursesu.com", "www.coursesu.com",
    "www.marmiton.org", "marmiton.org", "www.750g.com", "750g.com", "www.chefclub.tv",
    "www.cuisineaz.com", "cuisineaz.com", "www.micheldumas.com", "micheldumas.com",
    "www.youtube.com", "www.youtube-nocookie.com", "i.ytimg.com", "img.youtube.com",
    "www.wikidata.org", "commons.wikimedia.org", "upload.wikimedia.org",
    "cdn.simpleicons.org", "icons.duckduckgo.com",
    *[row[0] for row in PROVIDERS.values()],
}
SENSITIVE_NAMES = {"apikey", "api_key", "updatetoken", "grocyapikey", "password", "secret", "authorization"}


class ApiError(Exception):
    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.status, self.code = status, code


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def bounded_json(value, depth=0):
    if depth > 12:
        raise ApiError("JSON nesting exceeds limit")
    if isinstance(value, dict):
        if len(value) > 5000:
            raise ApiError("JSON object exceeds limit")
        for key, item in value.items():
            if len(key) > 320:
                raise ApiError("JSON key exceeds limit")
            bounded_json(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 5000:
            raise ApiError("JSON array exceeds limit")
        for item in value:
            bounded_json(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ApiError("Numbers must be finite")
    elif isinstance(value, str) and len(value) > 24 * 1024 * 1024:
        raise ApiError("JSON string exceeds limit")


def body_object():
    if not request.is_json:
        raise ApiError("Content-Type application/json required", 415)
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result
    try:
        value = json.loads(request.get_data(cache=True), object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError, UnicodeError) as error:
        raise ApiError("Invalid JSON document") from error
    if not isinstance(value, dict):
        raise ApiError("A JSON object is required")
    bounded_json(value)
    return value


def number(value, minimum=0, maximum=2**31 - 1):
    if isinstance(value, bool):
        raise ApiError("Integer required")
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError()
        result = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ApiError("Integer required") from error
    if not minimum <= result <= maximum:
        raise ApiError("Integer outside supported range")
    return result


def text(value, limit=320):
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ApiError("Text required")
    return str(value).strip()[:limit]


def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SENSITIVE_NAMES and item and not str(item).startswith("grocyste-credential:"):
                raise ApiError("Use the server credential vault for provider secrets")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)


def grocy_path(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise ApiError("Invalid Grocy path")
    parsed = urlsplit(value)
    decoded = unquote(unquote(parsed.path))
    if parsed.scheme or parsed.netloc or parsed.fragment or "%" in decoded or "\\" in decoded or "//" in decoded or any(ord(c) < 32 for c in value + decoded):
        raise ApiError("Only relative Grocy API paths are supported")
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise ApiError("Path traversal denied")
    path = decoded.lstrip("/")
    if not path.startswith("api/"):
        raise ApiError("Only the Grocy API can be proxied")
    if path.startswith(("api/objects/api_keys", "api/objects/sessions")):
        raise ApiError("Credential and session entities are private", 403, "forbidden")
    if any(key.lower() in {"grocy-api-key", "api_key", "apikey"} for key, _ in parse_qsl(parsed.query)):
        raise ApiError("Credentials in query strings are prohibited")
    return path + ("?" + parsed.query if parsed.query else "")


def public_addons(registry):
    return [{"id": addon_id, "version": row.get("version", ""), "enabled": row.get("enabled") is True,
             "manifest": {key: row.get("manifest", {}).get(key) for key in ("entrypoints", "dependencies", "capabilities", "name")}}
            for addon_id, row in registry.get("addons", {}).items() if isinstance(row, dict)]


def _cookie():
    # Version-specific Grocy names are not interpreted: the upstream remains the verifier.
    names = [name for name in request.cookies if name.lower().startswith("grocy") and name != "grocyste_session"]
    if not names:
        raise ApiError("Log in to Grocy first", 401, "unauthorized")
    return "; ".join(f"{name}={request.cookies[name]}" for name in sorted(names))


class UnixManagerConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost", timeout=120)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def call_manager(socket_path, action, params):
    connection = UnixManagerConnection(socket_path)
    try:
        connection.request("POST", f"/v1/{action}", canonical(params), {"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read(1024 * 1024))
        if response.status >= 400:
            raise ApiError(text(payload.get("message")) or "Le gestionnaire a refusé cette opération.", response.status, "manager_error")
        return payload
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise NetworkError("Package manager unavailable") from error
    finally:
        connection.close()


def create_app(config=None, grocy_transport=None, external_transport=None, manager_transport=None):
    app = Flask(__name__)
    app.config.update(
        GROCY_URL=os.environ.get("GROCY_URL", "http://grocy:80").rstrip("/"),
        PUBLIC_ORIGIN=os.environ.get("PUBLIC_ORIGIN", ""),
        BASE_PATH=os.environ.get("BASE_PATH", "/__grocyste").rstrip("/"),
        STATE_DIR=os.environ.get("STATE_DIR", "/var/lib/grocyste"),
        PACKAGES_DIR=os.environ.get("PACKAGES_DIR", "/packages"),
        SECRET_FILE=os.environ.get("SECRET_FILE", os.path.join(os.environ.get("STATE_DIR", "/var/lib/grocyste"), "grocy-service.json")),
        MANAGER_SOCKET=os.environ.get("MANAGER_SOCKET", "/run/grocyste/manager.sock"),
        WEB_DIR=os.environ.get("WEB_DIR", str(Path(__file__).resolve().parent.parent / "web")),
        LEGACY_IMPORT_DIR=os.environ.get("LEGACY_IMPORT_DIR", ""),
        EXTERNAL_HOSTS=DEFAULT_EXTERNAL_HOSTS,
        INSTANCE_CONFIG_FILE=os.environ.get("INSTANCE_CONFIG_FILE", ""),
        CATALOG_FILE=os.environ.get("CATALOG_FILE", str(Path(__file__).resolve().parent.parent / "catalog.json")),
        LIVE_URL=os.environ.get("LIVE_URL", "http://mon-grocy-live:8093/__mon_grocy/live/v1").rstrip("/"),
        MAX_CONTENT_LENGTH=24 * 1024 * 1024,
    )
    if config:
        app.config.update(config)
    state = State(app.config["STATE_DIR"])
    app.extensions["grocyste_state"] = state
    grocy_transport = grocy_transport or internal_request
    external_transport = external_transport or external_request
    manager_transport = manager_transport or (lambda action, params: call_manager(app.config["MANAGER_SOCKET"], action, params))
    prefix = app.config["BASE_PATH"] + "/v1"

    def registry():
        path = Path(app.config["PACKAGES_DIR"]) / "current.json"
        if not path.exists():
            return {"schema": 1, "generation": 0, "addons": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value.get("addons"), dict):
                raise ValueError()
            return value
        except (OSError, ValueError, AttributeError) as error:
            raise ApiError("Package registry is invalid", 503, "registry_unavailable") from error

    def addon(addon_id, capability):
        if not isinstance(addon_id, str) or not ID.fullmatch(addon_id):
            raise ApiError("Invalid addon identifier")
        if addon_id == "grocyste":
            require_admin()
            return {"capabilities": [capability]}
        row = registry()["addons"].get(addon_id)
        if not row or row.get("enabled") is not True:
            raise ApiError("Addon is unknown or disabled", 403, "addon_disabled")
        manifest = row.get("manifest", {})
        if capability not in manifest.get("capabilities", []):
            raise ApiError("Addon capability denied", 403, "capability_denied")
        return manifest

    def origin():
        allowed = app.config["PUBLIC_ORIGIN"].rstrip("/")
        if not allowed or request.headers.get("Origin") != allowed:
            raise ApiError("Request origin denied", 403, "origin_denied")
        if request.headers.get("Sec-Fetch-Site") not in {None, "same-origin"}:
            raise ApiError("Cross-site request denied", 403, "origin_denied")

    def upstream(path, method="GET", data=None, cookie=None, service=False, raw_body=None, content_type=None):
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if service:
            try:
                secret = json.loads(Path(app.config["SECRET_FILE"]).read_text(encoding="utf-8"))
                key = secret["apiKey"]
                if not isinstance(key, str) or not key:
                    raise ValueError()
            except (OSError, ValueError, KeyError) as error:
                raise ApiError("Service credential is unavailable", 503, "not_configured") from error
            headers["GROCY-API-KEY"] = key
        elif cookie is not None:
            headers["Cookie"] = cookie
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = canonical(data).encode()
        if raw_body is not None:
            headers["Content-Type"] = content_type
            body = raw_body
        return grocy_transport(app.config["GROCY_URL"] + "/" + path.lstrip("/"), method=method, headers=headers, body=body)

    def user(cookie):
        result = upstream("api/user", cookie=cookie)
        if result.status != 200:
            raise ApiError("Grocy authentication failed", 401 if result.status in {401, 403, 302} else 502, "unauthorized")
        try:
            value = result.json()
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            if not isinstance(value, dict):
                raise ValueError()
            user_id = number(value.get("id"), 1)
            return {"id": user_id, "username": text(value.get("username")), "displayName": text(value.get("display_name"))}
        except (ValueError, ApiError) as error:
            raise ApiError("Grocy returned an invalid identity", 502, "upstream_error") from error

    def require_admin():
        if getattr(g, "admin_checked", False):
            return
        result = upstream(f"api/users/{g.actor['id']}/permissions", cookie=g.grocy_cookie)
        if result.status != 200:
            raise ApiError("Grocy administrator permission required", 403, "admin_required")
        try:
            rows = result.json()
            hierarchy = upstream("api/objects/permission_hierarchy", cookie=g.grocy_cookie)
            if hierarchy.status != 200:
                raise ApiError("Grocy permission hierarchy unavailable", 502, "upstream_error")
            if not has_admin_permission(rows, admin_permission_id(hierarchy.json())):
                raise ValueError()
        except (TypeError, ValueError) as error:
            raise ApiError("Grocy administrator permission required", 403, "admin_required") from error
        g.admin_checked = True

    def authenticated(mutating=False):
        def decorator(function):
            @wraps(function)
            def wrapped(*args, **kwargs):
                needs_csrf = mutating or request.method not in SAFE_METHODS
                if needs_csrf:
                    origin()
                cookie = _cookie()
                token = request.cookies.get("grocyste_session", "")
                csrf = request.headers.get("X-Grocyste-CSRF", "") if needs_csrf else None
                session = state.session(token, cookie, csrf)
                if not session:
                    raise ApiError("Grocyste session or CSRF token invalid", 401, "unauthorized")
                actor = user(cookie)
                if actor["id"] != session["user_id"]:
                    state.revoke_session(token)
                    raise ApiError("Session identity changed", 401, "unauthorized")
                g.actor, g.grocy_cookie = actor, cookie
                if not state.rate_limit(f"actor:{actor['id']}", 240):
                    raise ApiError("Request rate exceeded", 429, "rate_limited")
                return function(*args, **kwargs)
            return wrapped
        return decorator

    def mutation(payload, execute):
        key = request.headers.get("Idempotency-Key", "")
        if not KEY.fullmatch(key):
            raise ApiError("A valid Idempotency-Key is required")
        previous = state.begin_operation(g.actor["id"], key, payload)
        if previous is not None:
            return previous
        try:
            response, status = execute()
        except ApiError as error:
            # Validation errors and explicit upstream denials are safe, deterministic results.
            response, status = {"ok": False, "error": public_message(error), "code": error.code}, error.status
        except BaseException:
            state.uncertain_operation(g.actor["id"], key)
            raise
        state.finish_operation(g.actor["id"], key, response, status)
        return response, status

    @app.errorhandler(ApiError)
    def api_error(error):
        return jsonify(ok=False, error=public_message(error), code=error.code), error.status

    @app.errorhandler(StateConflict)
    def conflict(error):
        return jsonify(ok=False, error=public_message(error), code="conflict"), 409

    @app.errorhandler(NetworkError)
    def network_error(_error):
        return jsonify(ok=False, error="Le serveur est indisponible. Une opération interrompue doit être vérifiée avant de la répéter.", code="upstream_unavailable"), 502

    @app.errorhandler(Exception)
    def unexpected(error):
        if isinstance(error, HTTPException):
            return jsonify(ok=False, error="La requête HTTP n’est pas valide ou cette page n’existe pas."), error.code
        # Never log request data, cookies, secrets, or raw exception messages.
        app.logger.error("Grocyste internal failure: %s", type(error).__name__)
        return jsonify(ok=False, error="Le service rencontre une erreur interne.", code="internal_error"), 500

    @app.after_request
    def secure_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(prefix + "/public-config")
    def public_config():
        current = registry()
        catalog = {"schema": 1, "addons": []}
        catalog_file = Path(app.config["CATALOG_FILE"])
        if catalog_file.is_file():
            try:
                source = json.loads(catalog_file.read_text(encoding="utf-8"))
                if source.get("schema") != 1 or not isinstance(source.get("addons"), list):
                    raise ValueError()
                catalog = source
            except (OSError, ValueError, AttributeError):
                raise ApiError("Catalogue de présentation invalide", 503)
        return jsonify(ok=True, name="Grocyste - Seasonings enabler", version=__version__, coreVersion=__version__, apiVersion=1,
                       basePath=app.config["BASE_PATH"], generation=current.get("generation", 0), addons=public_addons(current), catalog=catalog)

    @app.post(prefix + "/auth/session")
    def pair_session():
        origin()
        body_object()
        if not state.rate_limit("pair:" + (request.remote_addr or "unknown"), 30):
            raise ApiError("Pairing rate exceeded", 429, "rate_limited")
        cookie = _cookie()
        actor = user(cookie)
        g.actor, g.grocy_cookie = actor, cookie
        is_admin = False
        try:
            require_admin()
            is_admin = True
        except ApiError as error:
            if error.status != 403:
                raise
        state.revoke_session(request.cookies.get("grocyste_session", ""))
        token, csrf = state.issue_session(actor["id"], cookie)
        configuration = {}
        configuration_file = app.config["INSTANCE_CONFIG_FILE"] or str(Path(app.config["STATE_DIR"]) / "instance.json")
        if Path(configuration_file).is_file():
            try:
                source = json.loads(Path(configuration_file).read_text(encoding="utf-8"))
                configuration = {key: source[key] for key in ("recipePackageMeasures", "familyProfiles", "shoppingRangeGeneration", "sharedTimerEntityId") if key in source}
            except (OSError, ValueError, TypeError):
                raise ApiError("Instance configuration is invalid", 503)
        settings_data, _ = state.get("user:" + str(actor["id"]), "settings", {"uiLanguageMode": "auto", "currencySymbol": "€"})
        if not configuration.get("sharedTimerEntityId"):
            entities = upstream("api/objects/userentities", cookie=cookie)
            if entities.status == 200:
                try:
                    rows = entities.json()
                    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == "Mon_Grocy_minuteurs"] if isinstance(rows, list) else []
                    if len(matches) == 1:
                        configuration["sharedTimerEntityId"] = number(matches[0]["id"], 1)
                except (ValueError, ApiError):
                    pass
        response = jsonify(ok=True, csrfToken=csrf, user=actor, isAdmin=is_admin, serviceConfigured=Path(app.config["SECRET_FILE"]).is_file(), instanceConfig=configuration, settings=settings_data,
                           capabilities=["session", "addons.manage"] if is_admin else ["session"], addons=public_addons(registry()))
        response.set_cookie("grocyste_session", token, max_age=43200, secure=True, httponly=True, samesite="Strict", path=app.config["BASE_PATH"])
        return response

    @app.post(prefix + "/auth/logout")
    @authenticated(True)
    def logout():
        state.revoke_session(request.cookies.get("grocyste_session", ""))
        response = jsonify(ok=True)
        response.delete_cookie("grocyste_session", path=app.config["BASE_PATH"], secure=True, httponly=True, samesite="Strict")
        return response

    @app.get(prefix + "/health")
    @authenticated()
    def health():
        return jsonify(ok=True, version=__version__, user=g.actor)

    @app.post(prefix + "/grocy/request")
    @authenticated(True)
    def grocy_request():
        body = body_object()
        method = text(body.get("method") or "GET", 12).upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ApiError("Unsupported HTTP method")
        path = grocy_path(body.get("path"))
        binary = None
        content_type = body.get("contentType")
        if method == "PUT" and path.startswith("api/files/") and body.get("bodyEncoding") is None:
            raise ApiError("Binary uploads are limited to Grocy pictures")
        if body.get("bodyEncoding") is not None:
            if body["bodyEncoding"] != "base64" or method != "PUT" or not re.fullmatch(r"api/files/(?:productpictures|recipepictures|userpictures)/[^/?]+", path):
                raise ApiError("Binary uploads are limited to Grocy pictures")
            if content_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"} or not isinstance(body.get("data"), str):
                raise ApiError("Unsupported picture type")
            try:
                binary = base64.b64decode(body["data"], validate=True)
            except ValueError as error:
                raise ApiError("Invalid base64 picture") from error
            if len(binary) > 16 * 1024 * 1024:
                raise ApiError("Picture exceeds upload limit", 413)
            try:
                validate_picture(binary, content_type, path.rsplit("/", 1)[-1])
            except PictureError as error:
                raise ApiError(str(error)) from error
        addon(body.get("addonId"), "grocy.read" if method in SAFE_METHODS else "grocy.write")
        if body.get("service") is True:
            require_admin()
        def execute():
            result = upstream(path, method, None if binary is not None else body.get("data"), g.grocy_cookie,
                              service=body.get("service") is True, raw_body=binary, content_type=content_type)
            if result.status >= 500 and method not in SAFE_METHODS:
                raise NetworkError("Grocy mutation result is uncertain")
            response = {"ok": 200 <= result.status < 300, "status": result.status}
            if body.get("raw") is True:
                response.update(binary_envelope(result))
            else:
                try:
                    response["data"] = result.json()
                except ValueError:
                    response["data"] = None
            if not response["ok"]:
                response.update(error="Grocy a refusé la requête.", code="grocy_error")
            if method not in SAFE_METHODS:
                state.event(body["addonId"], "grocy.changed", {"method": method, "path": path.split("?")[0], "status": result.status})
            return response, result.status if result.status >= 400 else 200
        payload, status = execute() if method in SAFE_METHODS else mutation(body, execute)
        return jsonify(payload), status

    @app.route(prefix + "/storage/<addon_id>/<name>", methods=["GET", "PUT"])
    @authenticated()
    def storage(addon_id, name):
        addon(addon_id, "storage")
        if not KEY.fullmatch(name):
            raise ApiError("Invalid storage key")
        namespace = "addon:" + addon_id
        if request.method == "GET":
            value, revision = state.get(namespace, name, {})
        else:
            body = body_object()
            value = body.get("data")
            reject_secrets(value)
            if len(canonical(value).encode()) > 1024 * 1024:
                raise ApiError("Storage document exceeds limit", 413)
            expected = number(request.headers["If-Match"].strip('"')) if "If-Match" in request.headers else None
            revision = state.put(namespace, name, value, expected, quota_bytes=8 * 1024 * 1024, quota_documents=200)
            state.event(addon_id, "storage.changed", {"key": name, "revision": revision})
        response = jsonify(ok=True, data=value, revision=revision)
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.route(prefix + "/settings", methods=["GET", "PUT"])
    @authenticated()
    def settings():
        namespace = "user:" + str(g.actor["id"])
        if request.method == "GET":
            value, revision = state.get(namespace, "settings", {"uiLanguageMode": "auto", "currency": "EUR"})
        else:
            body = body_object()
            value = body.get("data", body)
            if not isinstance(value, dict):
                raise ApiError("Settings must be an object")
            reject_secrets(value)
            if len(canonical(value).encode()) > 65536:
                raise ApiError("Settings exceed limit", 413)
            expected = number(request.headers["If-Match"].strip('"')) if "If-Match" in request.headers else None
            revision = state.put(namespace, "settings", value, expected)
        return jsonify(ok=True, data=value, revision=revision)

    @app.get(prefix + "/events")
    @authenticated()
    def events():
        addon_id = request.args.get("addonId", "")
        addon(addon_id, "storage")
        rows = state.events([addon_id], number(request.args.get("after", 0)))
        return jsonify(ok=True, events=rows)

    @app.get(prefix + "/jobs")
    @authenticated()
    def jobs():
        return jsonify(ok=True, jobs=state.operations(g.actor["id"]))

    def fetch_external(params):
        value = params.get("url")
        if not isinstance(value, str) or len(value) > 4000:
            raise ApiError("Invalid external URL")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in set(app.config["EXTERNAL_HOSTS"]) or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment:
            raise ApiError("External host denied", 403, "external_host_denied")
        method = text(params.get("method") or "GET", 12).upper()
        if method not in {"GET", "HEAD", "POST"}:
            raise ApiError("External method denied")
        provider = next((name for name, row in PROVIDERS.items() if row[0] == parsed.hostname), None)
        if method == "POST" and provider is None:
            raise ApiError("External POST is limited to configured AI providers", 403)
        if provider:
            routes = PROVIDER_ROUTES.get(provider, {"GET": r"/v1/models", "POST": r"/v1/chat/completions"})
            if not re.fullmatch(routes.get("GET" if method == "HEAD" else method, r"(?!)"), unquote(parsed.path)):
                raise ApiError("Provider endpoint denied", 403, "provider_endpoint_denied")
        supplied_headers = params.get("headers") or {}
        if not isinstance(supplied_headers, dict):
            raise ApiError("Headers must be an object")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "Grocyste/1.0 (+https://github.com/Raph563/Grocyste)"}
        for key, value in supplied_headers.items():
            lower = key.lower()
            if lower in {"authorization", "x-api-key", "x-goog-api-key"}:
                marker = str(value).removeprefix("Bearer ")
                if not provider or marker != f"grocyste-credential:{provider}":
                    raise ApiError("Browser provider credentials are prohibited")
            elif lower in {"accept", "content-type", "anthropic-version"} or (provider == "openai" and lower in {"openai-organization", "openai-project"}) or (provider == "openrouter" and lower in {"http-referer", "x-title"}):
                if "\r" in str(value) or "\n" in str(value):
                    raise ApiError("Invalid header")
                headers[key] = text(value, 160)
            else:
                raise ApiError("External request header denied")
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in {"key", "api_key", "apikey", "token", "access_token", "grocy-api-key"}:
                if key.lower() == "key" and provider == "gemini" and value == "grocyste-credential:gemini":
                    continue
                raise ApiError("Secrets in external URLs are prohibited")
            query.append((key, value))
        url = parsed._replace(query=urlencode(query)).geturl()
        if provider:
            if not state.rate_limit(f"provider:{g.actor['id']}:{provider}", 20):
                raise ApiError("Request rate exceeded", 429, "rate_limited")
            secret = state.credential(provider)
            if not secret:
                raise ApiError("Configure the provider credential on the server first", 409, "provider_not_configured")
            _, header, prefix_value = PROVIDERS[provider]
            headers[header] = prefix_value + secret
        body = params.get("body")
        if body is not None:
            if isinstance(body, (dict, list)):
                body = canonical(body)
            if not isinstance(body, str) or len(body.encode()) > 12 * 1024 * 1024:
                raise ApiError("External body exceeds limit", 413)
            body = body.encode()
        for _ in range(4):
            result = external_transport(url, method=method, headers=headers, body=body)
            if provider and len(result.body) > 4 * 1024 * 1024:
                raise NetworkError("Provider response exceeds limit")
            if result.status not in {301, 302, 303, 307, 308}:
                if provider and method == "POST" and result.status >= 500:
                    raise NetworkError("Provider request result is uncertain")
                if provider and secret.encode() in result.body:
                    result.body = result.body.replace(secret.encode(), b"[redacted]")
                return binary_envelope(result)
            # No cross-host or relative redirect forwarding of a provider secret.
            if provider:
                raise ApiError("Provider redirect denied", 502)
            from urllib.parse import urljoin
            redirected = urlsplit(urljoin(url, result.headers.get("location", "")))
            if redirected.scheme != "https" or redirected.hostname not in set(app.config["EXTERNAL_HOSTS"]) or redirected.port not in {None, 443} or redirected.username or redirected.password:
                raise ApiError("External redirect denied", 403)
            url = redirected.geturl()
        raise ApiError("Too many external redirects", 502)

    def import_legacy():
        directory = app.config["LEGACY_IMPORT_DIR"]
        if not directory:
            return
        candidates = [("receipts", "memory", ["receiptscanner-receipt-memory.json", "producthelper-receipt-memory.json"]),
                      ("courseu", "state", ["producthelper-courseu-import-state.json"])]
        for namespace, name, filenames in candidates:
            _, revision = state.get(namespace, name)
            if revision:
                continue
            for filename in filenames:
                path = Path(directory) / filename
                if path.exists():
                    try:
                        if path.stat().st_size > 8 * 1024 * 1024:
                            raise ValueError()
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(payload, dict):
                            raise ValueError()
                        bounded_json(payload)
                    except (OSError, ValueError, ApiError) as error:
                        raise ApiError("Legacy state is invalid; source remains untouched", 503, "legacy_import_failed") from error
                    try:
                        state.put(namespace, name, payload, expected=0)
                    except StateConflict:
                        pass
                    break

    def runtime_operation(addon_id, operation, params):
        if operation == "core.pair":
            addon(addon_id, "addons.manage")
            require_admin()
            try:
                result = bootstrap_service_from_cookie(app.config["GROCY_URL"], g.grocy_cookie, app.config["SECRET_FILE"], grocy_transport)
                service = json.loads(Path(app.config["SECRET_FILE"]).read_text(encoding="utf-8"))
                metadata = ensure_timer_entity(app.config["GROCY_URL"], service["apiKey"], grocy_transport)
                configuration_file = Path(app.config["INSTANCE_CONFIG_FILE"] or str(Path(app.config["STATE_DIR"]) / "instance.json"))
                existing = json.loads(configuration_file.read_text(encoding="utf-8")) if configuration_file.exists() else {}
                write_secret(configuration_file, {**existing, **metadata})
            except BootstrapError as error:
                raise ApiError(str(error), 409, "provisioning_failed") from error
            return {"ok": True, **result, "instanceConfig": metadata}
        if operation == "sessions.live":
            addon(addon_id, "timers")
            method = text(params.get("method") or "GET", 12).upper()
            if method not in {"GET", "POST"}:
                raise ApiError("Unsupported live method")
            path = params.get("path")
            if not isinstance(path, str):
                raise ApiError("Live API path required")
            parsed = urlsplit(path)
            if parsed.scheme or parsed.netloc or parsed.fragment or not re.fullmatch(r"(?:state|sessions|timers|(?:sessions|timers)/[A-Za-z0-9_-]{1,100}/actions|recipes/[1-9][0-9]*)", parsed.path):
                raise ApiError("Live API path denied")
            if any(key != "servings" or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) for key, value in parse_qsl(parsed.query)):
                raise ApiError("Live query denied")
            data = params.get("data")
            result = grocy_transport(app.config["LIVE_URL"] + "/" + path, method=method,
                                     headers={"Cookie": g.grocy_cookie, "Origin": app.config["PUBLIC_ORIGIN"], "X-Mon-Grocy-Live": "1", "Accept": "application/json", "Content-Type": "application/json", "Accept-Encoding": "identity"},
                                     body=canonical(data).encode() if data is not None else None)
            if result.status >= 500 and method == "POST":
                raise NetworkError("Live operation result is uncertain")
            return {"ok": True, **binary_envelope(result)}
        if operation == "health":
            addon(addon_id, "storage")
            return {"ok": True, "version": __version__}
        if operation.startswith("credential"):
            addon(addon_id, "runtime.external")
            require_admin()
            if operation == "credentials.status":
                return {"ok": True, "providers": state.credential_status()}
            if operation != "credential.store" or params.get("provider") not in PROVIDERS:
                raise ApiError("Unknown provider or credential operation")
            secret = params.get("secret")
            if not isinstance(secret, str) or not 8 <= len(secret) <= 2048 or any(c.isspace() for c in secret):
                raise ApiError("Invalid provider credential")
            state.credential(params["provider"], secret)
            return {"ok": True, "provider": params["provider"], "configured": True}
        if operation == "external.fetch":
            addon(addon_id, "runtime.external")
            return {"ok": True, **fetch_external(params)}
        if operation.startswith("addons."):
            addon(addon_id, "addons.manage")
            require_admin()
            action = operation.split(".", 1)[1]
            if action == "check":
                return {"ok": True, "addons": public_addons(registry())}
            if action not in {"install", "disable", "rollback", "uninstall"}:
                raise ApiError("Unsupported addon action")
            target = params.get("addonId", params.get("target"))
            if not isinstance(target, str) or not ID.fullmatch(target):
                raise ApiError("Explicit addon identifier required")
            arguments = {"addonId": target}
            if action == "install":
                version = text(params.get("version"), 80)
                if not version:
                    raise ApiError("Explicit package version required")
                arguments["version"] = version
            result = manager_transport(action, arguments)
            state.event(addon_id, "addons.changed", {"action": action, "addonId": target})
            return result
        if operation == "cache.clear":
            addon(addon_id, "runtime.barcode")
            require_admin()
            state.clear("cache")
            return {"ok": True}
        if operation == "barcode.search":
            addon(addon_id, "runtime.barcode")
            provider = params.get("provider", "openfoodfacts")
            if provider not in {"openfoodfacts", "openproductsfacts"}:
                raise ApiError("Unknown barcode provider")
            product_name = text(params.get("productName"), 320)
            if not product_name:
                raise ApiError("productName required")
            page_size = number(params.get("pageSize", 24), 1, 50)
            cache_key = hashlib.sha256(canonical([provider, product_name.lower(), page_size]).encode()).hexdigest()
            cached, _ = state.get("cache", cache_key)
            import time
            if cached and cached.get("expires", 0) > time.time():
                return {"ok": True, "provider": provider, "cached": True, "payload": cached["data"]}
            url = "https://world." + provider + ".org/cgi/search.pl?" + urlencode({"search_terms": product_name, "search_simple": 1, "action": "process", "json": 1, "page_size": page_size})
            result = external_transport(url, headers={"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "Grocyste/1.0"})
            if result.status != 200:
                raise ApiError("Barcode provider unavailable", 502)
            value = result.json()
            state.put("cache", cache_key, {"expires": time.time() + 1800, "data": value})
            state.prune("cache")
            return {"ok": True, "provider": provider, "cached": False, "payload": value}
        if operation.startswith("receipt-memory."):
            addon(addon_id, "runtime.receipts")
            import_legacy()
            if operation == "receipt-memory.get":
                value, _ = state.get("receipts", "memory", {"items": {}})
                raw = value.get("items", {})
                items = list(raw.values()) if isinstance(raw, dict) else raw
                return {"ok": True, "updatedAt": value.get("updatedAt", ""), "items": items, "count": len(items)}
            if operation != "receipt-memory.upsert":
                raise ApiError("Unknown receipt memory operation")
            raw = params.get("entry", params)
            if not isinstance(raw, dict):
                raise ApiError("Receipt entry must be an object")
            key = text(raw.get("ticketKey", raw.get("ticket_key", raw.get("ticketName"))), 220)
            if not key:
                raise ApiError("ticketKey required")
            entry = {"ticketKey": key, "ticketName": text(raw.get("ticketName", raw.get("ticket_name"))),
                     "productId": number(raw.get("productId", raw.get("product_id")), 1), "productName": text(raw.get("productName", raw.get("product_name"))),
                     "unitId": number(raw.get("unitId", raw.get("unit_id", 0))), "unitName": text(raw.get("unitName", raw.get("unit_name")), 160), "updatedAt": now_iso()}
            def update(value):
                raw_items = value.get("items", {})
                items = raw_items if isinstance(raw_items, dict) else {row["ticketKey"]: row for row in raw_items if isinstance(row, dict) and row.get("ticketKey")}
                items[key] = entry
                rows = sorted(items.values(), key=lambda row: str(row.get("updatedAt", "")), reverse=True)[:4000]
                return {"updatedAt": now_iso(), "items": {row["ticketKey"]: row for row in rows}}
            value, _ = state.update("receipts", "memory", update, {"items": {}})
            return {"ok": True, "entry": entry, "updatedAt": value["updatedAt"], "objectId": 0}
        if operation.startswith("courseu.state."):
            addon(addon_id, "runtime.courseu")
            import_legacy()
            if operation == "courseu.state.get":
                value, _ = state.get("courseu", "state", courseu_state({}))
            elif operation == "courseu.state.upsert":
                def update(value):
                    if isinstance(params.get("state"), dict):
                        return courseu_state(params["state"])
                    for raw in params.get("items", [params.get("item", params)]):
                        item = courseu_item(raw)
                        keys = {str(item.get(key)) for key in ("id", "courseUId", "sourceUrl") if item.get(key)}
                        rows = value.get("items", [])
                        matches = [index for index, existing in enumerate(rows) if keys.intersection({str(existing.get(key)) for key in ("id", "courseUId", "sourceUrl") if existing.get(key)})]
                        if len(matches) > 1:
                            raise StateConflict("Course U candidate matches multiple records")
                        if matches:
                            rows[matches[0]] = {**rows[matches[0]], **item}
                        else:
                            rows.append(item)
                        value["items"] = rows
                    return courseu_state(value)
                value, _ = state.update("courseu", "state", update, courseu_state({}))
            else:
                raise ApiError("Unknown Course U operation")
            return {"ok": True, "state": value, "summary": value["summary"], "count": len(value["items"])}
        raise ApiError("Unknown runtime operation", 400, "unknown_operation")

    @app.post(prefix + "/runtime/call")
    @authenticated(True)
    def runtime_call():
        body = body_object()
        operation = body.get("operation")
        params = body.get("params", {})
        if not isinstance(operation, str) or not isinstance(params, dict):
            raise ApiError("operation and params required")
        capability = next((cap for stem, cap in {
            "health": "storage", "credential": "runtime.external", "external.": "runtime.external",
            "addons.": "addons.manage", "cache.": "runtime.barcode", "barcode.": "runtime.barcode",
            "receipt-memory.": "runtime.receipts", "courseu.state.": "runtime.courseu", "sessions.live": "timers", "core.pair": "addons.manage",
        }.items() if operation.startswith(stem)), None)
        if capability is None:
            raise ApiError("Unknown runtime operation")
        addon(body.get("addonId"), capability)
        if operation.startswith(("credential", "addons.")) or operation in {"cache.clear", "core.pair"}:
            require_admin()
        execute = lambda: (runtime_operation(body.get("addonId"), operation, params), 200)
        # A provider POST can consume credit / trigger work; never automatically repeat it.
        write = operation == "core.pair" or operation.endswith((".upsert", ".install", ".uninstall", ".disable", ".rollback", ".store", ".clear")) or (operation in {"external.fetch", "sessions.live"} and str(params.get("method", "GET")).upper() == "POST")
        payload, status = mutation(body, execute) if write else execute()
        return jsonify(payload), status

    # Session-authenticated wire adapters for old clients after their base URL migrates.
    # The old public token endpoints intentionally have no compatibility implementation.
    @app.route(prefix + "/runtime/barcode-search", methods=["POST"])
    @app.route(prefix + "/runtime/receipt-memory", methods=["GET", "POST"])
    @app.route(prefix + "/runtime/producthelper/receipt-memory", methods=["GET", "POST"])
    @app.route(prefix + "/runtime/receiptscanner/receipt-memory", methods=["GET", "POST"])
    @app.route(prefix + "/runtime/producthelper/courseu-import-state", methods=["GET", "POST"])
    @app.route(prefix + "/runtime/cache/clear", methods=["POST"])
    @app.route(prefix + "/runtime/health", methods=["GET"])
    @authenticated()
    def legacy_runtime():
        params = body_object() if request.method == "POST" else {}
        addon_id = params.pop("addonId", None) or request.args.get("addonId") or ("producthelper" if "courseu" in request.path else "receiptscanner" if "receipt-memory" in request.path else "producthelper")
        if "barcode-search" in request.path:
            operation = "barcode.search"
        elif "receipt-memory" in request.path:
            operation = "receipt-memory.get" if request.method == "GET" else "receipt-memory.upsert"
        elif "courseu-import-state" in request.path:
            operation = "courseu.state.get" if request.method == "GET" else "courseu.state.upsert"
        elif "cache/clear" in request.path:
            operation = "cache.clear"
        else:
            operation = "health"
        execute = lambda: (runtime_operation(addon_id, operation, params), 200)
        payload, status = mutation({"addonId": addon_id, "operation": operation, "params": params}, execute) if operation.endswith((".upsert", ".clear")) else execute()
        return jsonify(payload), status

    @app.get(app.config["BASE_PATH"] + "/assets/core.js")
    def core_asset():
        path = Path(app.config["WEB_DIR"]) / "core.js"
        if not path.is_file():
            raise ApiError("Core browser asset unavailable", 503)
        return send_file(path, mimetype="text/javascript")

    @app.get(app.config["BASE_PATH"] + "/assets/<addon_id>/<path:filename>")
    def addon_asset(addon_id, filename):
        current = registry()
        row = current["addons"].get(addon_id)
        if not row or row.get("enabled") is not True or filename not in row.get("manifest", {}).get("files", {}):
            raise ApiError("Unknown package asset", 404)
        package_root = Path(app.config["PACKAGES_DIR"]).resolve()
        directory = Path(row.get("packageDir", "")).resolve()
        path = (directory / filename).resolve()
        if not directory.is_relative_to(package_root) or not path.is_relative_to(directory) or not path.is_file():
            raise ApiError("Asset path denied", 403)
        file_meta = row["manifest"]["files"][filename]
        data = path.read_bytes()
        if len(data) != file_meta["size"] or hashlib.sha256(data).hexdigest() != file_meta["sha256"]:
            raise ApiError("Package asset integrity check failed", 503)
        return send_file(BytesIO(data), download_name=filename, mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream")

    return app


def binary_envelope(result):
    return {"status": result.status, "body": base64.b64encode(result.body).decode("ascii"), "bodyEncoding": "base64", "contentType": result.headers.get("content-type", "application/octet-stream")}


def courseu_item(raw):
    if not isinstance(raw, dict):
        raise ApiError("Course U candidate must be an object")
    reject_secrets(raw)
    payload = raw.get("productPayload") or {}
    if not isinstance(payload, dict):
        raise ApiError("Course U productPayload must be an object")
    item_id = text(raw.get("id") or raw.get("courseUId") or raw.get("sourceUrl") or payload.get("name"), 160)
    if not item_id:
        raise ApiError("Course U candidate identifier required")
    value = dict(raw)
    value.update(id=item_id, source="courseu", status=raw.get("status") if raw.get("status") in {"created", "skipped", "duplicate", "review", "error"} else "review", updatedAt=now_iso())
    if len(canonical(value).encode()) > 65536:
        raise ApiError("Course U candidate exceeds limit", 413)
    return value


def courseu_state(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("items", []), list):
        raise ApiError("Course U state must contain an items array")
    rows = [courseu_item(row) for row in raw.get("items", [])]
    if len(rows) > 2500:
        raise ApiError("Too many Course U candidates", 413)
    summary = {key: 0 for key in ("created", "skipped", "duplicate", "review", "error")}
    for row in rows:
        summary[row["status"]] += 1
    summary["total"] = len(rows)
    return {"schemaVersion": 1, "mode": "dry-run", "source": "Course U", "categoryUrl": text(raw.get("categoryUrl") or "https://www.coursesu.com/c/viandes-poissons", 700),
            "generatedAt": text(raw.get("generatedAt") or now_iso(), 80), "updatedAt": now_iso(), "summary": summary, "items": rows}
