"""
Set named Oracle leads to RETIRED, by LeadId, one at a time.

Deliberately takes explicit ids rather than a filter: the filter belongs in
scratch_owner_inventory.py, whose output a human reads before anything is
written. A script that both chooses and writes is one typo away from retiring
somebody's whole book.

Dry-run unless --apply is passed. Every record's prior StatusCode is read and
written to an undo file first, so any change here can be put back.

Usage:
    python scratch_retire_leads.py 300000151234567 --dry-run
    python scratch_retire_leads.py 300000151234567 --apply
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import oracle_sync  # noqa: E402

TARGET_STATUS = 'RETIRED'
READ_FIELDS = 'LeadId,LeadNumber,Name,StatusCode,OwnerPartyName,CreationDate'


def read_lead(client, lead_id):
    return client._get_json(
        f'{client.leads_url}/{lead_id}',
        {'fields': READ_FIELDS, 'onlyData': 'true'},
    )


def patch_status(client, lead_id, status):
    """PATCH one lead's StatusCode. Raises on any non-2xx."""
    response = client.session.patch(
        f'{client.leads_url}/{lead_id}',
        json={'StatusCode': status},
        auth=(client.username, client.password),
        headers={
            'Content-Type': 'application/vnd.oracle.adf.resourceitem+json',
            'Accept': 'application/json',
        },
        timeout=client.timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f'PATCH {lead_id} failed ({response.status_code}): {response.text[:400]}'
        )
    return response.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('lead_ids', nargs='+', type=int)
    ap.add_argument('--apply', action='store_true',
                    help='actually write; without it nothing is changed')
    ap.add_argument('--undo-file', default='scratch_retire_undo.json')
    args = ap.parse_args()

    client = oracle_sync.build_client()

    before = []
    print('--- current state ---')
    for lead_id in args.lead_ids:
        lead = read_lead(client, lead_id)
        before.append({
            'id': lead.get('LeadId'), 'number': lead.get('LeadNumber'),
            'name': lead.get('Name'), 'status_before': lead.get('StatusCode'),
            'owner': lead.get('OwnerPartyName'),
            'created_at': lead.get('CreationDate'),
        })
        print(f"  {lead.get('LeadNumber'):<14} {str(lead.get('StatusCode')):<12} "
              f"{lead.get('OwnerPartyName')}  |  {lead.get('Name')}")

    already = [b for b in before if (b['status_before'] or '').upper() == TARGET_STATUS]
    todo = [b for b in before if (b['status_before'] or '').upper() != TARGET_STATUS]
    for b in already:
        print(f"  skip {b['number']}: already {TARGET_STATUS}")

    if not args.apply:
        print(f'\nDRY RUN. Would set {len(todo)} lead(s) to {TARGET_STATUS}. '
              'Re-run with --apply to write.')
        return 0

    undo_path = Path(args.undo_file)
    undo_path.write_text(json.dumps({
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'target_status': TARGET_STATUS,
        'records': before,
    }, indent=2, default=str), encoding='utf-8')
    print(f'\nundo state written to {undo_path}')

    print(f'--- applying {TARGET_STATUS} ---')
    changed = 0
    for record in todo:
        try:
            result = patch_status(client, record['id'], TARGET_STATUS)
            print(f"  {record['number']}: {record['status_before']} -> "
                  f"{result.get('StatusCode')}")
            changed += 1
        except (RuntimeError, requests.RequestException) as exc:
            print(f"  {record['number']}: FAILED -- {exc}")
    print(f'\n{changed} of {len(todo)} updated.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
