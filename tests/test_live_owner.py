"""Live package activation, disable and rollback use one supervised writer."""
import json
import threading
import time
import pytest
from grocyste.hostutils import atomic_bytes, canonical
from grocyste.live_owner import revision, supervise


def test_only_live_package_and_instance_config_change_revision(tmp_path):
    configuration = tmp_path / "instance.json"
    configuration.write_text("{}")
    registry = {"addons": {"shared-timers": {"version": "1.0.0", "enabled": True}}}
    atomic_bytes(tmp_path / "current.json", canonical(registry))
    original = revision(tmp_path, configuration)
    registry["addons"]["other"] = {"version": "2.0.0"}
    atomic_bytes(tmp_path / "current.json", canonical(registry))
    assert revision(tmp_path, configuration) == original
    atomic_bytes(configuration, b'{"recipePackageMeasures":[]}')
    assert revision(tmp_path, configuration) != original
    atomic_bytes(tmp_path / "current.json", b"corrupt")
    with pytest.raises(ValueError):
        revision(tmp_path, configuration)


def test_supervisor_restarts_on_update_and_rollback_without_overlapping_writers(tmp_path):
    configuration = tmp_path / "instance.json"
    configuration.write_text("{}")
    stopped = threading.Event()
    children, errors = [], []
    def activate(version, enabled=True):
        atomic_bytes(tmp_path / "current.json", canonical({"addons": {
            "shared-timers": {"version": version, "enabled": enabled}}}))
    class Child:
        def __init__(self):
            self.running = True
        def poll(self):
            return None if self.running else 0
        def terminate(self):
            self.running = False
        def wait(self, timeout):
            assert not self.running
            return 0
    def spawn():
        assert not any(child.running for child in children)
        child = Child()
        children.append(child)
        return child
    def run():
        try:
            supervise(tmp_path, configuration, stopped, spawn=spawn)
        except BaseException as error:
            errors.append(error)
    def until(condition):
        deadline = time.monotonic() + 3
        while not condition() and not errors and time.monotonic() < deadline:
            time.sleep(.02)
        assert condition() and not errors
    activate("1.0.0")
    thread = threading.Thread(target=run)
    thread.start()
    try:
        until(lambda: len(children) == 1)
        activate("1.1.0")
        until(lambda: len(children) == 2)
        activate("1.0.0")
        until(lambda: len(children) == 3)
        activate("1.0.0", enabled=False)
        until(lambda: not any(child.running for child in children))
        activate("1.0.0")
        until(lambda: len(children) == 4)
        atomic_bytes(tmp_path / "current.json", b"corrupted")
        thread.join(timeout=3)
        assert errors and not any(child.running for child in children)
    finally:
        stopped.set()
        thread.join(timeout=4)
    assert not thread.is_alive() and not any(child.running for child in children)
