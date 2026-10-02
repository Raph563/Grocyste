"""Bounded HTTP without ambient credentials, proxies or automatic redirects."""

from dataclasses import dataclass
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit


class NetworkError(Exception):
    pass


@dataclass
class HttpResult:
    status: int
    body: bytes
    headers: dict

    def json(self):
        return json.loads(self.body.decode("utf-8")) if self.body else None


def _read(response, maximum):
    length = response.getheader("Content-Length")
    if length and int(length) > maximum:
        raise NetworkError("Upstream response exceeds limit")
    body = response.read(maximum + 1)
    if len(body) > maximum:
        raise NetworkError("Upstream response exceeds limit")
    return HttpResult(response.status, body, {key.lower(): value for key, value in response.getheaders()})


def internal_request(url, method="GET", headers=None, body=None, timeout=25, maximum=16 * 1024 * 1024):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise NetworkError("Invalid configured upstream")
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), body=body, headers=headers or {})
        return _read(connection.getresponse(), maximum)
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise NetworkError("Upstream request failed") from error
    finally:
        connection.close()


def external_request(url, method="GET", headers=None, body=None, timeout=25, maximum=16 * 1024 * 1024):
    """Pin a public DNS result into the TLS socket to prevent DNS rebinding."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443} or parsed.username or parsed.password:
        raise NetworkError("Only public HTTPS on port 443 is supported")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise NetworkError("Non-public network address denied")
        connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=timeout)
        raw = socket.create_connection((addresses[0][4][0], 443), timeout)
        try:
            connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
        except BaseException:
            raw.close()
            raise
        try:
            connection.request(method, (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), body=body, headers=headers or {})
            result = _read(connection.getresponse(), maximum)
            if result.headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                raise NetworkError("Upstream ignored identity encoding")
            return result
        finally:
            connection.close()
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise NetworkError("External request failed") from error
