"""
AI-powered column header mapper using Claude.
Maps arbitrary Excel column headers to the internal field names used by oracle_automation.py.
Results are cached to disk so the same header set is never re-analyzed.
"""

import hashlib
import json
from pathlib import Path

import ai_provider
import secrets_store

# Cached results live next to this file
CACHE_FILE = Path(__file__).parent / '.column_cache.json'

# The fields AI should map to (mirrors COLUMN_ALIASES keys in excel_parser.py)
KNOWN_FIELDS = [
    'first_name',
    'last_name',
    'organization',
    'title',
    'phone',
    'phone_mobile',
    'email',
    'address1',
    'address2',
    'city',
    'state',
    'zip',
    'country',
    'notes_action',
    'notes_body',
    'survey_answers',
    'qualifiers',
    'rank',
    'scan_device',
]

FIELD_DESCRIPTIONS = {
    'first_name':   'Contact first/given name',
    'last_name':    'Contact last/family/surname',
    'organization': 'Company, account, employer, or organization name',
    'title':        'Job title, position, or role',
    'phone':        'Primary or work phone number',
    'phone_mobile': 'Mobile, cell, or secondary phone number',
    'email':        'Email address',
    'address1':     'Street address line 1',
    'address2':     'Street address line 2 (suite, unit, etc.)',
    'city':         'City',
    'state':        'State or province',
    'zip':          'ZIP or postal code',
    'country':      'Country',
    'notes_action': 'Action item or follow-up action (short field)',
    'notes_body':   'General notes, comments, or application details',
    'survey_answers': 'Survey responses or answers to questions',
    'qualifiers':   'Lead qualifier answers or qualification data',
    'rank':         'Lead rank, interest level, or priority (Hot/Warm/Cold)',
    'scan_device':  'Device or staff member who scanned the lead',
}


def _fingerprint(headers: list[str]) -> str:
    """Stable hash of a sorted header list — used as cache key."""
    normalized = sorted(h.strip().lower() for h in headers if h)
    return hashlib.sha256(json.dumps(normalized).encode()).hexdigest()[:16]


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding='utf-8')
    except Exception:
        pass


def map_columns_with_ai(headers: list[str], already_mapped: set[str], provider: str = None) -> dict[str, int]:
    """
    Ask AI model (Claude or Gemini) to map unrecognized column headers to known field names.

    Parameters
    ----------
    headers       : full list of column headers from the Excel sheet (zero-based index implied)
    already_mapped: set of field names that alias-matching already resolved
    provider      : 'claude', 'gemini', or None (auto-detected)

    Returns
    -------
    dict mapping field_name → zero-based column index for any NEW mappings found.
    Returns {} if the API key is not set or AI is unavailable.
    """
    prov, api_key = ai_provider.get_ai_provider(provider)
    if not prov or not api_key:
        return {}

    # Which fields still need mapping?
    unmapped_fields = [f for f in KNOWN_FIELDS if f not in already_mapped]
    if not unmapped_fields:
        return {}

    # Check cache first
    cache = _load_cache()
    key = _fingerprint(headers)
    if key in cache:
        cached = cache[key]
        # Return only the fields still needed
        return {f: idx for f, idx in cached.items() if f in unmapped_fields}

    # Build prompt
    field_list = '\n'.join(
        f'  {f}: {FIELD_DESCRIPTIONS[f]}' for f in unmapped_fields
    )
    header_list = '\n'.join(
        f'  {i}: "{h}"' for i, h in enumerate(headers) if h
    )

    prompt = f"""You are mapping Excel column headers to CRM field names for a lead import tool.

Excel column headers (index: "header text"):
{header_list}

CRM fields that still need to be mapped (field_name: description):
{field_list}

Return ONLY a JSON object where each key is a field_name from the list above and each value is the integer column index that best matches it. Only include fields where you are confident there is a match. If no column matches a field, omit that field. Do not include any explanation — only the JSON object.

Example response format:
{{"first_name": 2, "last_name": 3, "organization": 0}}"""

    try:
        raw = ai_provider.call_llm(prompt, provider=prov, max_tokens=512)
        mapping = ai_provider.parse_json_response(raw)
        # Validate: keys must be known fields, values must be valid column indices
        validated = {}
        for field, idx in mapping.items():
            if field in KNOWN_FIELDS and isinstance(idx, int) and 0 <= idx < len(headers):
                validated[field] = idx

        # Cache the full result for this header fingerprint
        cache[key] = validated
        _save_cache(cache)

        # Return only unmapped fields
        return {f: i for f, i in validated.items() if f in unmapped_fields}

    except Exception as e:
        # AI mapping is optional — never crash the main flow
        print(f'  [AI mapper] Skipped: {e}')
        return {}
