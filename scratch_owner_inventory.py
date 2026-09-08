"""
Read-only inventory of one Territory Manager's Oracle leads for a year range.

Reads nothing but GET. Produces the same shape as scratch_wade_inventory.json:
what the owner has, bucketed by status, so a retirement pass can be decided
from full project names and LeadNumbers rather than from a truncated screen.

Usage:
    python scratch_owner_inventory.py "Jeff Frederick" --years 2025 2026
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import oracle_sync  # noqa: E402

FIELDS = 'LeadId,LeadNumber,Name,StatusCode,CreationDate,OwnerPartyName,PrimaryContactPartyName'
PAGE = 200


def fetch_owner_leads(client, owner):
    """Every lead whose OwnerPartyName is this person, oldest page first."""
    out, offset = [], 0
    while True:
        page = client._get_json(
            client.leads_url,
            {
                'q': f"OwnerPartyName='{owner}'",
                'fields': FIELDS, 'onlyData': 'true',
                'limit': str(PAGE), 'offset': str(offset),
            },
        )
        items = page.get('items') or []
        out.extend(items)
        if not page.get('hasMore'):
            return out
        offset += int(page.get('limit') or len(items) or PAGE)


def row(item):
    return {
        'id': item.get('LeadId'),
        'number': item.get('LeadNumber'),
        'name': item.get('Name'),
        'status': item.get('StatusCode'),
        'created_at': item.get('CreationDate'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('owner')
    ap.add_argument('--years', nargs='+', default=['2025', '2026'])
    ap.add_argument('--out', default='scratch_owner_inventory.json')
    args = ap.parse_args()

    client = oracle_sync.build_client()
    everything = fetch_owner_leads(client, args.owner)

    in_range = [
        i for i in everything
        if str(i.get('CreationDate') or '')[:4] in set(args.years)
    ]

    counts = {}
    for item in in_range:
        status = item.get('StatusCode') or '(none)'
        counts[status] = counts.get(status, 0) + 1

    # QUALIFIED/UNQUALIFIED are the retirement candidates; CONVERTED is a real
    # outcome and RETIRED is already done. Same buckets the Wade pass used.
    candidates = [
        row(i) for i in in_range
        if (i.get('StatusCode') or '').upper() in ('QUALIFIED', 'UNQUALIFIED')
    ]

    result = {
        'owner': args.owner,
        'years': args.years,
        f'total_{args.owner.split()[0].lower()}_leads_all_time': len(everything),
        'total_leads_in_range': len(in_range),
        'status_counts_in_range': counts,
        'qualified_or_unqualified_to_retire': sorted(
            candidates, key=lambda r: r['created_at'] or ''),
        'already_retired': [
            row(i) for i in in_range
            if (i.get('StatusCode') or '').upper() == 'RETIRED'],
        'converted_leads': [
            row(i) for i in in_range
            if (i.get('StatusCode') or '').upper() == 'CONVERTED'],
    }

    Path(args.out).write_text(
        json.dumps(result, indent=2, default=str), encoding='utf-8')

    print(f"owner              : {args.owner}")
    print(f"all time           : {len(everything)}")
    print(f"in {'/'.join(args.years):<15}: {len(in_range)}")
    print('status counts      :')
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f'    {status:<14} {n}')
    print(f'\ncandidates to retire ({len(candidates)}):')
    for r in result['qualified_or_unqualified_to_retire']:
        print(f"    {r['number']:<14} {r['status']:<12} {str(r['created_at'])[:10]}  {r['name']}")
    print(f'\nwrote {args.out}')


if __name__ == '__main__':
    main()
