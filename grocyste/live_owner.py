"""SPDX-License-Identifier: GPL-3.0-or-later

Hold an OS lock for the complete lifetime of the single live-state writer.
"""
import os
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import threading

from .hostutils import file_lock


def revision(packages: Path, configuration: Path):
    """Only live code/config changes restart the owner, not unrelated packages."""
    value = json.loads((packages / "current.json").read_bytes())
    row = value.get("addons", {}).get("shared-timers")
    if not isinstance(row, dict):
        raise RuntimeError("Registre SharedTimers absent")
    token = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()
        + configuration.read_bytes()).hexdigest()
    return token, row.get("enabled") is True


def stop_child(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(packages, configuration, stopped, spawn=None):
    spawn = spawn or (lambda: subprocess.Popen(["node", "/app/scripts/live-bridge.mjs"]))
    child, previous = None, None
    try:
        while not stopped.is_set():
            current, enabled = revision(packages, configuration)
            if current != previous:
                stop_child(child)
                child = spawn() if enabled else None
                previous = current
            elif child is not None and child.poll() is not None:
                raise RuntimeError("Le runtime live s'est arrêté")
            stopped.wait(0.5)
    finally:
        stop_child(child)


def main():
    directory = Path(os.environ.get("LIVE_STATE_DIRECTORY", "/state"))
    packages = Path(os.environ.get("PACKAGES_DIR", "/packages"))
    configuration = Path(os.environ.get("INSTANCE_CONFIG_FILE", "/instance/instance.json"))
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with file_lock(directory / ".live-owner.lock", timeout=1):
        supervise(packages, configuration, stopped)


if __name__ == "__main__":
    main()
