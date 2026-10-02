#!/usr/bin/env python3
"""Exercise disposable Grocy instances and write sanitized, reproducible evidence.

No request is permitted outside the fixed loopback laboratory ports. Credential
material lives exclusively in lab/private/grocy-credentials.json (mode 0600).
"""
from __future__ import annotations
import argparse
import http.cookiejar
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request

LAB = Path('/home/wwadmin/grocyste-work/lab')


class Client:
    def __init__(self, base: str):
        parsed = urllib.parse.urlsplit(base)
        if parsed.hostname != '127.0.0.1' or parsed.port not in (19283, 19284, 19285):
            raise ValueError('Only explicitly isolated Grocy laboratory URLs are accepted')
        self.base = base.rstrip('/')
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, body=None, key=None, headers=None):
        hdr = {'User-Agent': 'grocyste-lab-baseline/1.0'}
        hdr.update(headers or {})
        if key:
            hdr['GROCY-API-KEY'] = key
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            hdr['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, body, hdr, method=method)
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.read()
        try:
            content = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            content = raw.decode(errors='replace')
        return response.status, content, response.geturl(), dict(response.headers)

    def login(self, username: str, password: str):
        form = urllib.parse.urlencode({'username': username, 'password': password}).encode()
        status, _, url, _ = self.request('POST', '/login', form,
                                        headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if status != 200 or 'invalid=true' in url:
            raise RuntimeError('Synthetic lab login failed at ' + self.base + ' with status ' + str(status))
        status, user, _, _ = self.request('GET', '/api/user')
        if status != 200:
            raise RuntimeError('Lab session was not authenticated')
        return user[0] if isinstance(user, list) else user

    def create_key(self, description: str):
        status, html, url, _ = self.request('GET', '/manageapikeys/new?description=' +
                                          urllib.parse.quote(description, safe=''))
        match = re.search(r'[?&]key=(\d+)', url)
        if status != 200 or not match or not isinstance(html, str):
            raise RuntimeError('Grocy UI key creation contract changed')
        key_id = int(match.group(1))
        match = re.search(r'data-apikey-id="' + str(key_id) + r'"\s+data-apikey-key="([0-9a-f]{48})"', html)
        if not match:
            raise RuntimeError('Grocy UI key extraction contract changed')
        return {'id': key_id, 'key': match.group(1), 'visible_key_count': len(re.findall('data-apikey-id=', html))}


def seed_lab_user(target: str, password: str):
    result = subprocess.run(['docker', 'exec', '-i', 'grocyste-lab-' + target, 'php', '-r',
                             'echo password_hash(stream_get_contents(STDIN), PASSWORD_ARGON2ID);'],
                            input=password, text=True, capture_output=True, check=True)
    db = sqlite3.connect(LAB / target / 'data/grocy.db')
    user = db.execute('SELECT id FROM users WHERE username=?', ('grocyste-lab-admin',)).fetchone()
    if user:
        db.execute('UPDATE users SET password=? WHERE id=?', (result.stdout.strip(), user[0]))
        user_id = user[0]
    else:
        cursor = db.execute('INSERT INTO users(username,first_name,last_name,password,picture_file_name) VALUES (?,?,?,?,NULL)',
                            ('grocyste-lab-admin', 'Grocyste', 'Lab', result.stdout.strip()))
        user_id = cursor.lastrowid
    admin = db.execute("SELECT id FROM permission_hierarchy WHERE name='ADMIN'").fetchone()[0]
    db.execute('DELETE FROM user_permissions WHERE user_id=?', (user_id,))
    db.execute('INSERT INTO user_permissions(user_id,permission_id) VALUES (?,?)', (user_id, admin))
    db.commit()
    db.close()
    return 'grocyste-lab-admin'


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    evidence = []
    credentials_file = LAB / 'private/grocy-credentials.json'
    credentials = json.loads(credentials_file.read_text()) if credentials_file.exists() else {}

    def check(name, actual, expected, target, **metadata):
        evidence.append({'name': name, 'target': target, 'passed': actual == expected,
                         'actual': actual, 'expected': expected, **metadata})

    for target, base in [('vanilla', 'http://127.0.0.1:19283'),
                         ('clone', 'http://127.0.0.1:19284'),
                         ('subpath', 'http://127.0.0.1:19285/grocy')]:
        anon = Client(base)
        status, _, _, _ = anon.request('GET', '/')
        check('initial_migrations_and_login_page', status, 200, target)
        status, _, _, _ = anon.request('GET', '/api/user')
        check('anonymous_api_denied', status, 401, target)
        status, _, _, _ = anon.request('GET', '/api/user', key='f' * 48)
        check('invalid_api_key_denied', status, 401, target)
        admin = Client(base)
        if target not in credentials:
            password = secrets.token_urlsafe(40)
            # Fixture preparation mutates authentication tables only in disposable volumes.
            username = seed_lab_user(target, password)
            current = admin.login(username, password)
            admin_key = admin.create_key('Grocyste lab private administrator')
            credentials[target] = {'base_url': base, 'admin_username': username, 'admin_password': password,
                                   'admin_id': current['id'], 'admin_key': admin_key['key']}
            credentials_file.write_text(json.dumps(credentials, indent=2) + '\n')
            credentials_file.chmod(0o600)
        else:
            admin.login(credentials[target]['admin_username'], credentials[target]['admin_password'])
        status, info, _, _ = admin.request('GET', '/api/system/info')
        check('pinned_stable_version', info.get('grocy_version', {}).get('Version'), '4.7.1', target)
        status, spec, _, _ = admin.request('GET', '/api/openapi/specification')
        check('instance_openapi_contract', status, 200, target)
        check('instance_base_path', urllib.parse.urlsplit(spec['servers'][0]['url']).path,
              '/grocy/api' if target == 'subpath' else '/api', target)
        status, _, _, _ = admin.request('POST', '/api/objects/api_keys', {})
        check('generic_api_key_creation_rejected', status, 400, target)
        hierarchy = admin.request('GET', '/api/objects/permission_hierarchy')[1]
        admin_permission = next(item['id'] for item in hierarchy if item['name'] == 'ADMIN')
        suffix = secrets.token_hex(4)
        username = 'grocyste-lab-limited-' + suffix
        password = secrets.token_urlsafe(40)
        status, _, _, _ = admin.request('POST', '/api/users',
                                        {'username': username, 'first_name': 'Synthetic', 'last_name': 'Limited',
                                         'password': password, 'picture_file_name': None})
        check('create_service_via_official_api', status, 204, target)
        users = admin.request('GET', '/api/users?query[]=' + urllib.parse.quote('username=' + username))[1]
        limited_id = next(user['id'] for user in users if user['username'] == username)
        status, _, _, _ = admin.request('PUT', f'/api/users/{limited_id}/permissions', {'permissions': []})
        check('clear_default_service_permissions', status, 204, target)
        limited = Client(base)
        limited.login(username, password)
        key = limited.create_key('Grocyste isolated bootstrap')
        check('bootstrap_does_not_read_other_user_keys', key['visible_key_count'], 1, target)
        status, _, _, _ = limited.request('GET', f'/api/users/{limited_id}/permissions')
        check('limited_permission_read_denied', status, 403, target)
        status, _, _, _ = limited.request('POST', '/api/objects/products', {'name': 'Forbidden synthetic product'})
        check('limited_master_data_write_denied', status, 403, target)
        pure_key = Client(base)
        status, user, _, _ = pure_key.request('GET', '/api/user', key=key['key'])
        check('private_service_key_identifies_service', user[0]['id'] if status == 200 else status, limited_id, target)
        status, _, _, _ = admin.request('PUT', f'/api/users/{limited_id}/permissions', {'permissions': [admin_permission]})
        check('grant_admin_after_private_bootstrap', status, 204, target)
        status, _, _, _ = pure_key.request('GET', f'/api/users/{limited_id}/permissions', key=key['key'])
        check('service_key_inherits_granted_permissions', status, 200, target)
        admin.request('PUT', f'/api/users/{limited_id}/permissions', {'permissions': []})
        status, _, _, _ = pure_key.request('GET', f'/api/users/{limited_id}/permissions', key=key['key'])
        check('permission_revocation_applies_to_existing_key', status, 403, target)
        status, _, _, _ = limited.request('GET', f'/api/users/{limited_id}/permissions',
                                          key=credentials[target]['admin_key'])
        check('limited_session_precedes_admin_header', status, 403, target)
        limited.request('GET', '/logout')
        status, _, _, _ = limited.request('GET', '/api/user')
        check('logout_invalidates_session', status, 401, target)
        admin.request('DELETE', f'/api/objects/api_keys/{key["id"]}')
        status, _, _, _ = pure_key.request('GET', '/api/user', key=key['key'])
        check('revocation_invalidates_service_key', status, 401, target)
        if target == 'clone':
            limited.login(username, password)
            victim = admin.create_key('Synthetic disposable IDOR victim')
            status, _, _, _ = limited.request('DELETE', f'/api/objects/api_keys/{victim["id"]}')
            revoked = Client(base).request('GET', '/api/user', key=victim['key'])[0]
            evidence.append({'name': 'upstream_api_key_owner_check', 'target': target,
                             'passed': status == 403, 'actual': status, 'expected': 403,
                             'finding': 'UPSTREAM-IDOR-001', 'victim_key_revoked': revoked == 401,
                             'synthetic_records_only': True})
        admin.request('DELETE', f'/api/users/{limited_id}')

    report = {'suite': 'official-grocy-baseline', 'credentials_in_report': False,
              'tests': evidence, 'passed': sum(item['passed'] for item in evidence),
              'failed': sum(not item['passed'] for item in evidence),
              'production_requests': 0, 'destructive_targets': ['vanilla', 'clone', 'subpath']}
    (LAB / 'reports/grocy-baseline.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'suite': report['suite'], 'passed': report['passed'], 'failed': report['failed'],
                      'findings': [item['finding'] for item in evidence if 'finding' in item]}))


if __name__ == '__main__':
    main()
