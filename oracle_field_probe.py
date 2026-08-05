"""
Read-only probe for the Oracle attribute behind the lead's Website box.

Which attribute that box maps to is tenant configuration -- it may be a stock
field or one of this instance's custom *_c fields -- and guessing is not an
option: an attribute Oracle doesn't recognize comes back as a 400 that rejects
the whole lead, not just the field. So this asks the tenant.

Three read-only probes, cheapest first:

  1. --sample  Pull recent leads and report every attribute whose NAME looks
               website-ish, plus every attribute whose VALUE looks like a URL.
               The second half is the one that finds oddly-named custom fields.
  2. --lead N  Same, for one known lead number. Use a lead whose Website box is
               filled in and this will name the field outright.
  3. --describe  Oracle's own metadata for the leads resource. Authoritative
               but verbose, and some tenants disable it.

Nothing here writes. It issues GETs with the same credentials pipeline_submit
uses, prints what it found, and suggests the config.json line to add.

Usage:  py oracle_field_probe.py                 (--sample, the default)
        py oracle_field_probe.py --lead 300000123456789
        py oracle_field_probe.py --describe
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

import secrets_store
from oracle_api import API_VERSION

# Attribute names worth a look. Deliberately loose -- this is a report for a
# human to read, not an automatic decision.
_NAME_HINT_RE = re.compile(r'url|web|site|homepage|domain', re.IGNORECASE)

# Values that look like a website. Excludes Oracle's own REST hrefs, which
# appear in every response's "links" and would bury everything else.
_URL_VALUE_RE = re.compile(r'^\s*(?:https?://|www\.)[^\s]+$', re.IGNORECASE)


def _load_config():
    with open(Path(__file__).parent / 'config.json') as f:
        return json.load(f)['oracle']


def _credentials():
    load_dotenv()
    username = secrets_store.get_secret('ORACLE_USERNAME')
    password = secrets_store.get_secret('ORACLE_PASSWORD')
    if not username or not password:
        sys.exit(
            'ORACLE_USERNAME/ORACLE_PASSWORD not found. Store them first:\n'
            '  py secrets_store.py ORACLE_USERNAME <user>\n'
            '  py secrets_store.py ORACLE_PASSWORD <password>'
        )
    return username, password


def _get(session, url, auth, timeout, **params):
    response = session.get(
        url, params=params or None, auth=auth,
        headers={'Accept': 'application/json'}, timeout=timeout,
    )
    if response.status_code >= 400:
        sys.exit(f'Oracle returned {response.status_code} for {url}\n{response.text[:1000]}')
    return response.json()


def _walk(node, path=''):
    """Yields (dotted_path, key, value) for every scalar in a JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f'{path}.{key}' if path else key
            if isinstance(value, (dict, list)):
                yield from _walk(value, child)
            else:
                yield child, key, value
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f'{path}[{i}]')


_FLAG_KEYS = ('type', 'updatable', 'creatable', 'createable', 'mandatory',
              'maxlength', 'title', 'precision', 'hasdefault')


def _find_attribute_metadata(node, matches):
    """
    Collects (name, flags) for attribute definitions whose name satisfies
    `matches`.

    Oracle describes an attribute two different ways depending on where it
    appears: keyed by name ({"WebsiteBSE_c": {...}}) or as a list entry
    carrying its own "name" ({"name": "WebsiteBSE_c", ...}). Only the first
    shape was handled at first, which is why a --describe run reported nothing
    but child-resource noise. Both are checked now, and identical entries are
    collapsed -- the same definition is repeated once per child resource.
    """
    found = {}

    def walk(node, key=None):
        if isinstance(node, dict):
            own_name = node.get('name') or node.get('Name')
            name = own_name if isinstance(own_name, str) else key
            if isinstance(name, str) and matches(name):
                flags = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
                if flags:
                    found.setdefault(
                        (name, tuple(sorted((k, str(v)) for k, v in flags.items()))), flags
                    )
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)

    walk(node)
    return [(name, flags) for (name, _), flags in found.items()]


def _report_attribute_metadata(node, field=None):
    """Prints an attribute's flags -- how you tell one that merely exists from
    one you can actually write on create."""
    if field:
        matches = lambda name: name.lower() == field.lower()
        label = f'Metadata for {field}'
    else:
        matches = lambda name: bool(_NAME_HINT_RE.search(name))
        label = 'Metadata for website-ish attributes'

    found = _find_attribute_metadata(node, matches)
    print(f'\n{label} (look for updatable/creatable):')
    if not found:
        print('  (not listed in this describe response)')
        if field:
            print(f'  Custom *_c fields are often absent here even when writable.')
        return
    for name, flags in sorted(found):
        interesting = {k: v for k, v in flags.items() if k.lower() in _FLAG_KEYS} or flags
        print(f'  {name}')
        for k, v in sorted(interesting.items()):
            print(f'      {k:<14} {v}')


def _report(records, label):
    """Prints name-matched and value-matched attributes across records."""
    by_name, by_value = {}, {}
    for record in records:
        for path, key, value in _walk(record):
            if path.startswith('links') or key in ('rel', 'href', 'kind'):
                continue
            if _NAME_HINT_RE.search(key):
                by_name.setdefault(key, set()).add(repr(value)[:60])
            if isinstance(value, str) and _URL_VALUE_RE.match(value):
                by_value.setdefault(key, set()).add(value[:60])

    print(f'\n--- {label} ---')
    print(f'\nAttributes whose NAME mentions url/web/site ({len(by_name)}):')
    for key in sorted(by_name):
        print(f'  {key:<45} e.g. {", ".join(sorted(by_name[key])[:3])}')
    if not by_name:
        print('  (none)')

    print(f'\nAttributes whose VALUE looks like a URL ({len(by_value)}):')
    for key in sorted(by_value):
        print(f'  {key:<45} e.g. {", ".join(sorted(by_value[key])[:3])}')
    if not by_value:
        print('  (none -- try --lead on a lead whose Website box is filled in)')

    candidates = sorted(set(by_name) | set(by_value))
    if candidates:
        print('\nIf one of these is the Website box, put it in config.json:')
        print(f'  "oracle": {{ ..., "website_field": "{candidates[0]}" }}')
        if len(candidates) > 1:
            print(f'  (other candidates: {", ".join(candidates[1:])})')
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lead', help='Probe one lead by LeadNumber')
    parser.add_argument('--describe', action='store_true',
                        help="Fetch Oracle's metadata for the leads resource")
    parser.add_argument('--field',
                        help='With --describe, report just this attribute '
                             '(e.g. --field WebsiteBSE_c)')
    parser.add_argument('--limit', type=int, default=5,
                        help='How many recent leads to sample (default 5)')
    args = parser.parse_args()

    config = _load_config()
    base = config['base_url'].rstrip('/')
    leads_url = f'{base}/crmRestApi/resources/{API_VERSION}/leads'
    auth = _credentials()
    timeout = int(config.get('api_timeout_seconds', 60))
    session = requests.Session()

    if args.describe:
        data = _get(session, f'{leads_url}/describe', auth, timeout)
        if not args.field:
            _report([data], 'leads/describe metadata')
        _report_attribute_metadata(data, args.field)
    elif args.lead:
        data = _get(session, leads_url, auth, timeout,
                    q=f"LeadNumber='{args.lead}'", onlyData='true')
        items = data.get('items') or []
        if not items:
            sys.exit(f'No lead found with LeadNumber {args.lead}')
        _report(items, f'lead {args.lead}')
    else:
        data = _get(session, leads_url, auth, timeout,
                    limit=args.limit, orderBy='CreationDate:desc', onlyData='true')
        items = data.get('items') or []
        print(f'Sampled {len(items)} recent lead(s).')
        _report(items, f'{len(items)} most recent leads')


if __name__ == '__main__':
    main()
