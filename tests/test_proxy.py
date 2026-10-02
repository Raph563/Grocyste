"""SPDX-License-Identifier: GPL-3.0-or-later"""
import pytest
from grocyste.proxy import migrate_caddy
from grocyste.manager import ManagerError


def test_targeted_proxy_migration_and_no_secret_reactivation():
    original = b'''other.example.org {
    reverse_proxy unrelated:8080
}
grocy.example.org {
    handle_path /__nerdcore_update/* {
        reverse_proxy nerdcore-update-api:8787 {
            header_up X-NerdCore-Token "synthetic-private-token"
        }
    }
    handle /__mon_grocy/live/v1/* {
        reverse_proxy mon-grocy-live:8093
    }
    reverse_proxy grocy:80
}
'''
    result = migrate_caddy(original, "https://grocy.example.org", "/__grocyste")
    assert b"synthetic-private-token" not in result
    assert b"X-NerdCore-Token" not in result
    assert b"respond \"Route supprim" in result
    assert b"handle /__mon_grocy/live/v1/*" in result
    assert b"reverse_proxy grocyste-live:8093" in result
    assert result.startswith(b"other.example.org {\n    reverse_proxy unrelated:8080\n}\n")
    assert migrate_caddy(result, "https://grocy.example.org", "/__grocyste") == result


def test_missing_duplicate_or_foreign_route_refused():
    with pytest.raises(ManagerError):
        migrate_caddy(b"other.org {\n}\n", "https://grocy.example.org", "/__grocyste")
    with pytest.raises(ManagerError):
        migrate_caddy(b"grocy.example.org {\n}\ngrocy.example.org {\n}\n", "https://grocy.example.org", "/__grocyste")
    with pytest.raises(ManagerError):
        migrate_caddy(b"grocy.example.org {\nhandle /__grocyste/* {\nreverse_proxy other:1234\n}\n}\n", "https://grocy.example.org", "/__grocyste")
