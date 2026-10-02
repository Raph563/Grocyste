import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading
import struct
import zlib
from urllib.parse import urlsplit

import pytest

from grocyste.bootstrap import BootstrapError, KeyParser, bootstrap_service, bootstrap_service_from_cookie, configure_existing_key, ensure_timer_entity
from grocyste.network import HttpResult, NetworkError, external_request
from grocyste.runtime import create_app, grocy_path
from grocyste.state import State, StateConflict


def test_vault_first_stores_cannot_observe_partial_key(tmp_path, monkeypatch):
    """Two service workers must share one complete encryption key at first use."""
    state = State(tmp_path)
    key_path = tmp_path / "credentials.key"
    publication_started, release_publication = threading.Event(), threading.Event()
    second_started, second_finished = threading.Event(), threading.Event()
    real_open, real_replace = os.open, os.replace

    def pause_publication():
        publication_started.set()
        assert release_publication.wait(5), "vault publication did not resume"

    def intercepted_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        # Covers the old directly visible, empty file as well as the corrected
        # atomic publication below, so the original regression is reproducible.
        if Path(path) == key_path and flags & os.O_CREAT:
            pause_publication()
        return descriptor

    def intercepted_replace(source, destination, *args, **kwargs):
        if Path(destination) == key_path:
            pause_publication()
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "open", intercepted_open)
    monkeypatch.setattr(os, "replace", intercepted_replace)
    def second_store():
        second_started.set()
        try:
            return state.credential("gemini", "synthetic-secret-two")
        finally:
            second_finished.set()
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(state.credential, "openai", "synthetic-secret-one")
        try:
            assert publication_started.wait(5)
            second = workers.submit(second_store)
            assert second_started.wait(5)
            # A correctly serialized worker waits. The former implementation
            # reads the already-visible empty file and raises immediately.
            second_finished.wait(0.5)
        finally:
            release_publication.set()
        assert first.result(timeout=5) is None
        assert second.result(timeout=5) is None
    assert state.credential("openai") == "synthetic-secret-one"
    assert state.credential("gemini") == "synthetic-secret-two"
    assert len(key_path.read_bytes()) == 44
    assert b"synthetic-secret" not in state.path.read_bytes()


def test_missing_vault_key_does_not_replace_key_for_existing_ciphertext(tmp_path):
    state = State(tmp_path)
    state.credential("openai", "synthetic-existing-secret")
    path = tmp_path / "credentials.key"
    original = path.read_bytes()
    rows = state.credential_status()
    path.unlink()
    with pytest.raises(StateConflict, match="clé du coffre"):
        state.credential("openai")
    with pytest.raises(StateConflict, match="clé du coffre"):
        state.credential("gemini", "synthetic-other-secret")
    assert not path.exists()
    assert state.credential_status() == rows
    path.write_bytes(original)
    assert state.credential("openai") == "synthetic-existing-secret"


def result(data, status=200, headers=None):
    return HttpResult(status, json.dumps(data).encode(), headers or {"content-type": "application/json"})


class Grocy:
    def __init__(self):
        self.admin = True
        self.authenticated = True
        self.fail_mutation = False
        self.calls = []

    def __call__(self, url, method="GET", headers=None, body=None, **kwargs):
        self.calls.append((url, method, headers, body))
        path = urlsplit(url).path
        if path == "/api/user":
            return result([{"id": 7, "username": "owner"}], 200 if self.authenticated else 401)
        if path == "/api/users/7/permissions":
            return result([{"permission_id": 1}], 200 if self.admin else 403)
        if path == "/api/objects/permission_hierarchy":
            return result([{"id": 1, "name": "ADMIN"}])
        if method not in {"GET", "HEAD"} and self.fail_mutation:
            raise NetworkError("Lost response after possible write")
        return result({"created_object_id": 21} if method == "POST" else [{"id": 21, "name": "Bread"}])


@pytest.fixture
def environment(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    capabilities = ["grocy.read", "grocy.write", "storage", "runtime.external", "runtime.barcode", "runtime.receipts", "runtime.courseu", "timers"]
    registry = {"schema": 1, "generation": 1, "addons": {"producthelper": {"version": "1.0.0", "enabled": True, "manifest": {"capabilities": capabilities, "files": {}}}, "receiptscanner": {"version": "1.0.0", "enabled": True, "manifest": {"capabilities": capabilities, "files": {}}}}}
    (packages / "current.json").write_text(json.dumps(registry))
    grocy = Grocy()
    external_calls = []
    def external(url, **kwargs):
        external_calls.append((url, kwargs))
        return result({"products": [{"code": "123", "product_name": "Bread"}]})
    manager_calls = []
    def manager(action, params):
        manager_calls.append((action, params))
        return {"ok": True, "jobId": "job-123"}
    app = create_app({"TESTING": True, "PUBLIC_ORIGIN": "https://grocy.test", "GROCY_URL": "http://grocy", "STATE_DIR": str(tmp_path / "state"), "PACKAGES_DIR": str(packages), "SECRET_FILE": str(tmp_path / "secret.json")}, grocy, external, manager)
    client = app.test_client()
    client.set_cookie("grocy_session_access_token", "valid-cookie", domain="grocy.test")
    pair = client.post("/__grocyste/v1/auth/session", json={}, base_url="https://grocy.test", headers={"Origin": "https://grocy.test"})
    assert pair.status_code == 200
    headers = {"Origin": "https://grocy.test", "X-Grocyste-CSRF": pair.json["csrfToken"]}
    return app, client, headers, grocy, external_calls, manager_calls, packages


def post(env, path, data, key="operation-1", headers=None):
    _, client, default_headers, *_ = env
    return client.post("/__grocyste/v1/" + path, json=data, base_url="https://grocy.test", headers={**default_headers, "Idempotency-Key": key, **(headers or {})})


def runtime(env, operation, params=None, key="operation-1", addon="producthelper"):
    return post(env, "runtime/call", {"addonId": addon, "operation": operation, "params": params or {}}, key)


def test_public_configuration_has_no_secret_or_internal_paths(environment):
    app, client, *_ = environment
    response = client.get("/__grocyste/v1/public-config", base_url="https://grocy.test")
    assert response.status_code == 200
    assert "updateToken" not in response.json
    assert "csrfToken" not in response.json
    assert "packageDir" not in response.get_data(as_text=True)
    assert response.json["addons"][0]["id"] == "producthelper"


def test_pair_requires_exact_origin_and_grocy_login(environment):
    _, client, _, grocy, *_ = environment
    response = client.post("/__grocyste/v1/auth/session", json={}, base_url="https://grocy.test", headers={"Origin": "https://evil.test"})
    assert response.status_code == 403
    grocy.authenticated = False
    response = client.post("/__grocyste/v1/auth/session", json={}, base_url="https://grocy.test", headers={"Origin": "https://grocy.test"})
    assert response.status_code == 401


def test_session_cookie_secure_and_opaque(environment):
    _, client, *_ = environment
    response = client.post("/__grocyste/v1/auth/session", json={}, base_url="https://grocy.test", headers={"Origin": "https://grocy.test"})
    cookie = response.headers["Set-Cookie"]
    assert all(value in cookie for value in ["HttpOnly", "Secure", "SameSite=Strict", "Path=/__grocyste"])
    assert "valid-cookie" not in cookie
    assert response.json["isAdmin"] is True
    assert "addons.manage" in response.json["capabilities"]


@pytest.mark.parametrize("headers,status", [({"Origin": "https://evil.test"}, 403), ({"X-Grocyste-CSRF": "wrong"}, 401), ({"Sec-Fetch-Site": "cross-site"}, 403)])
def test_csrf_and_cross_site_denied(environment, headers, status):
    response = post(environment, "grocy/request", {"addonId": "producthelper", "path": "api/objects/products", "method": "POST", "data": {"name": "Bread"}}, headers=headers)
    assert response.status_code == status


def test_grocy_logout_and_cookie_change_invalidate_core_session(environment):
    _, client, _, grocy, *_ = environment
    grocy.authenticated = False
    assert client.get("/__grocyste/v1/health", base_url="https://grocy.test").status_code == 401
    grocy.authenticated = True
    client.set_cookie("grocy_session_access_token", "different", domain="grocy.test")
    assert client.get("/__grocyste/v1/health", base_url="https://grocy.test").status_code == 401


def test_upstream_uses_user_cookie_without_admin_key(environment):
    response = post(environment, "grocy/request", {"addonId": "producthelper", "path": "api/objects/products", "method": "GET"})
    assert response.status_code == 200
    assert response.json["data"][0]["name"] == "Bread"
    headers = environment[3].calls[-1][2]
    assert headers["Cookie"] == "grocy_session_access_token=valid-cookie"
    assert "GROCY-API-KEY" not in headers


@pytest.mark.parametrize("path", ["https://evil.test/api/user", "//evil.test/api/user", "api/../user", "api/%252e%252e/user", "api\\user", "api/objects/api_keys/1", "api/objects/sessions/1", "api/user?GROCY-API-KEY=x", "manageapikeys"])
def test_path_and_private_entity_denied(environment, path):
    response = post(environment, "grocy/request", {"addonId": "producthelper", "path": path, "method": "GET"})
    assert response.status_code in {400, 403}


def test_disabled_and_capability_restricted_addons(environment):
    packages = environment[-1]
    current = json.loads((packages / "current.json").read_text())
    current["addons"]["producthelper"]["manifest"]["capabilities"] = ["storage"]
    (packages / "current.json").write_text(json.dumps(current))
    assert post(environment, "grocy/request", {"addonId": "producthelper", "path": "api/user", "method": "GET"}).status_code == 403
    current["addons"]["producthelper"]["enabled"] = False
    (packages / "current.json").write_text(json.dumps(current))
    assert runtime(environment, "health").status_code == 403


def test_idempotency_executes_once_and_conflicting_input_rejected(environment):
    body = {"addonId": "producthelper", "path": "api/objects/products", "method": "POST", "data": {"name": "Bread"}}
    assert post(environment, "grocy/request", body).status_code == 200
    assert post(environment, "grocy/request", body).status_code == 200
    assert len([call for call in environment[3].calls if call[1] == "POST"]) == 1
    body["data"]["name"] = "Rice"
    assert post(environment, "grocy/request", body).status_code == 409


def test_uncertain_mutation_never_retried(environment):
    environment[3].fail_mutation = True
    body = {"addonId": "producthelper", "path": "api/stock/products/21/purchase", "method": "POST", "data": {"amount": 1}}
    assert post(environment, "grocy/request", body).status_code == 502
    assert post(environment, "grocy/request", body).status_code == 409
    assert len([call for call in environment[3].calls if call[1] == "POST"]) == 1
    assert environment[0].extensions["grocyste_state"].operations(7)[0]["state"] == "needs-reconciliation"


def test_mutation_needs_idempotency_key(environment):
    response = post(environment, "grocy/request", {"addonId": "producthelper", "path": "api/objects/products", "method": "POST"}, key="")
    assert response.status_code == 400


def test_administrator_permission_removed_blocks_manager_and_service_key(environment):
    environment[3].admin = False
    assert runtime(environment, "addons.install", {"addonId": "producthelper", "version": "1.0.0"}, addon="grocyste").status_code == 403
    response = post(environment, "grocy/request", {"addonId": "producthelper", "path": "api/user", "method": "GET", "service": True})
    assert response.status_code == 403
    assert not environment[5]


def test_manager_requires_explicit_target_and_version(environment):
    assert runtime(environment, "addons.install", {"target": "all"}, addon="grocyste").status_code == 400
    assert runtime(environment, "addons.install", {"target": "typo"}, key="next", addon="grocyste").status_code == 400
    assert runtime(environment, "addons.install", {"addonId": "producthelper", "version": "1.0.0"}, key="valid", addon="grocyste").status_code == 200
    assert environment[5] == [("install", {"addonId": "producthelper", "version": "1.0.0"})]


def test_uninstall_is_admin_only_csrf_protected_and_idempotent(environment):
    _, client, headers, grocy, _, calls, _ = environment
    payload = {"addonId": "producthelper"}
    data = {"addonId": "grocyste", "operation": "addons.uninstall", "params": payload}
    assert client.post("/__grocyste/v1/runtime/call", json=data, base_url="https://grocy.test",
                       headers={"Origin": "https://grocy.test", "Idempotency-Key": "missing-csrf"}).status_code == 401
    grocy.admin = False
    assert runtime(environment, "addons.uninstall", payload, key="denied", addon="grocyste").status_code == 403
    assert not calls
    grocy.admin = True
    assert runtime(environment, "addons.uninstall", {}, key="no-target", addon="grocyste").status_code == 400
    first = runtime(environment, "addons.uninstall", payload, key="uninstall-once", addon="grocyste")
    assert first.status_code == 200
    repeated = runtime(environment, "addons.uninstall", payload, key="uninstall-once", addon="grocyste")
    assert repeated.status_code == 200 and repeated.json == first.json
    assert calls == [("uninstall", payload)]
    assert runtime(environment, "addons.uninstall", {"addonId": "receiptscanner"},
                   key="uninstall-once", addon="grocyste").status_code == 409
    grocy.admin = False
    assert runtime(environment, "addons.uninstall", payload, key="uninstall-once", addon="grocyste").status_code == 403
    assert calls == [("uninstall", payload)]


def test_namespace_storage_compare_and_swap(environment):
    _, client, headers, *_ = environment
    url = "/__grocyste/v1/storage/producthelper/settings"
    response = client.put(url, json={"data": {"foo": 1}}, base_url="https://grocy.test", headers={**headers, "If-Match": "0"})
    assert response.status_code == 200 and response.json["revision"] == 1
    response = client.put(url, json={"data": {"foo": 2}}, base_url="https://grocy.test", headers={**headers, "If-Match": "0"})
    assert response.status_code == 409
    assert client.get(url, base_url="https://grocy.test").json["data"] == {"foo": 1}
    assert client.get("/__grocyste/v1/storage/receiptscanner/settings", base_url="https://grocy.test").json["data"] == {}
    assert client.get("/__grocyste/v1/events?addonId=producthelper", base_url="https://grocy.test").json["events"][0]["data"]["revision"] == 1


def test_settings_reject_plaintext_secrets(environment):
    _, client, headers, *_ = environment
    assert client.put("/__grocyste/v1/settings", json={"data": {"apiKey": "sensitive-value"}}, base_url="https://grocy.test", headers=headers).status_code == 400
    assert client.put("/__grocyste/v1/settings", json={"data": {"apiKey": "grocyste-credential:openai", "uiLanguageMode": "fr"}}, base_url="https://grocy.test", headers=headers).status_code == 200
    assert client.get("/__grocyste/v1/settings", base_url="https://grocy.test").json["data"]["uiLanguageMode"] == "fr"


def test_receipt_memory_and_malformed_ids(environment):
    entry = {"ticketKey": "rice", "productId": 21, "unitId": 3}
    assert runtime(environment, "receipt-memory.upsert", {"entry": entry}).status_code == 200
    read = runtime(environment, "receipt-memory.get")
    assert read.json["count"] == 1 and read.json["items"][0]["productId"] == 21
    entry["productId"] = "invalid"
    assert runtime(environment, "receipt-memory.upsert", {"entry": entry}, key="bad-id").status_code == 400


def test_courseu_distinct_items_with_missing_identifiers_do_not_merge(environment):
    assert runtime(environment, "courseu.state.upsert", {"items": [{"id": "bread"}, {"id": "rice"}]}).status_code == 200
    read = runtime(environment, "courseu.state.get")
    assert [row["id"] for row in read.json["state"]["items"]] == ["bread", "rice"]
    assert runtime(environment, "courseu.state.upsert", {"item": {"id": "rice", "status": "created"}}, key="second").status_code == 200
    assert runtime(environment, "courseu.state.get").json["summary"]["created"] == 1


def test_barcode_cache_and_invalid_page_size(environment):
    assert runtime(environment, "barcode.search", {"productName": "Bread"}).json["cached"] is False
    assert runtime(environment, "barcode.search", {"productName": "Bread"}).json["cached"] is True
    assert len(environment[4]) == 1
    assert runtime(environment, "barcode.search", {"productName": "Bread", "pageSize": "oops"}).status_code == 400
    assert runtime(environment, "cache.clear").status_code == 200
    assert runtime(environment, "barcode.search", {"productName": "Bread"}).json["cached"] is False


@pytest.mark.parametrize("url", ["http://world.openfoodfacts.org/api", "https://localhost/api", "https://127.0.0.1/api", "https://world.openfoodfacts.org:444/api", "https://user:pass@world.openfoodfacts.org/api"])
def test_external_url_policy(environment, url):
    assert runtime(environment, "external.fetch", {"url": url}).status_code == 403
    assert not environment[4]


def test_external_credentials_encrypted_and_never_returned(environment):
    assert runtime(environment, "credential.store", {"provider": "openai", "secret": "server-only-test-key"}).status_code == 200
    database = Path(environment[0].config["STATE_DIR"]) / "grocyste.sqlite3"
    assert b"server-only-test-key" not in database.read_bytes()
    response = runtime(environment, "external.fetch", {"url": "https://api.openai.com/v1/chat/completions", "method": "POST", "body": "{}"}, key="ai-post")
    assert response.status_code == 200
    assert environment[4][-1][1]["headers"]["Authorization"] == "Bearer server-only-test-key"
    assert "server-only-test-key" not in response.get_data(as_text=True)
    assert runtime(environment, "external.fetch", {"url": "https://api.openai.com/v1/models", "headers": {"Authorization": "Bearer browser-secret"}}).status_code == 400
    assert runtime(environment, "external.fetch", {"url": "https://generativelanguage.googleapis.com/v1beta/models?key=browser-secret"}).status_code == 400


def test_external_dns_rebinding_private_address_blocked(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(NetworkError):
        external_request("https://world.openfoodfacts.org/api")


def test_external_redirect_to_private_host_denied(environment):
    app = environment[0]
    # New app shares durable session state but has an adversarial external transport.
    fake = lambda *args, **kwargs: HttpResult(302, b"", {"location": "https://localhost/secret"})
    clone = create_app(app.config, environment[3], fake)
    client = clone.test_client()
    client.set_cookie("grocy_session_access_token", "valid-cookie", domain="grocy.test")
    response = client.post("/__grocyste/v1/auth/session", json={}, base_url="https://grocy.test", headers={"Origin": "https://grocy.test"})
    response = client.post("/__grocyste/v1/runtime/call", json={"addonId": "producthelper", "operation": "external.fetch", "params": {"url": "https://world.openfoodfacts.org/api"}}, base_url="https://grocy.test", headers={"Origin": "https://grocy.test", "X-Grocyste-CSRF": response.json["csrfToken"]})
    assert response.status_code == 403


def test_binary_grocy_upload_and_raw_response(environment):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    picture = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")
    filename = base64.b64encode(b"test.png").decode()
    body = {"addonId": "producthelper", "method": "PUT", "path": "api/files/recipepictures/" + filename, "data": base64.b64encode(picture).decode(), "bodyEncoding": "base64", "contentType": "image/png", "raw": True}
    response = post(environment, "grocy/request", body)
    assert response.status_code == 200 and response.json["bodyEncoding"] == "base64"
    assert environment[3].calls[-1][3] == picture
    body["path"] = "api/objects/products"
    assert post(environment, "grocy/request", body, key="bad-binary").status_code == 400


@pytest.mark.parametrize("content,filename,declared_type", [
    (b"<!doctype html><script>alert(1)</script>", "test.jpg", "image/jpeg"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "test.png", "image/png"),
    (b"GIF89a\1\0\1\0\0\0\0;", "test.html", "image/gif"),
    (b"GIF89a\1\0\1\0\0\0\0;", "test.svg", "image/gif"),
    (b"\xff\xd8synthetic-jpeg\xff\xd9", "test.jpg", "image/jpeg"),
    (b"GIF89a\1\0\1\0\0\0\0;", "../test.gif", "image/gif"),
])
def test_active_or_invalid_picture_upload_never_reaches_native_file_store(environment, content, filename, declared_type):
    data = {"addonId": "producthelper", "method": "PUT", "path": "api/files/productpictures/" + base64.b64encode(filename.encode()).decode(),
            "data": base64.b64encode(content).decode(), "bodyEncoding": "base64", "contentType": declared_type}
    before = len(environment[3].calls)
    assert post(environment, "grocy/request", data).status_code == 400
    assert not any(call[1] == "PUT" for call in environment[3].calls[before:])


def test_picture_put_cannot_bypass_binary_validation_with_json(environment):
    data = {"addonId": "producthelper", "method": "PUT", "path": "api/files/productpictures/" + base64.b64encode(b"test.html").decode(),
            "data": "<script>alert(1)</script>", "contentType": "image/jpeg"}
    assert post(environment, "grocy/request", data).status_code == 400
    assert not any(call[1] == "PUT" for call in environment[3].calls)


def test_assets_require_declared_integrity_and_package_boundary(environment, tmp_path):
    packages = environment[-1]
    directory = packages / "installed" / "producthelper" / "1.0.0"
    directory.mkdir(parents=True)
    (directory / "addon.js").write_bytes(b"console.log('safe')")
    current = json.loads((packages / "current.json").read_text())
    current["addons"]["producthelper"].update(packageDir=str(directory))
    current["addons"]["producthelper"]["manifest"]["files"] = {"addon.js": {"size": 19, "sha256": hashlib.sha256(b"console.log('safe')").hexdigest()}}
    current["addons"]["producthelper"]["manifest"]["files"]["addon.js"]["size"] = len(b"console.log('safe')")
    (packages / "current.json").write_text(json.dumps(current))
    client = environment[1]
    url = "/__grocyste/assets/producthelper/addon.js"
    assert client.get(url, base_url="https://grocy.test").status_code == 200
    (directory / "addon.js").write_bytes(b"malicious")
    assert client.get(url, base_url="https://grocy.test").status_code == 503
    current["addons"]["producthelper"]["packageDir"] = str(tmp_path)
    (packages / "current.json").write_text(json.dumps(current))
    assert client.get(url, base_url="https://grocy.test").status_code == 403


def test_legacy_json_import_preserved_and_corruption_fails_closed(environment, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    data = {"updatedAt": "2026-01-01", "items": {"rice": {"ticketKey": "rice", "productId": 21}}}
    path = legacy / "producthelper-receipt-memory.json"
    path.write_text(json.dumps(data))
    environment[0].config["LEGACY_IMPORT_DIR"] = str(legacy)
    assert runtime(environment, "receipt-memory.get").json["items"][0]["productId"] == 21
    assert json.loads(path.read_text()) == data
    environment[0].extensions["grocyste_state"].clear("receipts")
    path.write_text("corrupt")
    assert runtime(environment, "receipt-memory.get").status_code == 503
    assert path.read_text() == "corrupt"


def test_sqlite_instances_coordinate_writers(tmp_path):
    first, second = State(tmp_path), State(tmp_path)
    first.put("counter", "value", 0)
    def increment(index):
        store = first if index % 2 else second
        store.update("counter", "value", lambda value: value + 1, 0)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(increment, range(80)))
    assert State(tmp_path).get("counter", "value") == (80, 81)


def test_state_rejects_replay_pending_after_process_restart(tmp_path):
    assert State(tmp_path).begin_operation(7, "key", {"amount": 1}) is None
    with pytest.raises(StateConflict):
        State(tmp_path).begin_operation(7, "key", {"amount": 1})


def test_key_parser_extracts_only_selected_key():
    parser = KeyParser(4)
    parser.feed('<a data-apikey-id="3" data-apikey-key="wrong"></a><a data-apikey-id="4" data-apikey-key="selected"></a>')
    assert parser.value == "selected"


def test_bootstrap_existing_key_admin_validation_and_no_secret_response(tmp_path):
    grocy = Grocy()
    path = tmp_path / "secret.json"
    metadata = configure_existing_key("http://grocy", "test-admin-api-key", path, grocy)
    assert metadata == {"ok": True, "userId": 7, "mode": "provided"}
    assert json.loads(path.read_text())["apiKey"] == "test-admin-api-key"
    grocy.admin = False
    with pytest.raises(BootstrapError):
        configure_existing_key("http://grocy", "bad-key", tmp_path / "other", grocy)


@pytest.mark.parametrize("cookie_mode", [False, True])
def test_bootstrap_service_uses_official_api_and_ui_no_database(tmp_path, cookie_mode):
    calls = []
    created = False
    service_admin = False
    captured_key = False
    def transport(url, method="GET", headers=None, body=None, **kwargs):
        nonlocal created, service_admin, captured_key
        calls.append((url, method))
        path = urlsplit(url).path
        if path == "/api/user":
            return result([{"id": 9 if headers.get("GROCY-API-KEY") == "generated-service-key" or headers.get("Cookie") == "grocy_session_access_token=service-session" else 7}])
        if path == "/api/objects/permission_hierarchy":
            return result([{"id": 42, "name": "ADMIN"}])
        if path.endswith("/permissions"):
            if method == "PUT":
                assigned = json.loads(body)["permissions"]
                if assigned:
                    assert captured_key, "Service ADMIN must only be granted after the key is captured"
                    assert assigned == [42]
                    service_admin = True
                else:
                    assert not captured_key
                return HttpResult(204, b"", {})
            return result([{"permission_id": 42}])
        if path == "/api/users" and method == "POST":
            created = True
            return HttpResult(204, b"", {})
        if path == "/api/users":
            return result([{"id": 9, "username": "grocyste_service"}] if created else [{"id": 7, "username": "owner"}])
        if path == "/login":
            return HttpResult(302, b"", {"set-cookie": "grocy_session_access_token=service-session; HttpOnly", "location": "/"})
        if path == "/manageapikeys/new":
            return HttpResult(302, b"", {"location": "/manageapikeys?key=12"})
        if path == "/manageapikeys":
            assert service_admin is False
            captured_key = True
            return HttpResult(200, b'<a data-apikey-id="12" data-apikey-key="generated-service-key"></a>', {})
        if path == "/logout":
            return HttpResult(302, b"", {})
        raise AssertionError((url, method))
    secret = tmp_path / "secret.json"
    if cookie_mode:
        metadata = bootstrap_service_from_cookie("http://grocy", "grocy_session_access_token=admin-session", secret, transport)
    else:
        metadata = bootstrap_service("http://grocy", "provision-admin-key", secret, transport)
    assert metadata == {"ok": True, "userId": 9, "keyId": 12, "mode": "service"}
    assert json.loads(secret.read_text())["apiKey"] == "generated-service-key"
    assert any(path.endswith("/login") for path, _ in calls)


def test_timer_entity_provisioning_preserves_native_ids_and_is_idempotent():
    entities, fields, writes = [], [], []
    def transport(url, method="GET", headers=None, body=None, **kwargs):
        path = urlsplit(url).path
        if path == "/api/user":
            return result({"id": 7})
        if path == "/api/users/7/permissions":
            return result([{"permission_id": 42}])
        if path == "/api/objects/permission_hierarchy":
            return result([{"id": 42, "name": "ADMIN"}])
        rows = entities if path == "/api/objects/userentities" else fields if path == "/api/objects/userfields" else None
        assert rows is not None, "Metadata provisioning must never change stock or other entities"
        if method == "GET":
            return result(rows)
        assert method == "POST"
        payload = json.loads(body)
        writes.append(payload)
        identifier = 13 if rows is entities else 29
        rows.append({"id": identifier, **payload})
        return result({"created_object_id": identifier})
    assert ensure_timer_entity("http://grocy", "test-key", transport) == {"sharedTimerEntityId": 13}
    assert ensure_timer_entity("http://grocy", "test-key", transport) == {"sharedTimerEntityId": 13}
    assert len(writes) == 2
    assert fields[0]["type"] == "text-multi-line"
    fields[0]["type"] = "number-integral"
    with pytest.raises(BootstrapError):
        ensure_timer_entity("http://grocy", "test-key", transport)
    assert len(writes) == 2


def test_browser_core_pair_requires_admin_and_returns_metadata_only(environment, monkeypatch):
    calls = []
    def pair(url, cookie, secret_file, transport):
        calls.append((url, cookie))
        Path(secret_file).write_text(json.dumps({"apiKey": "server-pairing-only", "userId": 9, "keyId": 12}))
        return {"ok": True, "userId": 9, "keyId": 12, "mode": "service"}
    monkeypatch.setattr("grocyste.runtime.bootstrap_service_from_cookie", pair)
    monkeypatch.setattr("grocyste.runtime.ensure_timer_entity", lambda *args: {"sharedTimerEntityId": 13})
    response = runtime(environment, "core.pair", addon="grocyste")
    assert response.status_code == 200 and response.json["instanceConfig"]["sharedTimerEntityId"] == 13
    assert "server-pairing-only" not in response.get_data(as_text=True)
    assert calls == [("http://grocy", "grocy_session_access_token=valid-cookie")]
    environment[3].admin = False
    assert runtime(environment, "core.pair", key="nonadmin", addon="grocyste").status_code == 403
    assert len(calls) == 1


def test_external_provider_key_cannot_access_admin_or_file_endpoints(environment):
    assert runtime(environment, "credential.store", {"provider": "openai", "secret": "server-only-test-key"}).status_code == 200
    assert runtime(environment, "external.fetch", {"url": "https://api.openai.com/v1/files"}).status_code == 403
    assert runtime(environment, "external.fetch", {"url": "https://api.openai.com/v1/projects", "method": "POST", "body": "{}"}, key="project-write").status_code == 403
    assert not environment[4]


def test_duplicate_json_keys_and_infinite_numbers_denied(environment):
    _, client, headers, *_ = environment
    response = client.post("/__grocyste/v1/runtime/call", data='{"addonId":"producthelper","addonId":"grocyste","operation":"health"}', content_type="application/json", base_url="https://grocy.test", headers=headers)
    assert response.status_code == 400
    response = client.post("/__grocyste/v1/runtime/call", data='{"addonId":"producthelper","operation":"health","params":{"value":Infinity}}', content_type="application/json", base_url="https://grocy.test", headers=headers)
    assert response.status_code == 400


def test_public_errors_are_french(environment):
    response = post(environment, "grocy/request", {"addonId": "producthelper", "method": "GET", "path": "https://evil.test/api/user"})
    assert response.json["error"] == "Seuls les chemins relatifs de l’API Grocy sont autorisés."


def test_live_bridge_cookie_origin_and_path_validation(environment):
    response = runtime(environment, "sessions.live", {"path": "state"})
    assert response.status_code == 200 and response.json["bodyEncoding"] == "base64"
    headers = environment[3].calls[-1][2]
    assert headers["Cookie"] == "grocy_session_access_token=valid-cookie"
    assert headers["Origin"] == "https://grocy.test"
    assert runtime(environment, "sessions.live", {"path": "../secrets"}).status_code == 400


def test_malformed_json_shapes_do_not_produce_500(environment):
    assert post(environment, "runtime/call", []).status_code == 400
    assert post(environment, "runtime/call", {"operation": "barcode.search", "addonId": "producthelper", "params": []}).status_code == 400
    assert post(environment, "runtime/call", {"operation": "barcode.search", "addonId": "producthelper", "params": {"pageSize": float("nan")}}).status_code == 400
