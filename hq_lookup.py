"""Headquarters lookup for leads whose email never says where they are.

territory_map routes on country/state/ZIP, so a lead with no address at all
reaches the dashboard as "Unassigned -- no state on file", however obvious the
company is. Bal Seal has heard of Eaton; the pipeline has not, because nobody
wrote an address in the email.

lead_extractor reads what the email states and location_enrichment.py reads a
signature and pattern-matches the company's own site. This module is the step
after both fail: a model is given exactly one tool -- an HTTP GET of a public
page -- and navigates the company's site itself, the way a person would, until
it finds the head office. It is the most expensive path here (an AI call plus
several page fetches) and deliberately the last one tried.

Two rules keep an invented answer out of the CRM:

  * The address must cite a page the tool actually fetched. A model that names
    a city without having read it anywhere is discarded, not trusted.
  * The address must route. territory_map.assign_tm() has to return a manager
    for it, or the lead stays unassigned with its original blank fields -- a
    human reviewer looking at "Unassigned" is a better outcome than a lead
    quietly filed under the wrong rep.

Whatever is written lands on the lead with its source URL in the assignment
basis, so the reviewer can open the page the address came from.
"""

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import ai_provider
import company_website
import location_enrichment
import territory_map

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / 'config.json'

# Defaults apply when config.json has no "hq_lookup" block at all, so the
# feature works on an existing deployment without a config edit.
DEFAULT_SETTINGS = {
    'enabled': True,
    'max_fetches': 6,
    'budget_seconds': 60,
    'min_confidence': 0.6,
}

# How much of a page the model is shown. A corporate homepage flattens to
# well over this; the head office address, when a page carries one, is
# essentially always in the first screenful or the footer, and the footer
# survives because the cap is applied to the head and tail of the text.
PAGE_CHAR_LIMIT = 7000
MAX_LINKS = 40

# Links worth putting in front of the model first when a page has hundreds.
_NAVIGATION_WORDS = (
    'contact', 'about', 'location', 'headquarter', 'head-office', 'corporate',
    'offices', 'impressum', 'who-we-are', 'company',
)

_US_ABBR_BY_NAME = {
    name.lower(): abbr for abbr, name in territory_map.US_STATE_ABBR.items()
}
_US_COUNTRY_WORDS = {
    'usa', 'us', 'u.s.', 'u.s.a.', 'united states', 'united states of america',
    'america', 'united states (usa)',
}

# The answer's shape, enforced by the API rather than asked for in the prompt.
# Without it a model part-way through browsing will happily reply "I will now
# fetch the contact page to confirm", which is neither an answer nor JSON.
ANSWER_SCHEMA = {
    'type': 'json_schema',
    'name': 'head_office',
    'strict': True,
    'schema': {
        'type': 'object',
        'properties': {
            'found': {'type': 'boolean'},
            'address1': {'type': 'string'},
            'city': {'type': 'string'},
            'state': {'type': 'string'},
            'zip': {'type': 'string'},
            'country': {'type': 'string'},
            'source_url': {'type': 'string'},
            'confidence': {'type': 'number'},
            'note': {'type': 'string'},
        },
        # strict mode requires every property listed; a blank string is how the
        # model says "not applicable" (a non-US address has no state).
        'required': ['found', 'address1', 'city', 'state', 'zip', 'country',
                     'source_url', 'confidence', 'note'],
        'additionalProperties': False,
    },
}

FETCH_TOOL = [{
    'type': 'function',
    'name': 'fetch_page',
    'description': (
        'HTTP GET a public web page and return its visible text and the links '
        'it contains. Use it to read a company site and follow its navigation.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'url': {
                'type': 'string',
                'description': 'Absolute http(s) URL of the page to read.',
            },
        },
        'required': ['url'],
        'additionalProperties': False,
    },
}]


def settings():
    """The hq_lookup block of config.json, over the defaults above."""
    values = dict(DEFAULT_SETTINGS)
    try:
        with open(CONFIG_PATH, encoding='utf-8') as handle:
            configured = (json.load(handle) or {}).get('hq_lookup') or {}
    except (OSError, ValueError):
        return values
    if isinstance(configured, dict):
        values.update({k: v for k, v in configured.items() if k in DEFAULT_SETTINGS})
    return values


def is_enabled():
    """Whether a lookup can run: turned on in config, and a key to run it."""
    return bool(settings()['enabled']) and bool(ai_provider.get_openai_key())


def _base_host(value):
    host = (urlparse(value).hostname or value or '').strip().lower()
    return host[4:] if host.startswith('www.') else host


def _clip(text):
    """Head and tail of a page -- the tail is where footers keep addresses."""
    text = text or ''
    if len(text) <= PAGE_CHAR_LIMIT:
        return text
    head = PAGE_CHAR_LIMIT * 2 // 3
    tail = PAGE_CHAR_LIMIT - head
    return f'{text[:head]}\n[...trimmed...]\n{text[-tail:]}'


def _ranked_links(links, base_url):
    """Absolute, de-duplicated links, navigation-looking ones first."""
    seen = {}
    for href in links or []:
        try:
            absolute = urljoin(base_url, href)
        except ValueError:
            continue
        parsed = urlparse(absolute)
        if parsed.scheme not in ('http', 'https'):
            continue
        absolute = absolute.split('#')[0]
        if absolute and absolute not in seen:
            marker = absolute.lower()
            seen[absolute] = any(word in marker for word in _NAVIGATION_WORDS)
    ordered = sorted(seen, key=lambda url: (not seen[url], len(url)))
    return ordered[:MAX_LINKS]


class _FetchTool:
    """The model's single tool, with the budget it is allowed to spend.

    Every fetch goes through location_enrichment.get_public_page(), so a URL
    the model chose is subject to the same guards as one this pipeline
    followed itself: public hosts only (no reaching into the local network),
    capped bytes, capped redirects, HTML only.
    """

    # Plenty of large manufacturers do not answer an unattended HTTP client at
    # all -- eaton.com times out on every path. Left to itself a model will
    # spend its whole budget knocking on that same door, so the door is closed
    # for it after two failures and it is told to go elsewhere.
    MAX_HOST_FAILURES = 2

    def __init__(self, max_fetches, budget_seconds):
        self.max_fetches = max_fetches
        self.deadline = time.monotonic() + budget_seconds
        self.fetched = []          # every URL that returned a page
        self.hosts = set()         # their hosts, for the citation check
        self.failures = {}         # host -> how often it has let us down

    def __call__(self, name, arguments):
        if name != 'fetch_page':
            return {'error': f'No tool named {name!r}. Only fetch_page exists.'}

        url = str(arguments.get('url') or '').strip()
        if not urlparse(url).scheme:
            url = f'https://{url}'
        host = _base_host(url)
        if len(self.fetched) >= self.max_fetches:
            return {'error': 'Page budget spent. Answer from what you have read.'}
        remaining = self.deadline - time.monotonic()
        if remaining <= 1:
            return {'error': 'Time budget spent. Answer from what you have read.'}
        if self.failures.get(host, 0) >= self.MAX_HOST_FAILURES:
            return {'error': f'{host} does not answer automated requests. '
                             'It will not work -- read a different site instead.'}

        try:
            page, final_url = location_enrichment.get_public_page(
                url, deadline=time.monotonic() + min(remaining, 10),
            )
        except requests.RequestException as exc:
            self.failures[host] = self.failures.get(host, 0) + 1
            return {'error': f'Could not fetch {url}: {type(exc).__name__}. '
                             'Try a different site rather than this one again.'}
        if not page:
            self.failures[host] = self.failures.get(host, 0) + 1
            return {'error': f'No readable HTML page at {url}.'}

        self.fetched.append(final_url)
        self.hosts.add(_base_host(final_url))
        text, links = location_enrichment.page_text_and_links(page)
        return {
            'url': final_url,
            'text': _clip(text),
            'links': _ranked_links(links, final_url),
            'pages_left': self.max_fetches - len(self.fetched),
        }


def _prompt(organization, website, email, phone, max_fetches):
    domain = (email or '').rsplit('@', 1)[-1].strip().lower()
    known = [
        f'Company name: {organization or "(not stated in the email)"}',
        f'Company website: {website or "(none derived)"}',
        f'Contact email domain: {domain or "(none)"}',
        f'Contact phone as written: {phone or "(none)"}',
    ]
    return f"""You are locating a company's head office so that a sales lead can be
routed to the right territory manager. The lead's email gave no address.

{chr(10).join(known)}

Use the fetch_page tool to read public web pages. Start with the company's own
website and follow its links -- a head office address is usually on a Contact,
About, Locations or Investor page, or in the page footer. If its own site does
not give one, another public page about the company will do. You may fetch at
most {max_fetches} pages, so choose each one deliberately.

Every page you want must be read with the fetch_page tool: writing out that you
are about to fetch something is not fetching it, and nothing you have not
fetched can be cited.

Large manufacturers often block automated requests, so their own site may
simply time out. When a site fails, do not try it again -- read a different
public page about the company instead. Answer as soon as you know the city and
the state or country; a street address and postal code are welcome when a page
gives them, but do not spend fetches hunting for them.

Which office to report:
- Report the company's corporate headquarters.
- If the company is multinational AND has its own head office in the country
  the contact is in -- judge that from the phone's country code and the email
  domain -- report that national head office instead, and say so in "note".
  A lead from a US contact belongs to the US organisation.
- Report an office address, never a mailing box or a factory floor.

Then answer, filling in every field of the answer format:

- "found": whether you have an address. Leave the other fields blank if not.
- "address1": street address of that office.
- "state": US state as its two-letter abbreviation, Canadian province as its
  name or abbreviation, otherwise the region name or "".
- "zip": the postal code, if a page you read gives one. It decides the
  territory only in California, Pennsylvania and Nevada, so for a head office
  in one of those three states it is worth a fetch; elsewhere leave it blank
  rather than searching for it.
- "country": "USA", "Canada", or the English name of the country.
- "source_url": the exact URL you fetched that showed this address. Cite a page
  you actually read; do not cite one you did not fetch.
- "confidence": how sure you are that this is that company's head office.
- "note": one short sentence on which office this is and how you found it.

If the pages you read do not show a head office address, answer found=false and
say why in "note". A blank is expected and useful; a guessed city routes the
lead to the wrong person, which is not."""


def _normalize_country(value):
    country = ' '.join(str(value or '').split()).strip(' .,')
    if country.lower() in _US_COUNTRY_WORDS:
        return 'USA'
    if country.lower() == 'canada':
        return 'Canada'
    return country


def _normalize_state(value, country):
    state = ' '.join(str(value or '').split()).strip(' .,')
    if country == 'USA':
        if state.upper() in territory_map.US_STATE_ABBR:
            return state.upper()
        return _US_ABBR_BY_NAME.get(state.lower(), state)
    return state


def _validated(answer, tool, min_confidence):
    """Turn the model's JSON into lead fields, or nothing at all."""
    if not isinstance(answer, dict) or not answer.get('found'):
        logger.info('  HQ lookup found no address: %s', answer.get('note')
                    if isinstance(answer, dict) else answer)
        return {}

    try:
        confidence = float(answer.get('confidence') or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < min_confidence:
        logger.info('  HQ lookup discarded: confidence %.2f below %.2f',
                    confidence, min_confidence)
        return {}

    source_url = str(answer.get('source_url') or '').strip()
    if _base_host(source_url) not in tool.hosts:
        logger.warning('  HQ lookup discarded: cited %r, which it never fetched.',
                       source_url or '(nothing)')
        return {}

    country = _normalize_country(answer.get('country'))
    found = {
        'city': ' '.join(str(answer.get('city') or '').split()),
        'state': _normalize_state(answer.get('state'), country),
        'zip': re.sub(r'[^A-Za-z0-9 -]', '', str(answer.get('zip') or '')).strip(),
        'country': country,
    }
    address1 = ' '.join(str(answer.get('address1') or '').split())
    if address1:
        found['address1'] = address1

    tm, basis = territory_map.assign_tm(found)
    if not tm:
        logger.info('  HQ lookup discarded -- %s, %s does not route: %s',
                    found['city'] or '?', found['state'] or found['country'], basis)
        return {}

    found['_source_url'] = source_url
    found['_note'] = ' '.join(str(answer.get('note') or '').split())
    found['_confidence'] = confidence
    return found


def find_headquarters(organization=None, website=None, email=None, phone=None):
    """Research one company's head office. Returns lead fields, or {}.

    Never raises: a lookup that fails leaves the lead exactly as it was, which
    is what the reviewer would have seen anyway.
    """
    if not is_enabled():
        return {}

    website = (company_website.normalize_url(website)
               or company_website.website_from_email(email))
    organization = (organization or '').strip()
    if not website and not organization:
        return {}

    values = settings()
    tool = _FetchTool(int(values['max_fetches']), float(values['budget_seconds']))
    prompt = _prompt(organization, website, email, phone, tool.max_fetches)

    try:
        # More turns than pages: a refused fetch costs a turn without costing
        # a page, and running out of turns mid-search throws away the reading
        # already done.
        raw = ai_provider.call_llm_with_tools(
            prompt, FETCH_TOOL, tool, max_rounds=tool.max_fetches + 4,
            max_tokens=3000, text_format=ANSWER_SCHEMA,
        )
        answer = ai_provider.parse_json_response(raw)
    except Exception as exc:
        logger.warning('  HQ lookup failed for %s: %s',
                       organization or website, exc)
        return {}

    found = _validated(answer, tool, float(values['min_confidence']))
    if found:
        logger.info('  HQ lookup: %s, %s %s from %s',
                    found.get('city'), found.get('state'),
                    found.get('country'), found['_source_url'])
    return found


def apply_headquarters(lead):
    """Fill a lead's blank location fields from its company's head office.

    Only blanks are written -- anything the customer stated, or a reviewer
    typed, always wins -- and `_location_source` records where the rest came
    from, which the caller puts in the territory assignment basis.
    """
    if (lead.get('state') or '').strip():
        return {}
    # A lead that already routes has nothing to gain here: "Germany" with no
    # state is a complete answer, and researching Siemens' head office would
    # only spend an AI call to reach the manager it already has.
    if territory_map.assign_tm(lead)[0]:
        return {}

    found = find_headquarters(
        organization=lead.get('organization'), website=lead.get('website'),
        email=lead.get('email'), phone=lead.get('phone') or lead.get('phone_mobile'),
    )
    if not found:
        return {}

    # An unroutable country already on the lead ("Atlantis", a typo, a region
    # name) is still the customer's own statement about themselves. Writing a
    # US state beside it would produce an address that exists nowhere.
    stated_country = _normalize_country(lead.get('country'))
    if stated_country and stated_country.lower() != found['country'].lower():
        logger.info('  HQ lookup discarded: lead says %s, head office is in %s.',
                    stated_country, found['country'])
        return {}

    written = {}
    for key, value in found.items():
        if key.startswith('_') or not value:
            continue
        if not (lead.get(key) or '').strip():
            lead[key] = value
            written[key] = value
    # The note travels with the source, because for a multinational it is the
    # answer to the reviewer's first question: why Ohio and not Ireland?
    source = f"company HQ lookup ({found['_source_url']})"
    if found.get('_note'):
        source = f"{source} -- {found['_note']}"
    lead['_location_source'] = source
    return written


if __name__ == '__main__':
    # Run one real lookup by hand:  py hq_lookup.py
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / '.env', override=False)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    sample = {
        'organization': 'Eaton', 'website': 'https://eaton.com',
        'email': 'benemrick@eaton.com', 'phone': '+1 301 256-1792',
    }
    print(json.dumps(find_headquarters(**sample), indent=2))
