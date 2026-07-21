import openpyxl
from pathlib import Path

from ai_column_mapper import map_columns_with_ai


# Maps our internal field names to every column header spelling we might encounter.
COLUMN_ALIASES = {
    'first_name':      ['first name', 'firstname', 'first', 'given name'],
    'last_name':       ['last name', 'lastname', 'last', 'surname', 'family name'],
    'organization':    ['organization', 'company', 'org', 'account', 'account name',
                        'employer', 'company name'],
    'title':           ['title', 'job title', 'position', 'role'],
    # Work phone takes priority; mobile is a fallback (see phone_mobile below)
    'phone':           ['phone', 'work phone', 'contact work phone', 'telephone', 'tel',
                        'phone number', 'work phone number'],
    'phone_mobile':    ['mobile phone number', 'mobile phone', 'cell phone', 'cell', 'mobile'],
    'email':           ['email', 'email address', 'contact email', 'e-mail'],
    'address1':        ['address 1', 'address1', 'street', 'street address', 'addr1', 'addr 1',
                        'work address 1', 'work address1'],
    'address2':        ['address 2', 'address2', 'addr2', 'addr 2',
                        'work address 2', 'work address2'],
    'city':            ['city', 'work city'],
    'state':           ['state/province', 'state', 'province', 'state / province',
                        'work state/province', 'work state'],
    'zip':             ['zip/postal code', 'zip', 'postal code', 'postcode', 'zip code',
                        'work zip/postal code'],
    'country':         ['country', 'work country'],
    'notes_action':    ['action'],
    'notes_body':      ['notes', 'note', 'application notes'],
    'survey_answers':  ['survey answers', 'survey', 'answers'],
    'qualifiers':      ['qualifiers', 'qualifier'],
    'rank':            ['rank', 'hot/warmcold', 'priority', 'lead rank', 'lead interest'],
    'scan_device':     ['scan device', 'device', 'scanned by', 'booth staff first name'],
}

# Column 9 (index 8) in the known format is an unnamed rank column.
UNNAMED_RANK_COL_INDEX = 8


def _find_header_row(ws):
    """Scan the first 10 rows to find the one that looks like a header."""
    for row_idx in range(1, min(11, ws.max_row + 1)):
        row = [ws.cell(row_idx, col).value for col in range(1, ws.max_column + 1)]
        # A header row has mostly string values
        text_cells = sum(1 for c in row if isinstance(c, str) and c.strip())
        if text_cells >= 3:
            return row_idx, row
    return 1, [ws.cell(1, col).value for col in range(1, ws.max_column + 1)]


def _build_column_map(headers):
    """
    Return a dict mapping field_name → zero-based column index.
    Matches case-insensitively and ignores leading/trailing whitespace.
    """
    normalized = {
        i: (str(h).strip().lower() if h else '')
        for i, h in enumerate(headers)
    }

    col_map = {}
    for field, aliases in COLUMN_ALIASES.items():
        for i, header_text in normalized.items():
            if header_text in aliases:
                col_map[field] = i
                break

    # Special case: unnamed column at index 8 that contains Hot/Warm/Cold
    if 'rank' not in col_map and UNNAMED_RANK_COL_INDEX < len(headers):
        col_map['rank'] = UNNAMED_RANK_COL_INDEX

    # AI fallback: map any fields that alias-matching didn't cover
    ai_additions = map_columns_with_ai(headers, already_mapped=set(col_map.keys()))
    if ai_additions:
        print(f'  AI mapped    : {", ".join(sorted(ai_additions))}')
        col_map.update(ai_additions)

    return col_map


def _get(row, col_map, field):
    """Safely retrieve a value from a row by field name."""
    idx = col_map.get(field)
    if idx is None or idx >= len(row):
        return None
    val = row[idx]
    if val is None:
        return None
    val = str(val).strip()
    return val if val else None


def _build_lead_details(row, col_map):
    """Combine all notes fields into a single Lead Details string."""
    parts = []
    for field in ('notes_action', 'notes_body', 'survey_answers', 'qualifiers'):
        val = _get(row, col_map, field)
        if val:
            parts.append(val)
    return '\n\n'.join(parts) if parts else None


def parse_excel(file_path):
    """
    Read an Excel file and return a list of lead dicts.

    Each dict has these keys (value is None if not found):
      first_name, last_name, organization, title, phone, email,
      address1, address2, city, state, zip, country,
      lead_details, rank
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f'Excel file not found: {file_path}')

    wb = openpyxl.load_workbook(path, data_only=True)

    # If multiple sheets, let the caller choose; default to first
    ws = wb.active

    header_row_idx, headers = _find_header_row(ws)
    col_map = _build_column_map(headers)

    # Report which fields were found
    found = sorted(col_map.keys())
    missing = [f for f in COLUMN_ALIASES if f not in col_map]
    print(f'  Columns mapped : {", ".join(found)}')
    if missing:
        print(f'  Columns missing: {", ".join(missing)} (those fields will be blank)')

    leads = []
    for row_idx in range(header_row_idx + 1, ws.max_row + 1):
        row = [ws.cell(row_idx, col).value for col in range(1, ws.max_column + 1)]

        # Skip completely empty rows
        if all(v is None or str(v).strip() == '' for v in row):
            continue

        # Skip rows with no name and no organization
        first = _get(row, col_map, 'first_name')
        last = _get(row, col_map, 'last_name')
        org = _get(row, col_map, 'organization')
        if not first and not last and not org:
            continue

        # Skip Cold leads — not entered into Oracle
        rank_val = _get(row, col_map, 'rank')
        if rank_val and rank_val.strip().lower() == 'cold':
            continue

        lead = {
            'row_number':   row_idx,
            'first_name':   first,
            'last_name':    last,
            'organization': org,
            'title':        _get(row, col_map, 'title'),
            'phone':        _get(row, col_map, 'phone') or _get(row, col_map, 'phone_mobile'),
            'email':        _get(row, col_map, 'email'),
            'address1':     _get(row, col_map, 'address1'),
            'address2':     _get(row, col_map, 'address2'),
            'city':         _get(row, col_map, 'city'),
            'state':        _get(row, col_map, 'state'),
            'zip':          _get(row, col_map, 'zip'),
            'country':      _get(row, col_map, 'country'),
            'rank':         _get(row, col_map, 'rank'),
            'lead_details': _build_lead_details(row, col_map),
        }
        leads.append(lead)

    return leads


def get_sheet_names(file_path):
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    return wb.sheetnames


if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\kkaess\Downloads\Leads for automation.xlsx'
    leads = parse_excel(path)
    print(f'\nParsed {len(leads)} leads. First 3:\n')
    for lead in leads[:3]:
        print(lead)
        print()
