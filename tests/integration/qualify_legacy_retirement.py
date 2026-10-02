#!/usr/bin/env python3
"""Exercise retirement using five real, exclusively lab-prefixed containers.

Only inspect-name and image-contract fields are translated by the adapter. Every
container mutation uses a previously captured real ID of a lab-owned container.
No historical service binary, credential or production container is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grocyste.legacy_services import SERVICES, check_closed, retire
from lab_support import lab_root

LAB = lab_root()


def run(arguments, *, output=False, timeout=60):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Commande laboratoire refusée ou échouée")
    return result.stdout if output else ""


def main():
    work = LAB / "security" / ("legacy-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    config = work / "config"
    data = config / "data"
    data.mkdir(parents=True, mode=0o700)
    project = "grocyste-security-" + uuid.uuid4().hex[:10]
    prefix = project + "-"
    compose = work / "compose.yml"
    blocks = ["services:\n"]
    for name in (*SERVICES, "sentinel"):
        blocks.extend([f"  {name}:\n", "    image: local/grocyste-lab:1.0.0\n",
            f"    container_name: {prefix}{name}\n", "    restart: unless-stopped\n",
            "    network_mode: none\n", "    read_only: true\n", "    cap_drop: [ALL]\n",
            "    security_opt: [no-new-privileges:true]\n",
            '    command: ["python", "-c", "import time;time.sleep(600)"]\n',
            "    volumes:\n", f"      - {config}:/grocy-config\n"])
        if name == "nerdcore-update-api":
            blocks.extend(["    environment:\n", "      NERDCORE_UPDATE_TOKEN: synthetic-laboratory-only\n"])
    compose.write_text("".join(blocks))
    compose.chmod(0o600)
    environment = work / "fixture.env"
    environment.write_text("")
    environment.chmod(0o600)
    invocation = ["docker", "compose", "--project-directory", str(work), "--project-name", project,
                  "--env-file", str(environment), "-f", str(compose)]
    owned, actions = {}, []
    state = {"interrupt": True}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(410 if self.path == "/__nerdcore_update/status" else 404)
            self.end_headers()
        def log_message(self, *args):
            pass

    closure_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=closure_server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{closure_server.server_port}"

    def adapter(arguments, *, output=False, timeout=60):
        arguments = list(arguments)
        if arguments[:2] == ["docker", "ps"]:
            original = arguments[-1]
            name = original.removeprefix("name=^/").removesuffix("$")
            if name not in SERVICES:
                raise AssertionError("Inspection étrangère refusée")
            arguments[-1] = "name=^/" + prefix + name + "$"
            return run(arguments, output=output, timeout=timeout)
        if arguments[:2] == ["docker", "inspect"]:
            if arguments[-1] not in owned.values():
                raise AssertionError("Identité Docker étrangère refusée")
            rows = json.loads(run(arguments, output=True, timeout=timeout))
            name = rows[0]["Name"].removeprefix("/" + prefix)
            if name not in SERVICES:
                raise AssertionError("Nom Docker étranger refusé")
            rows[0]["Name"] = "/" + name
            rows[0]["Config"]["Image"] = "grocy-nerdcore-update-api" if name == "nerdcore-update-api" else "nerdstats-updater:synthetic"
            return json.dumps(rows)
        if arguments[:2] == ["docker", "compose"]:
            if (arguments[:9] != invocation[:9] or arguments[-3:] != ["config", "--format", "json"]
                    or not Path(arguments[arguments.index("-f") + 1]).resolve().is_relative_to(work)):
                raise AssertionError("Opération Compose étrangère refusée")
            return run(arguments, output=output, timeout=timeout)
        if arguments[:2] in (["docker", "update"], ["docker", "stop"], ["docker", "rm"]):
            identifier = arguments[-1]
            if identifier not in {owned[name] for name in SERVICES}:
                raise AssertionError("Mutation Docker étrangère refusée")
            if arguments[1] == "stop" and state["interrupt"]:
                state["interrupt"] = False
                raise RuntimeError("Interruption synthétique après persistance des définitions")
            actions.append({"action": arguments[1], "ownedLabId": identifier})
            return run(arguments, output=output, timeout=timeout)
        raise AssertionError("Commande non autorisée par l'adaptateur laboratoire")

    try:
        run(invocation + ["up", "-d", "--no-build", "--pull", "never"])
        for name in (*SERVICES, "sentinel"):
            metadata = json.loads(run(["docker", "inspect", prefix + name], output=True))[0]
            assert metadata["Name"] == "/" + prefix + name
            owned[name] = metadata["Id"]
        original = json.loads(run(invocation + ["config", "--format", "json"], output=True))
        try:
            retire(data, work / "receipts", origin, adapter, closure=check_closed)
            raise AssertionError("Interruption non exercée")
        except RuntimeError as error:
            assert "Interruption synthétique" in str(error)
        journal = work / "receipts/legacy-retirement/receipt.json"
        assert json.loads(journal.read_bytes())["status"] == "definitions-retired"
        assert "synthetic-laboratory-only" not in journal.read_text()
        result = retire(data, work / "receipts", origin, adapter, closure=check_closed)
        assert result["status"] == "retired" and result["secretVerifierRemoved"] is True
        action_count = len(actions)
        assert retire(data, work / "receipts", origin, adapter, closure=check_closed)["status"] == "retired"
        assert len(actions) == action_count
        actual = json.loads(run(invocation + ["config", "--format", "json"], output=True))
        assert actual == {**original, "services": {"sentinel": original["services"]["sentinel"]}}
        sentinel = json.loads(run(["docker", "inspect", prefix + "sentinel"], output=True))[0]
        assert sentinel["Id"] == owned["sentinel"] and sentinel["State"]["Running"]
        for name in SERVICES:
            assert run(["docker", "ps", "--all", "--quiet", "--filter", "name=^/" + prefix + name + "$"], output=True).strip() == ""
        report = {"suite": "real-docker-isolated-legacy-retirement", "createdAt": datetime.now(timezone.utc).isoformat(),
            "status": "passed", "ownedLegacyContainers": 4, "sentinelUnchanged": True,
            "composeOtherServicesUnchanged": True, "durableInterruptionResumed": True,
            "replayNoAdditionalContainerMutation": True, "legacyContainersAbsent": True,
            "credentialsInReport": False, "productionRequests": 0,
            "limits": ["Legacy image names and inspect Name are translated contract fixtures",
                "Container stop/removal/Compose parsing are real Docker operations with lab-owned IDs",
                "Closure 410 exercised on loopback HTTP fixture, not production HTTPS",
                "No cryptographic rotation: only the four owned verifiers and definitions are removed"]}
        (work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        for name, identifier in owned.items():
            # Cleanup is restricted to the captured disposable IDs, even on failure.
            run(["docker", "rm", "--force", identifier]) if run(["docker", "ps", "--all", "--quiet", "--no-trunc",
                "--filter", "id=" + identifier], output=True).strip() else None
        closure_server.shutdown()
        closure_server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
