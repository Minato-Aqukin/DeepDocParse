#!/usr/bin/env python3
"""Apply an operator-verified ownership manifest to quarantined legacy assets.

Input: [{"resource_id": "...", "owner_id": "...", "organization_id": "...",
         "filename": "optional recovered original filename"}]

The manifest must be reconciled against control-plane membership/upload records.
This offline migration uses the corpus migration connection and never reads control
SQL. Default is a dry run; --apply changes only matching quarantined rows atomically.
"""
import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from ddp_corpus.db import get_sessionmaker
from ddp_corpus.models import Resource, ResourceVersion


async def resolve(rows: list[dict], *, apply: bool) -> int:
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError('manifest must contain 1–10000 mappings')
    seen = set()
    async with get_sessionmaker()() as session:
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'resource_id','owner_id','organization_id','filename'}:
                raise ValueError('invalid mapping fields')
            for key in ('resource_id','owner_id','organization_id'):
                value = row.get(key)
                if not isinstance(value, str) or not 1 <= len(value) <= 32 or value.startswith('migration:'):
                    raise ValueError(f'invalid {key}')
            if row['resource_id'] in seen:
                raise ValueError('duplicate resource_id')
            seen.add(row['resource_id'])
            filename = row.get('filename')
            if filename is not None and (not isinstance(filename, str) or not 1 <= len(filename) <= 255
                    or any(c in filename for c in ('\0','\r','\n','/','\\'))):
                raise ValueError('invalid recovered filename')
            resource = await session.scalar(select(Resource).where(Resource.id == row['resource_id'])
                                             .with_for_update())
            if resource is None or resource.owner_id != row['owner_id']:
                raise ValueError('resource and historical owner do not match')
            if resource.organization_id != 'migration:unresolved':
                if resource.organization_id == row['organization_id']:
                    continue  # repeated manifest after successful commit
                raise ValueError('resource is not a quarantined migration row')
            if apply:
                resource.organization_id = row['organization_id']
                if filename:
                    resource.display_name = filename
                    for version in await session.scalars(select(ResourceVersion).where(
                            ResourceVersion.resource_id == resource.id)):
                        version.filename = filename
        if apply:
            await session.commit()
        else:
            await session.rollback()
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.manifest.stat().st_size > 5 * 1024 * 1024:
        parser.error('manifest exceeds 5 MiB')
    count = asyncio.run(resolve(json.loads(args.manifest.read_text()), apply=args.apply))
    print(f'{"Applied" if args.apply else "Validated (dry run)"}: {count} mappings')


if __name__ == '__main__':
    main()
