"""SPDX-License-Identifier: GPL-3.0-or-later

Targeted Caddy migration, retaining every unrelated site and header. Tokens are
never parsed, returned or logged; the entire owned legacy route is removed.
"""
import re
from urllib.parse import urlsplit
from .hostutils import ManagerError


def _depth(line):
    # Count structural braces, ignoring quotes, escaped characters and comments.
    depth, quoted, escaped = 0, False, False
    for character in line:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character == "#":
            break
        elif not quoted:
            depth += (character == "{") - (character == "}")
    return depth


def _block(lines, start):
    depth = 0
    for end in range(start, len(lines)):
        depth += _depth(lines[end])
        if depth == 0:
            return end + 1
        if depth < 0:
            break
    raise ManagerError("invalid_proxy", "Bloc Caddy incomplet")


def migrate_caddy(content: bytes, origin: str, base_path: str):
    domain = urlsplit(origin).netloc
    if not domain or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", base_path):
        raise ManagerError("invalid_proxy", "Configuration proxy invalide")
    lines = content.decode("utf-8").splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines)
              if re.fullmatch(r"(?:https://)?" + re.escape(domain) + r"\s*\{\s*", line.strip())]
    if len(starts) != 1:
        raise ManagerError("ambiguous_proxy", "Le bloc du site Grocy doit être unique", 409)
    start, end = starts[0], _block(lines, starts[0])
    block = lines[start:end]
    result, i, legacy, existing_core, live = [], 0, 0, 0, 0
    while i < len(block):
        line = block[i]
        stripped = line.strip()
        if re.fullmatch(r"handle /__mon_grocy/live/v1/\*\s*\{", stripped):
            stop = _block(block, i)
            owned = block[i:stop]
            targets = [j for j, item in enumerate(owned) if re.fullmatch(
                r"reverse_proxy (?:mon-grocy-live|grocyste-live):8093", item.strip())]
            if len(targets) != 1 or any("reverse_proxy" in item and j not in targets for j, item in enumerate(owned)):
                raise ManagerError("ambiguous_live_proxy", "Route live non reconnue", 409)
            j = targets[0]
            owned[j] = owned[j].replace("mon-grocy-live:8093", "grocyste-live:8093")
            result.extend(owned)
            i, live = stop, live + 1
            continue
        if re.fullmatch(r"handle_path /__nerdcore_update/\*\s*\{", stripped):
            stop = _block(block, i)
            result.append("\t# Grocyste: ancien accès administrateur définitivement fermé.\n")
            result.append('\thandle /__nerdcore_update/* {\n\t\trespond "Route supprimée" 410\n\t}\n')
            i, legacy = stop, legacy + 1
            continue
        if re.fullmatch(r"handle " + re.escape(base_path) + r"/\*\s*\{", stripped):
            existing_core += 1
            stop = _block(block, i)
            owned = "".join(block[i:stop])
            if "reverse_proxy grocyste-core:8788" not in owned:
                raise ManagerError("proxy_drift", "Une route Grocyste étrangère existe déjà", 409)
        result.append(line)
        i += 1
    if legacy > 1 or existing_core > 1 or live > 1:
        raise ManagerError("ambiguous_proxy", "Routes de proxy dupliquées", 409)
    if not existing_core:
        insertion = f"\thandle {base_path}/* {{\n\t\treverse_proxy grocyste-core:8788\n\t}}\n"
        result.insert(1, insertion)
    return ("".join(lines[:start]) + "".join(result) + "".join(lines[end:])).encode("utf-8")
