"""Resolve Grocy's ADMIN permission from its authoritative hierarchy."""


def admin_permission_id(rows):
    if not isinstance(rows, list):
        raise ValueError("Invalid permission hierarchy")
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name", row.get("permission_name"))
        if name == "ADMIN":
            identifier = row.get("id", row.get("permission_id"))
            if isinstance(identifier, bool):
                raise ValueError("Invalid permission identifier")
            identifier = int(identifier)
            if identifier <= 0:
                raise ValueError("Invalid permission identifier")
            matches.append(identifier)
    if len(matches) != 1:
        raise ValueError("ADMIN permission cannot be resolved uniquely")
    return matches[0]


def has_admin_permission(rows, identifier):
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and str(row.get("permission_id", row.get("permissions_id", ""))) == str(identifier)
               for row in rows)
