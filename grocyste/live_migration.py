"""SPDX-License-Identifier: GPL-3.0-or-later

Preserve the known legacy live sidecar without writing Grocy data. Cancelled
shadow timers are archived; any other shadow timer requires reconciliation.
"""
from pathlib import Path
import os
from .hostutils import ManagerError, atomic_bytes, canonical, file_lock, load_json
from .legacy_services import inspect_optional, sha


def normalize(data):
    value = load_json(data, 8 * 1024 * 1024)
    if (not isinstance(value, dict) or value.get("schema") != "mon-grocy-live-store-v1"
            or not isinstance(value.get("sessions"), list) or not isinstance(value.get("commands"), list)
            or not isinstance(value.get("timers", []), list)):
        raise ManagerError("invalid_live_state", "État live historique invalide", 409)
    timers = value.get("timers", [])
    if any(not isinstance(t, dict) or t.get("status", t.get("state")) != "cancelled" for t in timers):
        raise ManagerError("live_timer_reconciliation", "Minuteurs historiques à réconcilier avant activation", 409)
    result = {k: v for k, v in value.items() if k not in {"timers", "legacyTimersImported"}}
    if not isinstance(result.get("timerReceipts", {}), dict):
        raise ManagerError("invalid_live_state", "Journal de minuteurs invalide", 409)
    result.setdefault("timerReceipts", {})
    return canonical(result), len(timers)


def migrate(home: Path, command):
    directory = home / "receipts/live-migration"
    journal = directory / "receipt.json"
    target = home / "state/live/live-state.json"
    with file_lock(home / "receipts/.live-migration.lock"):
        container = inspect_optional(command, "mon-grocy-live")
        if journal.exists():
            receipt = load_json(journal.read_bytes())
            if receipt.get("schema") != 1 or receipt.get("target") != str(target):
                raise ManagerError("invalid_live_receipt", "Reçu de migration live invalide", 409)
            if receipt.get("status") == "migrated":
                # The new owner may have legitimately advanced sessions since migration.
                if container and (container["Id"] != receipt["oldContainerId"] or container["State"]["Running"]):
                    raise ManagerError("old_live_active", "Un propriétaire live historique est actif", 409)
                return {"status": "noop", "receipt": str(journal)}
        else:
            if container is None:
                return {"status": "absent"}
            labels = container.get("Config", {}).get("Labels") or {}
            mounts = container.get("Mounts", [])
            sources = [m for m in mounts if m.get("Type") == "bind" and m.get("Destination") == "/state" and m.get("RW") is True]
            code = [m for m in mounts if m.get("Type") == "bind" and m.get("Destination") == "/code" and m.get("RW") is False]
            if (container.get("Config", {}).get("Image") != "mon-grocy-release-mon-grocy-release"
                    or labels.get("com.docker.compose.project") != "mon-grocy-release"
                    or labels.get("com.docker.compose.service") != "mon-grocy-release"
                    or len(sources) != 1 or len(code) != 1
                    or Path(code[0]["Source"]).resolve().parent != Path(sources[0]["Source"]).resolve().parent):
                raise ManagerError("unknown_live_owner", "Propriétaire live historique non reconnu", 409)
            source = Path(sources[0]["Source"]) / "live-state.json"
            if source.is_symlink() or not source.is_file() or source.resolve() == target.resolve():
                raise ManagerError("invalid_live_state", "Fichier live historique irrégulier", 409)
            original = source.read_bytes()
            normalized, archived = normalize(original)
            if target.exists():
                current = load_json(target.read_bytes(), 8 * 1024 * 1024)
                if (current.get("sessions") or current.get("commands") or current.get("timerReceipts")
                        or current.get("timers")) and target.read_bytes() != normalized:
                    raise ManagerError("live_destination_drift", "État live de destination déjà utilisé", 409)
            directory.mkdir(mode=0o700, parents=True)
            atomic_bytes(directory / "live-state.before.json", original)
            receipt = {"schema": 1, "status": "prepared", "oldContainerId": container["Id"],
                       "source": str(source), "target": str(target), "previewSourceSha256": sha(original)}
            atomic_bytes(journal, canonical(receipt))
        if container is None or container["Id"] != receipt["oldContainerId"]:
            raise ManagerError("live_owner_drift", "Le propriétaire live a changé", 409)
        command(["docker", "update", "--restart=no", container["Id"]], timeout=30)
        command(["docker", "stop", "--time", "20", container["Id"]], timeout=30)
        confirmed = inspect_optional(command, "mon-grocy-live")
        if confirmed is None or confirmed["Id"] != receipt["oldContainerId"] or confirmed["State"]["Running"]:
            raise ManagerError("old_live_active", "Le propriétaire live historique n'est pas arrêté", 503)
        source = Path(receipt["source"])
        original = source.read_bytes()
        normalized, archived = normalize(original)
        # Capture the final snapshot after stopping the writer, retaining the preview too.
        atomic_bytes(directory / "live-state.stopped.json", original)
        atomic_bytes(directory / "live-state.normalized.json", normalized)
        if target.exists():
            current = load_json(target.read_bytes(), 8 * 1024 * 1024)
            if (current.get("sessions") or current.get("commands") or current.get("timerReceipts") or current.get("timers")) and target.read_bytes() != normalized:
                raise ManagerError("live_destination_drift", "État live de destination modifié", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_bytes(target, normalized, owner=(1000, 1000) if os.name != "nt" and os.geteuid() == 0 else None)
        receipt.update(status="migrated", sourceSha256=sha(original), targetSha256=sha(normalized),
                       archivedCancelledShadowTimers=archived, nativeTimerWrites=0,
                       sessions=len(load_json(normalized, 8 * 1024 * 1024)["sessions"]))
        atomic_bytes(journal, canonical(receipt))
        return {"status": "migrated", "receipt": str(journal), "nativeTimerWrites": 0,
                "archivedCancelledShadowTimers": archived, "sessions": receipt["sessions"]}
