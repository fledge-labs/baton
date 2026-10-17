#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Restore the readable, hash-checked lab handoff archive into an empty directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    index = json.loads((here / 'lab-source-index.json').read_text())
    archive = here / index['archive']
    if hashlib.sha256(archive.read_bytes()).hexdigest() != index['archive_sha256']:
        raise ValueError('Archive checksum mismatch')
    records = []
    seen = set()
    with archive.open('rb') as source:
        for expected in index['files']:
            prefix = b'=== BATON_SOURCE_FILE === '
            header = source.readline()
            if not header.startswith(prefix):
                raise ValueError('Invalid archive header')
            metadata = json.loads(header[len(prefix):])
            name = PurePosixPath(metadata['path'])
            if name.is_absolute() or '..' in name.parts or str(name) in seen:
                raise ValueError('Unsafe or duplicate archive path')
            if metadata['path'] != expected['path'] or metadata['mode'] != expected['mode']:
                raise ValueError('Archive index mismatch')
            size = metadata['size']
            if type(size) is not int or not 0 <= size <= 10_000_000:
                raise ValueError('Invalid archive size')
            data = source.read(size)
            if len(data) != size or source.read(1) != b'\n':
                raise ValueError('Truncated archive')
            if hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise ValueError('File checksum mismatch')
            if metadata['mode'] not in ('100644', '100755'):
                raise ValueError('Unsupported archive mode')
            records.append((name, data, int(metadata['mode'][-3:], 8)))
            seen.add(str(name))
        if source.read() != b'=== BATON_SOURCE_END ===\n':
            raise ValueError('Unexpected archive trailer')
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Destination must be empty')
    for name, data, mode in records:
        target = destination.joinpath(*name.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, mode)
    print(f'Restored {len(records)} verified files')


if __name__ == '__main__':
    main()
