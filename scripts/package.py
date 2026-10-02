#!/usr/bin/env python3
"""SPDX-License-Identifier: GPL-3.0-or-later

Build reproducible signed packages. Private keys are files outside repositories;
this command never prints them. Package contents are an explicit file allowlist.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grocyste.manager import canonical, safe_member, validate_manifest, verify_archive
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption


def signing_key(path: Path):
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_bytes().strip(), validate=True))


def initialize_key(path: Path, public: Path):
    if path.exists():
        raise SystemExit("Clé déjà présente ; aucune rotation implicite")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())) + b"\n")
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_bytes(base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)) + b"\n")
    print(json.dumps({"publicKeySha256": hashlib.sha256(public.read_bytes()).hexdigest()}))


def build_package(source: Path, keyfile: Path, output: Path):
    source = source.resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    # build-files.txt is intentional, reviewable, and excludes private state by
    # construction. There is no recursive fallback exporting a working tree.
    allowlist = source / "build-files.txt"
    if not allowlist.exists():
        raise SystemExit("build-files.txt requis")
    paths = [line.strip() for line in allowlist.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if len(set(paths)) != len(paths):
        raise SystemExit("Fichier dupliqué dans build-files.txt")
    contents = {}
    for relative in sorted(paths):
        safe_member(relative)
        file = source / relative
        if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(source):
            raise SystemExit("Fichier absent ou chemin interdit")
        contents[relative] = file.read_bytes()
    manifest["files"] = {p: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                         for p, data in contents.items()}
    validate_manifest(manifest)
    key = signing_key(keyfile)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{manifest['id']}-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for name, data in {"manifest.json": canonical(manifest),
                           "manifest.sig": base64.b64encode(key.sign(canonical(manifest))), **contents}.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            package.writestr(info, data)
    verify_archive(archive, key.public_key())
    metadata = {"id": manifest["id"], "version": manifest["version"],
                "filename": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "size": archive.stat().st_size}
    archive.with_suffix(".metadata.json").write_bytes(canonical(metadata))
    print(json.dumps(metadata, ensure_ascii=False))
    return metadata


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("keygen")
    init.add_argument("--private", type=Path, required=True)
    init.add_argument("--public", type=Path, required=True)
    build = commands.add_parser("build")
    build.add_argument("source", type=Path)
    build.add_argument("--key", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    sign = commands.add_parser("sign-json")
    sign.add_argument("file", type=Path)
    sign.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "keygen":
        initialize_key(args.private, args.public)
    elif args.command == "build":
        build_package(args.source, args.key, args.output)
    else:
        value = json.loads(args.file.read_bytes())
        args.file.write_bytes(canonical(value))
        args.file.with_suffix(".sig").write_bytes(base64.b64encode(signing_key(args.key).sign(canonical(value))))


if __name__ == "__main__":
    main()
