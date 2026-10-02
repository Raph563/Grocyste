"""Synthetic retirement checks: scoped configuration, crash recovery, no secrets."""
import json
from pathlib import Path
import pytest
from grocyste.hostutils import ManagerError
from grocyste.legacy_services import retire, strip_services


def fixture(tmp_path):
    data = tmp_path / "config/data"
    data.mkdir(parents=True)
    compose = tmp_path / "compose.yml"
    before = b"services:\n  grocy:\n    image: synthetic-grocy\n  nerdcore-update-api:\n    image: grocy-nerdcore-update-api\n    environment:\n      NERDCORE_UPDATE_TOKEN: synthetic-private-secret\n  caddy:\n    image: synthetic-caddy\nnetworks:\n  default: {}\n"
    compose.write_bytes(before)
    identifier = "a" * 64
    metadata = {"Id": identifier, "Name": "/nerdcore-update-api",
        "Config": {"Image": "grocy-nerdcore-update-api", "Env": ["NERDCORE_UPDATE_TOKEN=synthetic-private-secret"],
            "Labels": {"com.docker.compose.project": "synthetic", "com.docker.compose.service": "nerdcore-update-api",
                "com.docker.compose.project.working_dir": str(tmp_path), "com.docker.compose.project.config_files": str(compose)}},
        "Mounts": [{"Type": "bind", "RW": True, "Source": str(data.parent), "Destination": "/opt/grocy/config"}]}
    state = {"present": True, "metadata": metadata, "calls": [], "crash": None}

    def command(args, **kwargs):
        state["calls"].append(args)
        if args[:2] == ["docker", "ps"]:
            return identifier if state["present"] and args[-1] == "name=^/nerdcore-update-api$" else ""
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([state["metadata"]])
        if args[:2] == ["docker", "compose"]:
            contents = Path(args[args.index("-f") + 1]).read_text()
            services = {"grocy": {"image": "synthetic-grocy"}, "caddy": {"image": "synthetic-caddy"}}
            if "  nerdcore-update-api:" in contents:
                services["nerdcore-update-api"] = {"image": "grocy-nerdcore-update-api"}
            if state.get("foreign_change") and "candidate" in args[args.index("-f") + 1]:
                services["grocy"]["image"] = "changed"
            return json.dumps({"name": "synthetic", "services": services})
        if state["crash"] == args[1]:
            state["crash"] = None
            raise RuntimeError("synthetic interruption")
        if args[:2] == ["docker", "rm"]:
            state["present"] = False
        return ""
    return data, compose, before, state, command


def test_scoped_retirement_replays_and_invalidates_verifier(tmp_path):
    data, compose, before, state, command = fixture(tmp_path)
    receipts = tmp_path / "receipts"
    result = retire(data, receipts, "https://synthetic.example", command, closure=lambda _: None)
    assert result["status"] == "retired" and result["secretVerifierRemoved"]
    assert compose.read_bytes() == strip_services(before, {"nerdcore-update-api"})
    assert b"synthetic-private-secret" not in compose.read_bytes()
    assert b"synthetic-private-secret" not in Path(result["receipt"]).read_bytes()
    assert (receipts / "legacy-retirement/compose-0.before").read_bytes() == before
    mutations = [c for c in state["calls"] if c[1] in {"update", "stop", "rm"}]
    assert [c[1] for c in mutations] == ["update", "stop", "rm"]
    assert all(c[-1] == "a" * 64 for c in mutations)
    count = len(mutations)
    assert retire(data, receipts, "https://synthetic.example", command, closure=lambda _: None)["status"] == "retired"
    assert len([c for c in state["calls"] if c[1] in {"update", "stop", "rm"}]) == count


@pytest.mark.parametrize("stage", ["update", "stop", "rm"])
def test_resume_after_interruption_never_restores_legacy(tmp_path, stage):
    data, compose, before, state, command = fixture(tmp_path)
    state["crash"] = stage
    with pytest.raises(RuntimeError):
        retire(data, tmp_path / "receipts", "https://synthetic.example", command, closure=lambda _: None)
    assert compose.read_bytes() == strip_services(before, {"nerdcore-update-api"})
    result = retire(data, tmp_path / "receipts", "https://synthetic.example", command, closure=lambda _: None)
    assert result["status"] == "retired" and not state["present"]
    assert not any("start" in c or "up" in c for c in state["calls"])


def test_foreign_service_or_compose_changes_fail_before_write(tmp_path):
    data, compose, before, state, command = fixture(tmp_path)
    state["metadata"]["Config"]["Image"] = "foreign-image"
    with pytest.raises(ManagerError, match="non reconnu"):
        retire(data, tmp_path / "receipts", "https://synthetic.example", command, closure=lambda _: None)
    assert compose.read_bytes() == before and state["present"]
    state["metadata"]["Config"]["Image"] = "grocy-nerdcore-update-api"
    state["foreign_change"] = True
    with pytest.raises(ManagerError, match="non vérifié"):
        retire(data, tmp_path / "receipts", "https://synthetic.example", command, closure=lambda _: None)
    assert compose.read_bytes() == before and state["present"]


def test_closure_required_before_backup_or_container_mutation(tmp_path):
    data, compose, before, state, command = fixture(tmp_path)
    def reject(_):
        raise ManagerError("legacy_route_open", "synthetic route open")
    with pytest.raises(ManagerError):
        retire(data, tmp_path / "receipts", "https://synthetic.example", command, closure=reject)
    assert compose.read_bytes() == before and state["present"]
    assert not any(c[1] in {"update", "stop", "rm"} for c in state["calls"])


@pytest.mark.parametrize("content", [b"services: {nerdcore: {}}\n", b"services:\n  nerdcore: {}\n",
    b"services:\n  nerdcore:\n  nerdcore:\n", b"services:\n\tnerdcore:\n"])
def test_ambiguous_yaml_refused(content):
    with pytest.raises(ManagerError):
        strip_services(content, {"nerdcore"})


def test_new_container_after_preparation_requires_reconciliation(tmp_path):
    data, compose, before, state, command = fixture(tmp_path)
    state["crash"] = "stop"
    with pytest.raises(RuntimeError):
        retire(data, tmp_path / "receipts", "https://synthetic.example", command, closure=lambda _: None)
    state["metadata"]["Id"] = "b" * 64
    with pytest.raises(ManagerError, match="changé"):
        retire(data, tmp_path / "receipts", "https://synthetic.example", command, closure=lambda _: None)
    assert state["present"]
