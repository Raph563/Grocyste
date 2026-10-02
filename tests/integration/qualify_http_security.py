#!/usr/bin/env python3
"""Real HTTP qualification limited to the fixed isolated laboratory endpoints.

Create temporary authentication fixtures and one location through native APIs;
remove owned fixtures in finally. Cookies and keys are never written to reports.
"""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import http.cookiejar
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from grocy_baseline import Client
from lab_support import lab_root

LAB = lab_root()
ORIGIN = "https://127.0.0.1:19443"
PREFIX = "/__grocyste/v1/"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Core:
    def __init__(self, grocy=None):
        self.cookie = "; ".join(f"{item.name}={item.value}" for item in grocy.jar) if grocy else ""
        self.token, self.csrf = "", ""
        # The laboratory terminates TLS with a disposable self-signed certificate.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))

    def request(self, method, path, body=None, headers=None, authenticated=True, origin=True):
        combined = {"Accept": "application/json", "User-Agent": "grocyste-security-lab/1.0"}
        if authenticated:
            combined["Cookie"] = self.cookie + ("; grocyste_session=" + self.token if self.token else "")
            if self.csrf:
                combined["X-Grocyste-CSRF"] = self.csrf
        if origin:
            combined["Origin"] = ORIGIN
        if isinstance(body, (dict, list)):
            body = json.dumps(body, allow_nan=False).encode()
            combined["Content-Type"] = "application/json"
        combined.update(headers or {})
        url = ORIGIN + PREFIX + path
        request = urllib.request.Request(url, data=body, headers=combined, method=method)
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.read(4 * 1024 * 1024)
        try:
            content = json.loads(raw)
        except ValueError:
            content = None
        return response.status, content, dict(response.headers), raw

    def pair(self):
        status, payload, headers, _ = self.request("POST", "auth/session", {})
        if status != 200:
            raise RuntimeError("L'appairage de la session laboratoire a échoué")
        cookie = SimpleCookie()
        cookie.load(headers.get("Set-Cookie", ""))
        self.token, self.csrf = cookie["grocyste_session"].value, payload["csrfToken"]
        return payload, headers

    def proxy(self, addon, method, path, data=None, **options):
        headers = options.pop("headers", {})
        return self.request("POST", "grocy/request", {"addonId": addon, "method": method,
            "path": path, "data": data, **options}, headers=headers)

    def call(self, operation, params=None, addon="grocyste", **kwargs):
        return self.request("POST", "runtime/call", {"addonId": addon, "operation": operation,
            "params": params or {}}, **kwargs)


def main():
    credentials = json.loads((LAB / "private/grocy-credentials.json").read_bytes())["vanilla"]
    assert credentials["base_url"] == "http://127.0.0.1:19283"
    admin_api = Client(credentials["base_url"])
    key = credentials["admin_key"]
    evidence, owned, locations, pictures = [], [], [], []

    def check(name, actual, expected):
        allowed = expected if isinstance(expected, tuple) else (expected,)
        passed = actual in allowed
        evidence.append({"name": name, "passed": passed, "actual": actual,
                         "expected": list(allowed) if len(allowed) > 1 else expected})
        if not passed:
            raise AssertionError(name + " : résultat inattendu")

    def api(method, path, data=None):
        return admin_api.request(method, path, data, key=key)

    try:
        anonymous = Core()
        status, public, headers, raw = anonymous.request("GET", "public-config", authenticated=False)
        check("anonymous_public_configuration", status, 200)
        check("public_installed_addons", len(public["addons"]), 9)
        check("public_configuration_no_credentials", any(field in raw for field in
            (b"apiKey", b"csrfToken", b"updateToken", b"packageDir", key.encode())), False)
        for path in ("health", "jobs", "settings", "events?addonId=producthelper", "storage/producthelper/probe"):
            check("anonymous_denied_" + path.split("?")[0], anonymous.request("GET", path, authenticated=False)[0], 401)
        check("pair_requires_native_cookie", anonymous.request("POST", "auth/session", {}, authenticated=False)[0], 401)
        check("api_key_header_cannot_pair", anonymous.request("POST", "auth/session", {},
            headers={"GROCY-API-KEY": key}, authenticated=False)[0], 401)
        hierarchy = api("GET", "/api/objects/permission_hierarchy")[1]
        admin_permission = next(row["id"] for row in hierarchy if row["name"] == "ADMIN")

        def actor(admin=False):
            username, password = "grocyste-security-" + secrets.token_hex(6), secrets.token_urlsafe(40)
            check("create_owned_authentication_fixture", api("POST", "/api/users", {
                "username": username, "password": password, "first_name": "Synthetic",
                "last_name": "Security", "picture_file_name": None})[0], 204)
            users = api("GET", "/api/users?query[]=" + urllib.parse.quote("username=" + username))[1]
            user_id = next(row["id"] for row in users if row["username"] == username)
            owned.append(user_id)
            check("set_fixture_native_permissions", api("PUT", f"/api/users/{user_id}/permissions",
                {"permissions": [admin_permission] if admin else []})[0], 204)
            native = Client(credentials["base_url"])
            check("native_session_identity", native.login(username, password)["id"], user_id)
            core = Core(native)
            pairing, cookie_headers = core.pair()
            return user_id, native, core, pairing, cookie_headers

        limited_id, limited_native, limited, pairing, cookie_headers = actor()
        check("limited_actor_has_no_core_admin", pairing["isAdmin"], False)
        check("limited_actor_cannot_manage_addons", "addons.manage" in pairing["capabilities"], False)
        check("opaque_session_cookie", len(limited.token) >= 40 and limited.token not in limited.cookie, True)
        check("session_cookie_attributes", all(value in cookie_headers["Set-Cookie"] for value in
            ("Secure", "HttpOnly", "SameSite=Strict", "Path=/__grocyste")), True)
        for name, extra, has_origin, expected in (
            ("missing_origin", {}, False, 403),
            ("cross_origin", {"Origin": "https://evil.invalid"}, True, 403),
            ("origin_port_mismatch", {"Origin": "https://127.0.0.1"}, True, 403),
            ("cross_site_fetch", {"Sec-Fetch-Site": "cross-site"}, True, 403),
            ("missing_csrf", {"X-Grocyste-CSRF": ""}, True, 401),
            ("wrong_csrf", {"X-Grocyste-CSRF": "invalid"}, True, 401),
            ("cookie_binding_changed", {"Cookie": limited.cookie + "x; grocyste_session=" + limited.token}, True, 401),
        ):
            check(name, limited.request("POST", "grocy/request", {"addonId": "producthelper",
                "method": "GET", "path": "api/user"}, headers=extra, origin=has_origin)[0], expected)
        status, body, response_headers, _ = limited.proxy("producthelper", "GET", "api/user")
        check("native_user_cookie_forwarded", status, 200)
        check("native_user_identity_preserved", body["data"][0]["id"], limited_id)
        check("native_permission_denial_preserved", limited.proxy("producthelper", "GET",
            f"api/users/{limited_id}/permissions", headers={"GROCY-API-KEY": key})[0], 403)
        check("core_service_key_denied_to_non_admin", limited.proxy("producthelper", "GET", "api/user", service=True)[0], 403)
        check("core_scope_denied_to_non_admin", limited.call("addons.check")[0], 403)
        check("vault_denied_to_non_admin", limited.call("credential.store", {"provider": "openai", "secret": "synthetic"},
            addon="producthelper", headers={"Idempotency-Key": uuid.uuid4().hex})[0], 403)
        for path in ("api/objects/api_keys", "api/objects/sessions", "manageapikeys", "api/%252e%252e/user", "//127.0.0.1/api/user"):
            check("private_or_ambiguous_path_denied_" + path, limited.proxy("producthelper", "GET", path)[0],
                  403 if path in {"api/objects/api_keys", "api/objects/sessions"} else 400)
        for addon in ("all", "missing-addon", "../producthelper"):
            check("unrecognized_addon_denied_" + addon, limited.proxy(addon, "GET", "api/user")[0], (400, 403))
        check("secret_cannot_be_stored_in_settings", limited.request("PUT", "settings", {"data": {"apiKey": "synthetic"}})[0], 400)
        check("settings_document_bounded", limited.request("PUT", "settings", {"data": {"text": "x" * 70000}})[0], 413)
        check("duplicate_json_rejected", limited.request("POST", "grocy/request", b'{"addonId":"producthelper","addonId":"grocyste"}',
            headers={"Content-Type": "application/json"})[0], 400)
        check("non_finite_json_rejected", limited.request("POST", "grocy/request", b'{"addonId":"producthelper","data":NaN}',
            headers={"Content-Type": "application/json"})[0], 400)
        for url in ("https://127.0.0.1/", "https://localhost/", "http://world.openfoodfacts.org/",
                    "https://world.openfoodfacts.org:444/", "https://user:secret@world.openfoodfacts.org/"):
            check("ssrf_url_rejected_" + urllib.parse.urlsplit(url).hostname, limited.call("external.fetch", {"url": url},
                addon="producthelper")[0], (400, 403))
        check("untrusted_provider_auth_rejected", limited.call("external.fetch", {"url": "https://api.openai.com/v1/models",
            "headers": {"Authorization": "Bearer synthetic-untrusted-secret"}}, addon="producthelper")[0], 400)
        check("nosniff_header", response_headers.get("X-Content-Type-Options"), "nosniff")
        check("no_store_header", response_headers.get("Cache-Control"), "no-store")
        check("referrer_policy_header", response_headers.get("Referrer-Policy"), "same-origin")
        cors = limited.request("OPTIONS", "grocy/request", headers={"Origin": "https://evil.invalid"})[2]
        check("no_cross_origin_cors_grant", "Access-Control-Allow-Origin" in cors, False)

        privileged_id, privileged_native, privileged, pairing, _ = actor(admin=True)
        check("native_admin_recognized", pairing["isAdmin"], True)
        check("core_admin_permission_present", "addons.manage" in pairing["capabilities"], True)
        check("private_api_keys_denied_even_to_core_admin", privileged.proxy("grocyste", "GET", "api/objects/api_keys")[0], 403)
        picture_name = "grocyste-security-" + uuid.uuid4().hex + ".png"
        picture_path = "api/files/recipepictures/" + base64.b64encode(picture_name.encode()).decode()
        pictures.append(picture_path)
        def encoded_upload(content, filename=picture_name, mime="image/png", **options):
            return privileged.proxy("producthelper", "PUT",
                "api/files/recipepictures/" + base64.b64encode(filename.encode()).decode(),
                base64.b64encode(content).decode(), bodyEncoding="base64", contentType=mime,
                headers={"Idempotency-Key": uuid.uuid4().hex}, **options)
        check("html_picture_refused", encoded_upload(b"<!doctype html><script>alert(1)</script>")[0], 400)
        check("svg_picture_refused", encoded_upload(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')[0], 400)
        check("json_picture_validation_bypass_refused", privileged.proxy("producthelper", "PUT", picture_path,
            "<!doctype html><script>alert(1)</script>", contentType="image/png",
            headers={"Idempotency-Key": uuid.uuid4().hex})[0], 400)
        check("invalid_picture_did_not_create_native_file", privileged.proxy("producthelper", "GET", picture_path, raw=True)[0], 404)
        def chunk(kind, content):
            return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)
        picture = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")
        upload_key = uuid.uuid4().hex
        upload = {"addonId": "producthelper", "method": "PUT", "path": picture_path,
                  "data": base64.b64encode(picture).decode(), "bodyEncoding": "base64",
                  "contentType": "image/png", "raw": True}
        uploaded = privileged.request("POST", "grocy/request", upload, headers={"Idempotency-Key": upload_key})
        check("synthetic_png_uploaded_via_core", uploaded[0], 200)
        repeated_picture = privileged.request("POST", "grocy/request", upload, headers={"Idempotency-Key": upload_key})
        check("picture_upload_replay_is_same_result", repeated_picture[1] == uploaded[1], True)
        downloaded = privileged.proxy("producthelper", "GET", picture_path, raw=True)
        check("synthetic_png_read_via_core", downloaded[0], 200)
        check("native_picture_bytes_identical", base64.b64decode(downloaded[1]["body"]) == picture, True)
        deleted = privileged.proxy("producthelper", "DELETE", picture_path, headers={"Idempotency-Key": uuid.uuid4().hex})
        check("synthetic_picture_deleted_via_core", deleted[0], 200)
        check("synthetic_picture_absent_after_delete", privileged.proxy("producthelper", "GET", picture_path, raw=True)[0], 404)
        name = "grocyste-security-location-" + uuid.uuid4().hex
        payload = {"name": name, "description": "Disposable isolated qualification fixture"}
        operation_key = uuid.uuid4().hex
        first = privileged.proxy("producthelper", "POST", "api/objects/locations", payload,
            headers={"Idempotency-Key": operation_key})
        check("owned_location_created_via_core", first[0], 200)
        location_id = first[1]["data"]["created_object_id"]
        locations.append(location_id)
        replay = privileged.proxy("producthelper", "POST", "api/objects/locations", payload,
            headers={"Idempotency-Key": operation_key})
        check("same_idempotency_result", replay[1] == first[1], True)
        check("idempotency_payload_conflict", privileged.proxy("producthelper", "POST", "api/objects/locations",
            {**payload, "name": name + "-changed"}, headers={"Idempotency-Key": operation_key})[0], 409)
        rows = api("GET", "/api/objects/locations")[1]
        check("replay_created_exactly_one_location", len([row for row in rows if row["name"] == name]), 1)
        check("permission_revocation_write", api("PUT", f"/api/users/{privileged_id}/permissions", {"permissions": []})[0], 204)
        check("existing_core_admin_session_loses_admin", privileged.call("addons.check")[0], 403)
        check("existing_core_session_uses_revoked_native_permissions", privileged.proxy("producthelper", "POST",
            "api/objects/locations", {"name": name + "-forbidden"}, headers={"Idempotency-Key": uuid.uuid4().hex})[0], 403)
        limited_native.request("GET", "/logout")
        check("grocy_logout_invalidates_core_session", limited.request("GET", "health")[0], 401)
        check("core_logout_revokes_opaque_session", privileged.request("POST", "auth/logout", {})[0], 200)
        check("opaque_session_after_logout_denied", privileged.request("GET", "health")[0], 401)
    finally:
        cleanup_statuses = []
        for picture_path in pictures:
            cleanup_statuses.append(api("DELETE", "/" + picture_path)[0])
        for location_id in locations:
            cleanup_statuses.append(api("DELETE", f"/api/objects/locations/{location_id}")[0])
        for user_id in owned:
            cleanup_statuses.append(api("DELETE", f"/api/users/{user_id}")[0])
        report = {"suite": "real-core-http-security", "createdAt": datetime.now(timezone.utc).isoformat(),
            "passed": sum(item["passed"] for item in evidence), "failed": sum(not item["passed"] for item in evidence),
            "tests": evidence, "productionRequests": 0, "credentialsInReport": False,
            "destructiveScope": "temporary users, one owned location and one owned raster file in disposable vanilla laboratory",
            "cleanupAttempted": True, "cleanupConfirmed": all(status in {200, 204, 404} for status in cleanup_statuses),
            "limits": ["TLS verification disabled only for loopback self-signed laboratory certificate",
                "No provider credentials or external paid inference exercised", "No manager package registry mutation in shared UI backend"]}
        output = LAB / "security/http-auth.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"suite": report["suite"], "passed": report["passed"], "failed": report["failed"],
            "failedTests": [row["name"] for row in evidence if not row["passed"]]}))
        if not report["cleanupConfirmed"]:
            raise RuntimeError("Le nettoyage des seules fixtures possédées doit être vérifié.")


if __name__ == "__main__":
    main()
