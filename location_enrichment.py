"""Conservative location fallbacks for territory assignment.

The LLM remains the broad contact extractor.  This module covers the much
narrower failure mode where that call is unavailable or overlooks an address:
first read a recognizable US/Canadian signature, then (for genuine/unknown
leads only) inspect the company's own public website.
"""

import html
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

import territory_map


_US_NAMES = {name.lower(): abbr for abbr, name in territory_map.US_STATE_ABBR.items()}
_US_TOKENS = sorted(
    list(territory_map.US_STATE_ABBR) + list(territory_map.US_STATE_ABBR.values()),
    key=len, reverse=True,
)
_CA_ABBR = {
    'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT',
}
_CA_NAMES = {
    'alberta': 'AB', 'british columbia': 'BC', 'manitoba': 'MB',
    'new brunswick': 'NB', 'newfoundland and labrador': 'NL',
    'nova scotia': 'NS', 'northwest territories': 'NT', 'nunavut': 'NU',
    'ontario': 'ON', 'prince edward island': 'PE', 'quebec': 'QC',
    'saskatchewan': 'SK', 'yukon': 'YT',
}
_REGION_PATTERN = '|'.join(re.escape(v) for v in _US_TOKENS + list(_CA_NAMES))
_US_LINE_RE = re.compile(
    rf'^\s*([A-Za-z][A-Za-z .\'\-]{{1,80}}?),\s*({_REGION_PATTERN})\s+' 
    r'(\d{5}(?:-\d{4})?)\s*(?:USA|US|United States(?: of America)?)?\s*$', re.I,
)
_CA_LINE_RE = re.compile(
    rf'^\s*([A-Za-z][A-Za-z .\'\-]{{1,80}}?),?\s+({_REGION_PATTERN}|[A-Z]{{2}})\s+'
    r'([A-Z]\d[A-Z][ -]?\d[A-Z]\d)\s*(?:Canada)?\s*$', re.I,
)
_STREET_RE = re.compile(r'^\s*\d{1,8}\s+\S+')


def _normalized_lines(text):
    text = html.unescape(re.sub(r'<[^>]+>', '\n', text or ''))
    return [re.sub(r'\s+', ' ', line).strip(' \t|') for line in text.splitlines()]


def extract_stated_location(text):
    """Return location fields only when a line is recognizably an address."""
    lines = _normalized_lines(text)
    for index, line in enumerate(lines):
        match = _US_LINE_RE.match(line)
        if match:
            city, region, postal = match.groups()
            region_key = region.lower().strip()
            state = (region.upper() if region.upper() in territory_map.US_STATE_ABBR
                     else _US_NAMES.get(region_key, region))
            result = {'city': city.strip(), 'state': state, 'zip': postal, 'country': 'USA'}
            previous = next((v for v in reversed(lines[:index]) if v), '')
            if _STREET_RE.match(previous):
                result['address1'] = previous
            return result

        match = _CA_LINE_RE.match(line)
        if match:
            city, region, postal = match.groups()
            upper = region.upper()
            state = upper if upper in _CA_ABBR else _CA_NAMES.get(region.lower(), region)
            # A two-letter US state followed by a Canadian-form postal code is
            # still Canada; the postal format is the stronger signal.
            result = {
                'city': city.strip(), 'state': state,
                'zip': postal.upper().replace(' ', ''), 'country': 'Canada',
            }
            previous = next((v for v in reversed(lines[:index]) if v), '')
            if _STREET_RE.match(previous):
                result['address1'] = previous
            return result
    return {}


# Tags whose contents are markup for the browser, not words on the page. A
# modern corporate page is mostly inline script: treating that as text buried
# the Wikipedia infobox this module is trying to read under four thousand
# characters of feature flags.
_NON_TEXT_TAGS = {'script', 'style', 'noscript', 'template', 'svg'}

# Tags that end a line of text. Table cells are in the list because an infobox
# ("Headquarters" | "Dublin, Ireland") is a table, and without a break between
# the cells its label and value run together into one unreadable word.
_BLOCK_TAGS = ('p', 'div', 'li', 'br', 'address', 'tr', 'td', 'th',
               'h1', 'h2', 'h3')


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.links = []
        self.json_ld = []
        self._in_json = False
        self._muted = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _BLOCK_TAGS:
            self.text.append('\n')
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'script' and 'ld+json' in attrs.get('type', '').lower():
            self._in_json = True
        elif tag in _NON_TEXT_TAGS:
            self._muted.append(tag)

    def handle_endtag(self, tag):
        if tag == 'script':
            self._in_json = False
        if tag in _NON_TEXT_TAGS and tag in self._muted:
            # Unbalanced markup is the norm, so unwind to the tag that closed
            # rather than assuming it is the innermost one.
            del self._muted[self._muted.index(tag):]
        if tag in _BLOCK_TAGS:
            self.text.append('\n')

    def handle_data(self, data):
        if self._in_json:
            self.json_ld.append(data)
        elif not self._muted:
            self.text.append(data)


def _postal_addresses(value):
    if isinstance(value, dict):
        if str(value.get('@type', '')).lower() == 'postaladdress':
            yield value
        for child in value.values():
            yield from _postal_addresses(child)
    elif isinstance(value, list):
        for child in value:
            yield from _postal_addresses(child)


def _location_from_page(page):
    parser = _PageParser()
    parser.feed(page or '')
    for block in parser.json_ld:
        try:
            data = json.loads(block)
        except (TypeError, ValueError):
            continue
        for address in _postal_addresses(data):
            region = str(address.get('addressRegion') or '').strip()
            postal = str(address.get('postalCode') or '').strip()
            city = str(address.get('addressLocality') or '').strip()
            country = str(address.get('addressCountry') or '').strip()
            joined = f'{city}, {region} {postal} {country}'.strip()
            found = extract_stated_location(joined)
            if found:
                street = str(address.get('streetAddress') or '').strip()
                if street:
                    found['address1'] = street
                return found, parser.links
    return extract_stated_location(''.join(parser.text)), parser.links


def page_text_and_links(page):
    """The visible text and hyperlinks of an HTML page.

    For callers that need to *read* a page rather than pattern-match it --
    hq_lookup.py returns this to the model as the result of its fetch tool.
    """
    parser = _PageParser()
    parser.feed(page or '')
    lines = (re.sub(r'[ \t\xa0]+', ' ', line).strip()
             for line in html.unescape(''.join(parser.text)).splitlines())
    return '\n'.join(line for line in lines if line), parser.links


def _host_is_public(host):
    """Do not turn customer-controlled URLs into an internal-network fetch."""
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
        return bool(addresses) and all(ipaddress.ip_address(ip).is_global for ip in addresses)
    except (OSError, ValueError):
        return False


MAX_RESEARCH_BYTES = 1_500_000
RESEARCH_BUDGET_SECONDS = 12


def get_public_page(url, timeout=8, deadline=None):
    """Fetch one public HTML page, bounded in redirects, size and time.

    Public because hq_lookup.py hands this same fetch to a model as its only
    tool -- every guard below (public hosts, capped bytes, shared deadline)
    has to apply to a URL the model chose exactly as it does to one this
    module followed itself.
    """
    deadline = deadline or (time.monotonic() + timeout)
    for _ in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or not _host_is_public(parsed.hostname):
            return None, None
        response = requests.get(
            url, timeout=(min(3, remaining), min(5, remaining)),
            allow_redirects=False, stream=True,
            headers={'User-Agent': 'BalSealLeadPipeline/1.0'},
        )
        if response.status_code in (301, 302, 303, 307, 308):
            target = response.headers.get('Location')
            response.close()
            if not target:
                return None, None
            url = urljoin(url, target)
            continue
        content_type = response.headers.get('Content-Type', '')
        if not response.ok or ('html' not in content_type and content_type):
            response.close()
            return None, None
        try:
            declared = int(response.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > MAX_RESEARCH_BYTES:
            response.close()
            return None, None
        chunks = []
        size = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_RESEARCH_BYTES:
                    return None, None
                chunks.append(chunk)
        finally:
            response.close()
        encoding = response.encoding or 'utf-8'
        return b''.join(chunks).decode(encoding, errors='replace'), url
    return None, None


def research_company_location(website):
    """Inspect the company site and up to two likely contact/location pages."""
    deadline = time.monotonic() + RESEARCH_BUDGET_SECONDS
    page, final_url = get_public_page(website, deadline=deadline)
    if not page:
        return {}
    found, links = _location_from_page(page)
    if found:
        return found

    origin = urlparse(final_url)
    candidates = []
    for href in links:
        absolute = urljoin(final_url, href)
        parsed = urlparse(absolute)
        if parsed.hostname != origin.hostname:
            continue
        marker = (parsed.path + '?' + parsed.query).lower()
        if any(word in marker for word in ('contact', 'location', 'about')) and absolute not in candidates:
            candidates.append(absolute)
    for candidate in candidates[:2]:
        child, _ = get_public_page(candidate, deadline=deadline)
        if child:
            found, _ = _location_from_page(child)
            if found:
                return found
    return {}


def enrich_location(lead, body=None, research=False):
    """Fill only blank lead fields; explicit/extracted values always win."""
    if not (lead.get('state') or '').strip():
        stated = extract_stated_location(body)
        if stated:
            for key, value in stated.items():
                if not (lead.get(key) or '').strip():
                    lead[key] = value
            lead['_location_source'] = 'email signature'

    if research and not (lead.get('state') or '').strip() and lead.get('website'):
        try:
            found = research_company_location(lead['website'])
        except requests.RequestException:
            found = {}
        if found:
            for key, value in found.items():
                if not (lead.get(key) or '').strip():
                    lead[key] = value
            lead['_location_source'] = 'company website'
    return lead
