"""Exercise old live state migration on one synthetic Docker container only."""
import json
from pathlib import Path
import subprocess
import sys
import uuid

LAB = Path('/home/wwadmin/grocyste-work/lab')
ROOT = LAB / 'core-snapshot'
sys.path.insert(0, str(ROOT))
from grocyste.hostutils import atomic_bytes, canonical, load_json
from grocyste.live_migration import migrate


def main():
    directory = LAB / 'private' / ('live-migration-' + uuid.uuid4().hex)
    directory.mkdir(parents=True, mode=0o700)
    code, state, home = directory / 'code', directory / 'state', directory / 'home'
    code.mkdir(); state.mkdir(); (home / 'receipts').mkdir(parents=True)
    original = canonical({'schema': 'mon-grocy-live-store-v1', 'sessions': [{'id': 'synthetic-session', 'status': 'paused'}],
                          'commands': ['synthetic-command'], 'timers': [{'id': 'synthetic-shadow', 'state': 'cancelled'}]})
    atomic_bytes(state / 'live-state.json', original)
    name = 'grocyste-lab-live-migration-' + uuid.uuid4().hex[:12]
    image = 'mon-grocy-release-mon-grocy-release'
    identifier = subprocess.check_output(['docker', 'run', '-d', '--name', name, '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--restart', 'unless-stopped',
        '--label', 'com.docker.compose.project=mon-grocy-release', '--label', 'com.docker.compose.service=mon-grocy-release',
        '-v', str(code) + ':/code:ro', '-v', str(state) + ':/state', '--entrypoint', '/bin/sh', image,
        '-c', 'sleep 300'], text=True).strip()
    results = []

    def command(arguments, *, output=False, timeout=30):
        args = list(arguments)
        if args[:2] == ['docker', 'ps']:
            assert args[-1] == 'name=^/mon-grocy-live$'
            args[-1] = 'name=^/' + name + '$'
        else:
            assert args[:2] in (['docker', 'inspect'], ['docker', 'update'], ['docker', 'stop'])
            assert args[-1] == identifier, 'Mutation or inspection outside owned fixture refused'
        run = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if run.returncode:
            raise RuntimeError('Synthetic Docker operation failed')
        if args[:2] == ['docker', 'inspect']:
            value = json.loads(run.stdout)
            assert len(value) == 1 and value[0]['Id'] == identifier and value[0]['Name'] == '/' + name
            value[0]['Name'] = '/mon-grocy-live'
            return json.dumps(value)
        return run.stdout if output else None

    try:
        calls = []
        assert migrate(home, lambda *args, **kwargs: calls.append(args))['status'] == 'not-selected' and not calls
        results.append({'name': 'unselected_instance_does_not_probe_or_stop_global_owner', 'passed': True})
        result = migrate(home, command, selected=True)
        migrated = load_json((home / 'state/live/live-state.json').read_bytes())
        assert result['status'] == 'migrated' and result['sessions'] == 1 and result['nativeTimerWrites'] == 0
        assert migrated['sessions'] == load_json(original)['sessions'] and migrated['commands'] == ['synthetic-command']
        assert 'timers' not in migrated and migrated['timerReceipts'] == {}
        assert (state / 'live-state.json').read_bytes() == original
        results.append({'name': 'sessions_commands_preserved_cancelled_shadow_archived_no_native_write', 'passed': True})
        current = json.loads(subprocess.check_output(['docker', 'inspect', identifier]))[0]
        assert not current['State']['Running'] and current['HostConfig']['RestartPolicy']['Name'] == 'no'
        results.append({'name': 'captured_owner_stopped_and_restart_disabled', 'passed': True})
        migrated['sessions'][0]['status'] = 'finished'
        atomic_bytes(home / 'state/live/live-state.json', canonical(migrated))
        assert migrate(home, command)['status'] == 'noop'
        assert load_json((home / 'state/live/live-state.json').read_bytes()) == migrated
        results.append({'name': 'replay_preserves_new_owner_progress', 'passed': True})
        passed = True
    finally:
        # Remove this exact fixture ID only; retain private state and receipts.
        subprocess.run(['docker', 'rm', '-f', identifier], check=True, capture_output=True)
    report = {'suite': 'real-legacy-live-migration', 'passed': passed, 'checks': results,
              'syntheticOnly': True, 'productionRequests': 0, 'nativeTimerWrites': 0,
              'identityAdapter': 'only the fixture container Name is translated for the legacy contract'}
    destination = LAB / 'reports/browser/live-migration.json'
    destination.write_bytes(canonical(report)); destination.chmod(0o600)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
