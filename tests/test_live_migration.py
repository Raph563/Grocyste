import json
import pytest
from grocyste.hostutils import ManagerError, canonical, atomic_bytes
from grocyste.live_migration import migrate, normalize


def test_live_state_preserves_sessions_commands_and_archives_only_cancelled_shadow():
    original = {"schema": "mon-grocy-live-store-v1", "sessions": [{"id": "synthetic-session"}],
                "commands": ["synthetic-command"], "timers": [{"id": "synthetic-timer", "status": "cancelled"}]}
    result, archived = normalize(canonical(original))
    value = json.loads(result)
    assert archived == 1 and "timers" not in value
    assert value["sessions"] == original["sessions"] and value["commands"] == original["commands"]
    original["timers"][0]["status"] = "running"
    with pytest.raises(ManagerError, match="réconcilier"):
        normalize(canonical(original))
    with pytest.raises(ManagerError):
        normalize(b"invalid")


def live_fixture(tmp_path):
    source = tmp_path / "old/state/live-state.json"
    source.parent.mkdir(parents=True)
    (tmp_path / "old/code").mkdir()
    value = {"schema": "mon-grocy-live-store-v1", "sessions": [{"id": "synthetic-session", "revision": 1}],
             "commands": ["synthetic-command"], "timers": [{"id": "shadow", "status": "cancelled"}]}
    atomic_bytes(source, canonical(value))
    metadata = {"Id": "a" * 64, "Name": "/mon-grocy-live", "State": {"Running": True},
        "Config": {"Image": "mon-grocy-release-mon-grocy-release", "Labels": {
            "com.docker.compose.project": "mon-grocy-release", "com.docker.compose.service": "mon-grocy-release"}},
        "Mounts": [{"Type": "bind", "Source": str(source.parent), "Destination": "/state", "RW": True},
                   {"Type": "bind", "Source": str(tmp_path / "old/code"), "Destination": "/code", "RW": False}]}
    state = {"metadata": metadata, "calls": [], "stop_crash": False}
    def command(args, **kwargs):
        state["calls"].append(args)
        if args[1] == "ps":
            return metadata["Id"]
        if args[1] == "inspect":
            return json.dumps([metadata])
        if args[1] == "stop":
            # A final legacy tick before it stops must be retained.
            value["sessions"][0]["revision"] = 2
            atomic_bytes(source, canonical(value))
            metadata["State"]["Running"] = False
            if state["stop_crash"]:
                state["stop_crash"] = False
                raise RuntimeError("synthetic interruption")
        return ""
    return tmp_path / "home", source, state, command


def test_live_migration_captures_stopped_writer_and_replays_without_overwriting_progress(tmp_path):
    home, source, state, command = live_fixture(tmp_path)
    result = migrate(home, command)
    target = home / "state/live/live-state.json"
    value = json.loads(target.read_bytes())
    assert result["nativeTimerWrites"] == 0 and result["archivedCancelledShadowTimers"] == 1
    assert value["sessions"][0]["revision"] == 2
    assert "timers" in json.loads(source.read_bytes())
    assert json.loads((home / "receipts/live-migration/live-state.before.json").read_bytes())["sessions"][0]["revision"] == 1
    value["sessions"][0]["revision"] = 3
    atomic_bytes(target, canonical(value))
    count = sum(c[1] in {"update", "stop"} for c in state["calls"])
    assert migrate(home, command)["status"] == "noop"
    assert json.loads(target.read_bytes())["sessions"][0]["revision"] == 3
    assert sum(c[1] in {"update", "stop"} for c in state["calls"]) == count
    state["metadata"]["State"]["Running"] = True
    with pytest.raises(ManagerError, match="actif"):
        migrate(home, command)


def test_resume_stopped_writer_without_native_timer_writes(tmp_path):
    home, source, state, command = live_fixture(tmp_path)
    state["stop_crash"] = True
    with pytest.raises(RuntimeError):
        migrate(home, command)
    assert migrate(home, command)["status"] == "migrated"
    assert not any(c[1] in {"rm", "start"} for c in state["calls"])


def test_existing_live_destination_and_unknown_owner_refused(tmp_path):
    home, source, state, command = live_fixture(tmp_path)
    target = home / "state/live/live-state.json"
    atomic_bytes(target, canonical({"sessions": [{"id": "foreign"}], "commands": []}))
    with pytest.raises(ManagerError, match="déjà utilisé"):
        migrate(home, command)
    assert state["metadata"]["State"]["Running"]
    target.unlink()
    state["metadata"]["Config"]["Image"] = "foreign"
    with pytest.raises(ManagerError, match="non reconnu"):
        migrate(home, command)
    assert state["metadata"]["State"]["Running"]
