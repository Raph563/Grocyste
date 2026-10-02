"""Process-safe companion state. This database is never Grocy's database."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


class StateConflict(Exception):
    pass


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "grocyste.sqlite3"

    @contextmanager
    def db(self, write=False):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=15000")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, user_id INTEGER NOT NULL,
                  cookie_hash TEXT NOT NULL, created REAL NOT NULL, touched REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                  namespace TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                  revision INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY(namespace,name));
                CREATE TABLE IF NOT EXISTS operations (
                  actor INTEGER NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
                  state TEXT NOT NULL, response TEXT, status INTEGER, created REAL NOT NULL,
                  PRIMARY KEY(actor,key));
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
                  kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS rate_limits (
                  name TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials (
                  provider TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, updated REAL NOT NULL);
            """)
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def issue_session(self, user_id, cookie):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        with self.db(True) as db:
            db.execute("DELETE FROM sessions WHERE created < ? OR touched < ?", (now - 43200, now - 1800))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (digest(token), digest(csrf), user_id, digest(cookie), now, now))
        return token, csrf

    def session(self, token, cookie, csrf=None):
        now = time.time()
        with self.db(True) as db:
            row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (digest(token),)).fetchone()
            if row is None or row["created"] < now - 43200 or row["touched"] < now - 1800:
                return None
            if not secrets.compare_digest(row["cookie_hash"], digest(cookie)):
                return None
            if csrf is not None and not secrets.compare_digest(row["csrf_hash"], digest(csrf)):
                return None
            db.execute("UPDATE sessions SET touched=? WHERE token_hash=?", (now, digest(token)))
            return dict(row)

    def revoke_session(self, token):
        with self.db(True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def rate_limit(self, name, limit, interval=60):
        window = int(time.time() // interval)
        with self.db(True) as db:
            db.execute("DELETE FROM rate_limits WHERE window < ?", (window - 2,))
            row = db.execute("SELECT * FROM rate_limits WHERE name=?", (name,)).fetchone()
            count = row["count"] + 1 if row and row["window"] == window else 1
            db.execute("INSERT INTO rate_limits VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET window=excluded.window,count=excluded.count", (name, window, count))
        return count <= limit

    def get(self, namespace, name, default=None):
        with self.db() as db:
            row = db.execute("SELECT value,revision FROM documents WHERE namespace=? AND name=?", (namespace, name)).fetchone()
        return (json.loads(row["value"]), row["revision"]) if row else (default, 0)

    def put(self, namespace, name, value, expected=None, quota_bytes=None, quota_documents=None):
        encoded = canonical(value)
        with self.db(True) as db:
            row = db.execute("SELECT revision,length(CAST(value AS BLOB)) AS bytes FROM documents WHERE namespace=? AND name=?", (namespace, name)).fetchone()
            revision = row["revision"] if row else 0
            if expected is not None and expected != revision:
                raise StateConflict("Revision changed")
            if quota_bytes is not None or quota_documents is not None:
                usage = db.execute("SELECT COUNT(*) AS count,COALESCE(SUM(length(CAST(value AS BLOB))),0) AS bytes FROM documents WHERE namespace=?", (namespace,)).fetchone()
                if (quota_documents is not None and usage["count"] + (0 if row else 1) > quota_documents) or (quota_bytes is not None and usage["bytes"] - (row["bytes"] if row else 0) + len(encoded.encode()) > quota_bytes):
                    raise StateConflict("Storage quota exceeded")
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?) ON CONFLICT(namespace,name) DO UPDATE SET value=excluded.value,revision=excluded.revision,updated=excluded.updated", (namespace, name, encoded, revision + 1, time.time()))
        return revision + 1

    def update(self, namespace, name, transform, default):
        with self.db(True) as db:
            row = db.execute("SELECT value,revision FROM documents WHERE namespace=? AND name=?", (namespace, name)).fetchone()
            value = json.loads(row["value"]) if row else default
            value = transform(value)
            revision = row["revision"] + 1 if row else 1
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?) ON CONFLICT(namespace,name) DO UPDATE SET value=excluded.value,revision=excluded.revision,updated=excluded.updated", (namespace, name, canonical(value), revision, time.time()))
        return value, revision

    def clear(self, namespace):
        with self.db(True) as db:
            db.execute("DELETE FROM documents WHERE namespace=?", (namespace,))

    def prune(self, namespace, maximum=800):
        with self.db(True) as db:
            db.execute("DELETE FROM documents WHERE namespace=? AND name NOT IN (SELECT name FROM documents WHERE namespace=? ORDER BY updated DESC LIMIT ?)", (namespace, namespace, maximum))

    def begin_operation(self, actor, key, payload):
        request_hash = digest(canonical(payload))
        with self.db(True) as db:
            row = db.execute("SELECT * FROM operations WHERE actor=? AND key=?", (actor, key)).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise StateConflict("Idempotency key already used for another request")
                if row["state"] != "done":
                    raise StateConflict("Operation requires reconciliation; do not retry blindly")
                return json.loads(row["response"]), row["status"]
            db.execute("INSERT INTO operations VALUES (?,?,?,'pending',NULL,NULL,?)", (actor, key, request_hash, time.time()))
        return None

    def finish_operation(self, actor, key, payload, status):
        with self.db(True) as db:
            db.execute("UPDATE operations SET state='done',response=?,status=? WHERE actor=? AND key=?", (canonical(payload), status, actor, key))

    def uncertain_operation(self, actor, key):
        with self.db(True) as db:
            db.execute("UPDATE operations SET state='needs-reconciliation' WHERE actor=? AND key=?", (actor, key))

    def operations(self, actor):
        with self.db() as db:
            rows = db.execute("SELECT key,state,created,status FROM operations WHERE actor=? ORDER BY created DESC LIMIT 100", (actor,)).fetchall()
        return [dict(row) for row in rows]

    def event(self, namespace, kind, payload):
        with self.db(True) as db:
            db.execute("INSERT INTO events(namespace,kind,payload,created) VALUES (?,?,?,?)", (namespace, kind, canonical(payload), time.time()))
            # Bounded event history; clients resync authoritative state after a gap.
            db.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM events)")

    def events(self, namespaces, after=0):
        placeholders = ",".join("?" for _ in namespaces)
        with self.db() as db:
            rows = db.execute(f"SELECT * FROM events WHERE id>? AND namespace IN ({placeholders}) ORDER BY id LIMIT 100", (after, *namespaces)).fetchall()
        return [{"id": row["id"], "namespace": row["namespace"], "kind": row["kind"], "data": json.loads(row["payload"]), "createdAt": row["created"]} for row in rows]

    def credential(self, provider, secret=None):
        from cryptography.fernet import Fernet
        key_path = self.directory / "credentials.key"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if secret is not None and not key_path.exists():
            key = Fernet.generate_key()
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(key)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass
        if not key_path.exists():
            return None
        cipher = Fernet(key_path.read_bytes())
        with self.db(secret is not None) as db:
            if secret is not None:
                db.execute("INSERT INTO credentials VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET ciphertext=excluded.ciphertext,updated=excluded.updated", (provider, cipher.encrypt(secret.encode()).decode(), time.time()))
                return None
            row = db.execute("SELECT ciphertext FROM credentials WHERE provider=?", (provider,)).fetchone()
        return cipher.decrypt(row["ciphertext"].encode()).decode() if row else None

    def credential_status(self):
        with self.db() as db:
            rows = db.execute("SELECT provider,updated FROM credentials ORDER BY provider").fetchall()
        return [{"provider": row["provider"], "configured": True, "updatedAt": row["updated"]} for row in rows]
