"""
Read-only Oracle report: lead status mix and conversion rates per Territory
Manager, per year, written to an .xlsx workbook.

GET only. Nothing here writes to Oracle; see scratch_retire_leads.py for the
write path, which is kept deliberately separate.

Usage:
    python scratch_tm_report.py
    python scratch_tm_report.py --owners "Wade Kahout" "Serdar Eren" --years 2025 2026
"""

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import oracle_sync  # noqa: E402

FIELDS = 'LeadId,LeadNumber,Name,StatusCode,CreationDate,OwnerPartyName'
PAGE = 200

# The four the report breaks out by name. Anything else Oracle returns is
# counted under "Other" rather than silently dropped, so the columns always
# reconcile to Total.
TRACKED = ('QUALIFIED', 'UNQUALIFIED', 'RETIRED', 'CONVERTED')

HEAD_FILL = PatternFill('solid', fgColor='28495A')
HEAD_FONT = Font(bold=True, color='FFFFFF')
TOTAL_FONT = Font(bold=True)


def fetch_owner_leads(client, owner):
    """Every lead whose OwnerPartyName is this person."""
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


def status_of(item):
    return (item.get('StatusCode') or '(none)').upper()


def year_of(item):
    return str(item.get('CreationDate') or '')[:4]


def tally(items):
    """Counts for one owner/year slice, plus the derived rates."""
    counts = Counter(status_of(i) for i in items)
    total = len(items)
    row = {name.title(): counts.get(name, 0) for name in TRACKED}
    row['Other'] = total - sum(row.values())
    row['Total'] = total

    def pct(n):
        return round(n / total * 100, 1) if total else 0.0

    row['Converted %'] = pct(row['Converted'])
    row['Qualified %'] = pct(row['Qualified'])
    row['Unqualified %'] = pct(row['Unqualified'])
    row['Retired %'] = pct(row['Retired'])
    return row


COLUMNS = [
    'Territory Manager', 'Year', 'Total',
    'Qualified', 'Unqualified', 'Retired', 'Converted', 'Other',
    'Converted %', 'Qualified %', 'Unqualified %', 'Retired %',
]


def autosize(ws, widths):
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width


def write_summary(ws, rows, years):
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(horizontal='center', vertical='center')

    for row in rows:
        ws.append([row.get(c, '') for c in COLUMNS])
        if row['Year'] in ('All years', f"{years[0]}-{years[-1]}"):
            for cell in ws[ws.max_row]:
                cell.font = TOTAL_FONT

    ws.freeze_panes = 'A2'
    autosize(ws, [22, 12, 9, 11, 13, 10, 11, 8, 13, 13, 15, 11])

    note = ws.max_row + 2
    ws.cell(note, 1, 'Percentages are each status as a share of that '
                     'manager/year Total, using the lead\'s current Oracle '
                     'StatusCode. They sum to 100%.').font = Font(italic=True)
    ws.cell(note + 1, 1, f'Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC '
                         '- read-only GET from Oracle Sales Cloud.').font = Font(italic=True)


def write_detail(ws, items):
    ws.append(['Lead Number', 'Project Name', 'Status', 'Created', 'Year'])
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
    for item in sorted(items, key=lambda i: str(i.get('CreationDate') or '')):
        ws.append([
            item.get('LeadNumber'), item.get('Name'), status_of(item),
            str(item.get('CreationDate') or '')[:10], year_of(item),
        ])
    ws.freeze_panes = 'A2'
    autosize(ws, [16, 62, 14, 12, 8])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--owners', nargs='+',
                    default=['Wade Kahout', 'Serdar Eren', 'Jeff Frederick'])
    ap.add_argument('--years', nargs='+', default=['2025', '2026'])
    ap.add_argument('--out', default='scratch_tm_lead_report.xlsx')
    args = ap.parse_args()

    client = oracle_sync.build_client()
    wb = Workbook()
    summary_rows = []
    per_owner = {}

    for owner in args.owners:
        everything = fetch_owner_leads(client, owner)
        in_range = [i for i in everything if year_of(i) in set(args.years)]
        per_owner[owner] = in_range
        print(f'{owner:<18} {len(everything):>5} all time   {len(in_range):>4} in '
              f'{"/".join(args.years)}')

        for year in args.years:
            row = tally([i for i in in_range if year_of(i) == year])
            summary_rows.append({'Territory Manager': owner, 'Year': year, **row})
        combined = tally(in_range)
        summary_rows.append(
            {'Territory Manager': owner, 'Year': 'All years', **combined})
        print(f'    {"":16} total={combined["Total"]:<5} '
              f'qualified={combined["Qualified"]:<4} '
              f'unqualified={combined["Unqualified"]:<4} '
              f'retired={combined["Retired"]:<4} '
              f'converted={combined["Converted"]:<4} '
              f'({combined["Converted %"]}% converted)')

    ws = wb.active
    ws.title = 'Summary'
    write_summary(ws, summary_rows, args.years)

    for owner, items in per_owner.items():
        write_detail(wb.create_sheet(owner[:31]), items)

    wb.save(args.out)
    print(f'\nwrote {args.out}')


if __name__ == '__main__':
    main()
