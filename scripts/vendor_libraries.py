#!/usr/bin/env python3
"""Vendor the exact browser dependencies from their official npm packages.

Archives are verified against npm's integrity field; only named regular files
are copied. No install script is executed. Each delivered asset has a SHA-256,
official package URL, upstream pathname and packaged license in provenance.json.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.parse
import urllib.request


def package(name, version):
    metadata_url = 'https://registry.npmjs.org/' + urllib.parse.quote(name, safe='') + '/' + version
    metadata = json.load(urllib.request.urlopen(metadata_url, timeout=30))
    url = metadata['dist']['tarball']
    raw = urllib.request.urlopen(url, timeout=120).read()
    algorithm, expected = metadata['dist']['integrity'].split('-', 1)
    if algorithm != 'sha512' or base64.b64encode(hashlib.sha512(raw).digest()).decode() != expected:
        raise RuntimeError('Official package integrity mismatch: ' + name)
    return metadata, tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repos', type=Path, required=True)
    args = parser.parse_args()
    receipt = args.repos / 'ReceiptScanner/vendor'
    chart = args.repos / 'StatNerd/vendor'
    records = {receipt: [], chart: []}

    def deliver(name, version, destination, selected):
        metadata, archive = package(name, version)
        members = {entry.name.removeprefix('package/'): entry for entry in archive.getmembers() if entry.isfile()}
        for original, relative in selected(members).items():
            if original == 'LICENSE' and original not in members:
                original = next((path for path in ('LICENSE.md', 'LICENSE.txt', 'LICENSE.MD') if path in members), original)
            external = original.startswith('https://')
            if not external and original not in members:
                raise RuntimeError(f'Required upstream asset is absent: {name}/{original}')
            if external:
                data = urllib.request.urlopen(original, timeout=30).read()
            else:
                source = archive.extractfile(members[original])
                assert source is not None
                data = source.read()
            file = destination / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            records[destination].append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest(),
                                         'size': len(data), 'package': name, 'version': version,
                                         'source_url': original if external else metadata['dist']['tarball'],
                                         'upstream_path': original,
                                         'package_integrity': None if external else metadata['dist']['integrity']})
        archive.close()

    deliver('tesseract.js', '5.1.1', receipt, lambda members: {
        'dist/tesseract.min.js': 'tesseract/tesseract.min.js',
        'dist/worker.min.js': 'tesseract/worker.min.js',
        'dist/tesseract.min.js.LICENSE.txt': 'tesseract/tesseract.min.js.LICENSE.txt',
        'dist/worker.min.js.LICENSE.txt': 'tesseract/worker.min.js.LICENSE.txt',
        'LICENSE': 'LICENSE-tesseract-js.txt'})
    core_files = ('tesseract-core', 'tesseract-core-simd', 'tesseract-core-lstm', 'tesseract-core-simd-lstm')
    deliver('tesseract.js-core', '5.1.1', receipt, lambda members: {
        **{stem + ext: 'tesseract-core/' + stem + ext for stem in core_files for ext in ('.wasm.js', '.wasm')},
        'LICENSE': 'LICENSE-tesseract-core.txt'})
    tessdata_commit = json.load(urllib.request.urlopen('https://api.github.com/repos/naptha/tessdata/commits/gh-pages', timeout=30))['sha']
    tessdata_license = 'https://raw.githubusercontent.com/naptha/tessdata/' + tessdata_commit + '/LICENSE'
    for language in ('eng', 'fra'):
        deliver('@tesseract.js-data/' + language, '1.0.0', receipt, lambda members, language=language: {
            '4.0.0_best_int/' + language + '.traineddata.gz': 'tessdata/' + language + '.traineddata.gz',
            tessdata_license: 'LICENSE-tessdata-' + language + '.txt'})
    deliver('pdfjs-dist', '4.10.38', receipt, lambda members: {
        'legacy/build/pdf.min.mjs': 'pdfjs/pdf.min.mjs',
        'legacy/build/pdf.worker.min.mjs': 'pdfjs/pdf.worker.min.mjs',
        **{path: 'pdfjs/' + path for path in members if path.startswith(('cmaps/', 'standard_fonts/'))},
        'LICENSE': 'LICENSE-pdfjs.txt'})
    deliver('chart.js', '2.9.4', chart, lambda members: {
        'dist/Chart.min.js': 'chart/Chart.min.js', 'LICENSE.md': 'LICENSE-chart-js.txt'})
    for destination, files in records.items():
        (destination / 'provenance.json').write_text(json.dumps({'schema': 1, 'files': files}, indent=2) + '\n')
        print(json.dumps({'addon': destination.parent.name, 'files': len(files),
                          'bytes': sum(file['size'] for file in files), 'integrity_verified': True}))


if __name__ == '__main__':
    main()
