"""Explicit private laboratory root for opt-in integration qualification."""
import os
from pathlib import Path


def lab_root():
    value = os.environ.get("GROCYSTE_LAB_ROOT")
    if not value:
        raise SystemExit("Définir GROCYSTE_LAB_ROOT vers le laboratoire privé isolé.")
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or root == Path(root.anchor):
        raise SystemExit("Le laboratoire doit être un répertoire absolu distinct.")
    root = root.resolve(strict=True)
    if not all((root / child).is_dir() for child in ("private", "security")):
        raise SystemExit("Le laboratoire doit contenir private/ et security/.")
    return root
