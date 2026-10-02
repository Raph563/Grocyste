"""SPDX-License-Identifier: GPL-3.0-or-later

Small, dependency-free primitives shared by host installation and services.
Importing these helpers must not require the container's cryptography runtime.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path
import tempfile
import time


class ManagerError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManagerError("invalid_json", "Clé JSON dupliquée")
        result[key] = value
    return result


def load_json(data: bytes, limit: int = 1024 * 1024):
    if len(data) > limit:
        raise ManagerError("too_large", "Document trop volumineux", 413)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ManagerError("invalid_json", "JSON invalide") from exc


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600,
                 owner: tuple[int, int] | None = None):
    """Publish durable bytes, setting ownership before the atomic replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".grocyste-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.name == "nt":
                os.chmod(temporary, mode)
            else:
                if owner is not None:
                    current = os.fstat(stream.fileno())
                    if (current.st_uid, current.st_gid) != tuple(owner):
                        os.fchown(stream.fileno(), *owner)
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        raise ManagerError("unsafe_lock", "Le verrou ne peut pas être un lien")
    descriptor = os.open(path, flags, 0o600)
    started = time.monotonic()
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if not os.fstat(descriptor).st_size:
                os.write(descriptor, b"\0")
        else:
            import fcntl
        while True:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() - started >= timeout:
                    raise ManagerError("busy", "Une opération est déjà en cours", 409) from error
                time.sleep(0.05)
        yield
    finally:
        if locked and os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)
