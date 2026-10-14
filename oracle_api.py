"""
Oracle Sales Cloud REST submitter.

The only path by which reviewed leads reach Oracle. It deliberately keeps the
public shape small: build a payload, submit it, and return the Oracle
identifiers needed for audit/retry.
"""

import base64
import difflib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

import country_codes
import description_compactor
import territory_map
from phone_parser import parse_phone

logger = logging.getLogger(__name__)

API_VERSION = '11.13.18.05'

# How many same-prefix accounts to pull back when checking for an existing
# customer, and how many to name in the warning shown to the operator.
MAX_ACCOUNT_CANDIDATES = 10
MAX_NAMED_CANDIDATES = 5

LEAD_SOURCE_VALUES = {
    'Prospecting',
    'ReadyTek',
    'Referrals',
    'Referrals - Kaman',
    'Referrals - GRW',
    'Targeted Marketing',
    'Trade Shows',
    'Webinars',
    'Website',
}

TRADE_SHOW_LEAD_SOURCE = 'Trade Shows'


def lead_source_for(lead, default='Website'):
    """
    The Lead Source a lead goes to Oracle with.

    A value chosen for the lead -- on the spreadsheet upload form or the
    review form -- wins. Otherwise a lead met at a trade show is 'Trade
    Shows' (Keaton, 2026-09-28): one carrying a show name, or a sheet of leads
    attached to an email, which has always been titled "Tradeshow". Anything
    else is `default`, the Website inquiry this pipeline was built for.
    """
    chosen = (lead.get('lead_source') or '').strip()
    if chosen in LEAD_SOURCE_VALUES:
        return chosen
    if (lead.get('trade_show') or '').strip() or lead.get('source') == 'email_excel_attachment':
        return TRADE_SHOW_LEAD_SOURCE
    return default

# How to express PrimaryContactCountry. 'code' sends the ISO alpha-2 territory
# code (US, DE), which is what this tenant has accepted for US/CA to date;
# 'name' sends Oracle's dropdown label ('Germany') instead. Overridable via
# config.oracle.country_format so a tenant that rejects codes is a config
# change rather than a code change.
COUNTRY_FORMATS = ('code', 'name')

_COMPANY_LEGAL_SUFFIXES = re.compile(
    r'\b(INC|INCORPORATED|CORP|CORPORATION|LLC|L\.L\.C\.|LTD|LIMITED|CO|COMPANY|GMBH|PLC|S\.A\.|BV|N\.V\.|AG|SAS)\b',
    re.IGNORECASE
)


def normalize_company_name(name):
    """
    Clean and normalize a company name for similarity matching:
    - Normalizes case and punctuation.
    - Strips common corporate/legal entity suffixes (Inc, Corp, LLC, etc.).
    - Collapses extra spaces.
    """
    if not name:
        return ''
    cleaned = name.upper()
    cleaned = re.sub(r'[^\w\s]', ' ', cleaned)
    cleaned = _COMPANY_LEGAL_SUFFIXES.sub('', cleaned)
    return ' '.join(cleaned.split())


def calculate_company_similarity(name1, name2):
    """
    Calculate a similarity score between 0.0 and 1.0 for two company names using
    normalized base comparison and sequence matching.
    """
    norm1 = normalize_company_name(name1)
    norm2 = normalize_company_name(name2)
    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 1.0

    ratio = difflib.SequenceMatcher(None, norm1, norm2).ratio()

    # If all tokens of the shorter normalized name match tokens of the longer name
    tokens1 = set(norm1.split())
    tokens2 = set(norm2.split())
    if tokens1 and tokens2:
        intersection = tokens1.intersection(tokens2)
        smaller_len = min(len(tokens1), len(tokens2))
        if len(intersection) == smaller_len and any(len(t) > 2 for t in intersection):
            ratio = max(ratio, 0.85)

    return ratio


def rank_company_accounts(organization, accounts):
    """Return every OSC account, most similar to *organization* first.

    Party ID is the stable identity.  Deduplicating on it prevents a page
    boundary overlap from showing the same account twice, while retaining
    same-named accounts that are genuinely different Oracle records.
    """
    unique = {}
    for account in accounts or []:
        party_id = account.get('PartyId')
        name = (account.get('OrganizationName') or '').strip()
        if party_id in (None, '') or not name:
            continue
        key = str(party_id)
        if key not in unique:
            item = dict(account)
            item['similarity'] = calculate_company_similarity(organization, name)
            unique[key] = item
    return sorted(
        unique.values(),
        key=lambda item: (
            -item['similarity'],
            (item.get('OrganizationName') or '').casefold(),
            str(item.get('PartyId')),
        ),
    )

# Oracle's custom LOV stores a code/value in IndustryDescriptionBSE_c and
# exposes the visible Create Lead dropdown label via
# IndustryDescriptionBSE_cMeaning. Confirmed live through read-only API samples
# from this tenant on 2026-07-27.
# Attachment child-resource names to try when link discovery doesn't turn one
# up. AllLeadAttachments is what Oracle's own Sales Leads REST reference
# documents for this object (leads/{leadsUniqID}/child/AllLeadAttachments);
# the generic Attachment/Attachments names are kept behind it because other CX
# objects use those and tenants do vary.
ATTACHMENT_CHILD_NAMES = ('AllLeadAttachments', 'Attachment', 'Attachments')

# Oracle validates CategoryName against a lookup, and which codes exist is
# tenant configuration. MISC is the standard seeded code; the spelled-out label
# is accepted on some tenants instead. Only tried in order when Oracle
# explicitly rejects the category -- see _is_category_error().
DEFAULT_ATTACHMENT_CATEGORIES = ('MISC', 'Miscellaneous')

_CONTENT_TYPES = {
    '.msg': 'application/vnd.ms-outlook',
    '.eml': 'message/rfc822',
    '.txt': 'text/plain',
    '.html': 'text/html',
    '.pdf': 'application/pdf',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls': 'application/vnd.ms-excel',
}


def _content_type(path):
    return _CONTENT_TYPES.get(Path(path).suffix.lower(), 'application/octet-stream')


def _is_category_error(status_code, detail):
    """Whether an Oracle rejection looks like it's about CategoryName rather
    than the request as a whole -- the one failure worth retrying with a
    different value instead of giving up."""
    return status_code in (400, 422) and 'categor' in (detail or '').lower()


MARKET_TO_API_VALUE = {
    'AEROSPACE/DEFENSE': 'AEROSPACE/DEFENSE',
    'ALTERNATIVE ENERGY': 'ALTERNATIVE ENERGY',
    'ANALYTICAL DEVICES': 'ANALYTICAL DEVICES',
    'FOREIGN DISTRIBUTORS': 'FOREIGN DISTRIBUTORS',
    'INDUSTRIAL': 'NON-TARGETED SEALS/SPRINGS',
    'MEDICAL DEVICES': 'MEDICAL DEVICES',
    'MEDICAL ELECTRONICS': 'MEDICAL ELECTRONICS',
    'MOBILITY': 'AUTOMOTIVE',
    'OIL & GAS': 'PETROCHEMICAL',
    'POWER T&D': 'HC/ELECTRICAL',
}


class OracleApiError(RuntimeError):
    def __init__(self, message, status_code=None, response_text=None,
                 outcome_unknown=False):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text
        # True only after a create request was sent but no authoritative
        # response was received. Retrying such a request can create a second
        # lead, so the submitter must reconcile it instead.
        self.outcome_unknown = outcome_unknown


def build_project_name(lead, trade_show_name, source_label='Tradeshow'):
    org = (lead.get('organization') or '').upper().strip()
    first = (lead.get('first_name') or '').strip()
    last = (lead.get('last_name') or '').strip()
    name = f'{first} {last}'.strip()
    return ' - '.join(
        part for part in (org, name, source_label, trade_show_name)
        if part and str(part).strip()
    )


def submission_blockers(lead):
    """Human-fixable omissions that must not be guessed during submission."""
    blockers = []
    country = _clean(lead.get('country'))
    state = _clean(lead.get('state'))
    if country:
        if not country_codes.to_alpha2(country):
            blockers.append(
                f'Country {country!r} is not recognized; correct it before sending.'
            )
    elif not _looks_like_us_state(state):
        blockers.append(
            'Country is missing and the state does not establish that this is a US lead.'
        )
    if not _clean(lead.get('assigned_tm')):
        blockers.append(
            'Territory Manager is unassigned; complete or correct the location, '
            'or choose a manager manually.'
        )
    # An uploaded trade-show list titles each lead after the show ("... -
    # Tradeshow - Space Tech Expo"). One whose sheet never named the show
    # would go out without it. Lists from anywhere else need no show.
    if (lead.get('source') == 'spreadsheet_upload'
            and lead_source_for(lead) == TRADE_SHOW_LEAD_SOURCE
            and not _clean(lead.get('trade_show'))):
        blockers.append(
            'Trade Show is missing; enter the show this lead came from.'
        )
    return blockers


def _q_escape(value):
    """Escape a value for interpolation into an Oracle REST q= expression."""
    return str(value).replace("'", "''")


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


_US_STATE_KEYS = (
    {abbr.upper() for abbr in territory_map.US_STATE_ABBR}
    | {name.upper() for name in territory_map.US_STATE_ABBR.values()}
)


def _looks_like_us_state(value):
    return (value or '').strip().upper() in _US_STATE_KEYS


def _resolve_country(value, state=None, country_format='code'):
    """
    Resolve a free-text country into what Oracle should be sent.

    Returns (send_value, alpha2, warning). `alpha2` is the caller's cue for the
    State-vs-Province split and is None when the country couldn't be resolved;
    `send_value` is None when nothing should go on the payload.

    An unrecognized country is dropped rather than passed through verbatim.
    Oracle's territory field won't resolve an arbitrary string, so sending one
    either fails the whole POST or is silently discarded -- both worse than
    creating the lead without a country and flagging it for a human.
    """
    raw = _clean(value)
    if raw:
        alpha2 = country_codes.to_alpha2(raw)
        if not alpha2:
            return None, None, (
                f'Country "{raw}" did not match any ISO country name or code; '
                'country omitted from the Oracle payload. Fix the lead or add an '
                'alias to country_codes.ALIASES.'
            )
        send = alpha2 if country_format == 'code' else country_codes.NAME_BY_CODE[alpha2]
        return send, alpha2, None

    # No country on the lead. A recognizable US state is enough to keep the
    # historical US default quietly; anything else gets flagged, because a
    # blank country on an international lead would otherwise be filed as US.
    send = 'US' if country_format == 'code' else country_codes.NAME_BY_CODE['US']
    if _looks_like_us_state(state):
        return send, 'US', None
    return send, 'US', (
        'No country on lead; defaulted to US. Verify -- an international lead '
        'with a blank country is filed as US.'
    )


def _split_phone(raw_phone):
    parsed = parse_phone(raw_phone)
    if not parsed:
        return {}
    number = parsed.get('number')
    return {
        'country_code': parsed.get('country_code'),
        'area_code': parsed.get('area_code'),
        'number': number,
    }


def _put(payload, key, value):
    value = _clean(value)
    if value:
        payload[key] = value


def _party_id(value):
    """An Oracle party id as the integer CustomerId wants, or None.

    Party ids arrive as integers from REST and as strings from anywhere they
    have been stored; a party *number* (SCE_150572) is not one and must not be
    sent as one -- a non-integer here would be a 400 that rejects the lead.
    """
    if value in (None, ''):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# The two attributes that link a lead to records Oracle already has, and the
# name-carrying attribute each one falls back to. Sent as identifiers because
# names do not link (see build_lead_payload); listed here so submit_lead can
# undo that in the one case where a tenant rejects them.
LINK_BY_ID_FIELDS = ('CustomerId', 'ContactPartyNumber')


def relink_payload_by_name(payload, existing):
    """Rewrite an identifier link back into the name form, in place.

    The escape hatch for a tenant whose leads resource refuses CustomerId or
    ContactPartyNumber: the lead still goes, carrying the names, rather than
    being rejected whole over the linking attribute.
    """
    existing = existing or {}
    for field in LINK_BY_ID_FIELDS:
        payload.pop(field, None)
    account = _clean(existing.get('account_name'))
    contact = _clean(existing.get('contact_name'))
    if account:
        payload['CustomerPartyName'] = account
        payload['EnableCreateAccount'] = 'N'
    if contact:
        payload['PrimaryContactPartyName'] = contact
        payload['EnableCreateContact'] = 'N'
    return payload


def build_lead_payload(lead, trade_show_name, source_label='Tradeshow',
                       lead_source='Website', campaign_name=None,
                       market_map=None, existing=None, country_format='code',
                       website_field=None, owner_resolver=None):
    """
    Convert the pipeline's lead shape into Oracle's REST lead payload.

    `existing` is the result of OracleLeadApiClient.resolve_existing_party():
    when it finds an account or contact already in Oracle, the payload points
    at that record by party identifier with EnableCreateAccount /
    EnableCreateContact set to 'N', instead of creating a fresh one.

    `website_field` names the attribute behind the Website box on this
    tenant's lead layout (config.json oracle.website_field). It is configured
    rather than assumed because an attribute this tenant doesn't recognize
    comes back "not a valid field" as a 400 that rejects the whole lead, so a
    guess here would cost far more than the field is worth. Run
    oracle_field_probe.py once to read the real name off the tenant. Unset
    simply means no website is sent.

    `owner_resolver` turns a Territory Manager name into the spelling
    Oracle will actually match -- see OracleLeadApiClient.resolve_owner().
    Optional so this stays a pure function; without one the name is sent
    as stored, which is what it did before the resolver existed.

    Returns (payload, project_name, warnings). Warnings are non-fatal mapping
    notes that should be stored with the lead for review.
    """
    market_map = market_map or MARKET_TO_API_VALUE
    existing = existing or {}
    warnings = list(existing.get('warnings') or [])
    project_name = build_project_name(lead, trade_show_name, source_label)

    lead_source = (lead_source or 'Website').strip()
    if lead_source not in LEAD_SOURCE_VALUES:
        warnings.append(f'Unverified lead source "{lead_source}"; sending as provided.')

    first = _clean(lead.get('first_name'))
    last = _clean(lead.get('last_name'))
    full_name = ' '.join(part for part in (first, last) if part)
    organization = _clean(lead.get('organization'))
    country, country_alpha2, country_warning = _resolve_country(
        lead.get('country'), lead.get('state'), country_format
    )
    if country_warning:
        warnings.append(country_warning)

    payload = {
        'Name': project_name,
        'OpportunityName': project_name,
        'LeadSourceDescriptionBSE_c': lead_source,
        'Description': _clean(lead.get('lead_details')) or _clean(lead.get('notes_body')) or 'Lead imported by Lead Input Pipeline.',
        'EnableCreateAccount': 'Y',
        'EnableCreateContact': 'Y',
    }
    _put(payload, 'PrimaryContactCountry', country)

    # Point at an existing account/contact when one was matched, otherwise let
    # Oracle create them.
    #
    # The link is made with the party identifier -- CustomerId for the account,
    # ContactPartyNumber for the contact -- because a name does not make it.
    # This used to send the record's exact stored name with EnableCreate* =
    # 'N', on the reading that Oracle would resolve it. It does not: of twelve
    # leads submitted that way, ten came back with EnableCreateAccount flipped
    # to 'Y', CustomerId empty and the company left as loose text, so the lead
    # reached Oracle attached to nothing (audited 2026-09-18, and the reason
    # the Assign-to-existing-company button appeared to do nothing). The
    # earlier note here said no ID attribute existed on leads; leads/describe
    # on this tenant lists both of the two used below. The four names tried
    # then -- CustomerPartyId, PrimaryContactPartyId, AccountPartyId, PartyId
    # -- are indeed not among them.
    existing_account = _clean(existing.get('account_name'))
    existing_contact = _clean(existing.get('contact_name'))
    account_party_id = _party_id(existing.get('account_party_id'))
    contact_party_number = _clean(existing.get('contact_party_number'))

    if account_party_id is not None:
        payload['CustomerId'] = account_party_id
        payload['EnableCreateAccount'] = 'N'
    elif existing_account:
        # Matched, but the identifier did not come back. Better than nothing:
        # the lead carries the right company name for a human to link.
        payload['CustomerPartyName'] = existing_account
        payload['EnableCreateAccount'] = 'N'
        warnings.append(
            f'No party ID for matched account {existing_account!r}; sent by name, '
            'which Oracle may not resolve -- check the company on the lead.'
        )
    elif organization:
        payload['CustomerPartyName'] = organization

    if contact_party_number:
        payload['ContactPartyNumber'] = contact_party_number
        payload['EnableCreateContact'] = 'N'
    elif existing_contact:
        payload['PrimaryContactPartyName'] = existing_contact
        payload['EnableCreateContact'] = 'N'
        warnings.append(
            f'No party number for matched contact {existing_contact!r}; sent by name, '
            'which Oracle may not resolve -- check the contact on the lead.'
        )
    elif full_name:
        payload['PrimaryContactPartyName'] = full_name
    _put(payload, 'PrimaryContactPersonFirstName', first)
    _put(payload, 'PrimaryContactPersonLastName', last)
    _put(payload, 'JobTitle', lead.get('title'))
    _put(payload, 'PrimaryContactEmailAddress', lead.get('email'))
    if website_field:
        _put(payload, website_field, lead.get('website'))

    rank = (lead.get('rank') or '').strip().upper()
    if rank in ('HOT', 'WARM'):
        payload['Rank'] = rank
    elif rank:
        warnings.append(f'Unsupported rank "{lead.get("rank")}"; rank omitted.')

    # CampaignName field is read-only or not writable on REST Lead creation in Oracle Sales Cloud
    # (Oracle returns 400: "Campaign cannot be updated." if sent in POST).

    market = (lead.get('market') or 'INDUSTRIAL').strip().upper()
    api_market = market_map.get(market)
    if api_market:
        payload['IndustryDescriptionBSE_c'] = api_market
    else:
        warnings.append(
            f'No verified API value for market "{market}"; defaulted to INDUSTRIAL mapping.'
        )
        payload['IndustryDescriptionBSE_c'] = market_map['INDUSTRIAL']

    phone = _split_phone(lead.get('phone') or lead.get('phone_mobile'))
    if phone:
        _put(payload, 'PrimaryPhoneCountryCode', phone.get('country_code'))
        _put(payload, 'PrimaryPhoneAreaCode', phone.get('area_code'))
        _put(payload, 'PrimaryPhoneNumber', phone.get('number'))
        _put(payload, 'WorkPhoneCountryCode', phone.get('country_code'))
        _put(payload, 'WorkPhoneAreaCode', phone.get('area_code'))
        _put(payload, 'WorkPhoneNumber', phone.get('number'))
        _put(payload, 'PhoneAreaCodeBSE_c', phone.get('area_code'))
        _put(payload, 'PhoneNumberBSE_c', phone.get('number'))

    _put(payload, 'PrimaryContactAddress1', lead.get('address1'))
    _put(payload, 'PrimaryContactAddress2', lead.get('address2'))
    _put(payload, 'PrimaryContactCity', lead.get('city'))
    # An unresolved country (alpha2 None) means a country was named but not
    # recognized -- never a US alias, since those all resolve -- so Province is
    # the right home for its state value.
    if country_alpha2 == 'US':
        _put(payload, 'PrimaryContactState', lead.get('state'))
    else:
        _put(payload, 'PrimaryContactProvince', lead.get('state'))
    _put(payload, 'PrimaryContactPostalCode', lead.get('zip'))

    assigned_tm = _clean(lead.get('assigned_tm'))
    if assigned_tm:
        # Oracle matches this field on create as an exact, case-sensitive
        # string, and a near-miss is discarded in silence: 201 Created, no
        # warning, and the lead owned by whoever the API authenticated as
        # rather than by the rep. That is not a hypothetical -- 'Marcus
        # James' for Oracle's 'MARCUS JAMES' sent every UK, Ireland and
        # Scandinavian lead to the API account for weeks, and nothing in
        # the pipeline noticed. An earlier comment here predicted a 400
        # would surface such a mismatch; no 400 ever comes.
        #
        # So the name is resolved against Oracle first, which is
        # case-insensitive, and what gets sent is Oracle's own spelling.
        resolved = owner_resolver(assigned_tm) if owner_resolver else None
        if resolved and resolved.get('name'):
            payload['OwnerPartyName'] = resolved['name']
            # Case and spacing are what the resolver is for, so correcting
            # those is not news -- every Marcus James lead would carry the
            # same notice forever. A match that changed more than that is
            # worth a look: it means Oracle matched a different string.
            if resolved['name'].casefold().split() != assigned_tm.casefold().split():
                warnings.append(
                    f'Territory Manager "{assigned_tm}" matched Oracle resource '
                    f'"{resolved["name"]}"; check it is the right person.'
                )
            if resolved.get('ambiguous'):
                warnings.append(
                    f'More than one Oracle resource matches "{assigned_tm}"; '
                    f'used {resolved["name"]} ({resolved.get("party_number")}).'
                )
        else:
            # Send it anyway -- unchanged from the old behaviour -- but say
            # so on the lead. Silence is the actual defect being fixed here.
            payload['OwnerPartyName'] = assigned_tm
            if owner_resolver:
                warnings.append(
                    f'No Oracle resource matches Territory Manager '
                    f'"{assigned_tm}"; Oracle will ignore the owner and the '
                    f'lead will belong to the submitting account. Check the '
                    f'spelling against Oracle.'
                )

    return {k: v for k, v in payload.items() if v is not None}, project_name, warnings


class OracleLeadApiClient:
    def __init__(self, config, username, password, session=None):
        self.base_url = config['base_url'].rstrip('/')
        self.lead_source = config.get('lead_source', 'Website')
        self.campaign_name = config.get('campaign_name')
        self.timeout = int(config.get('api_timeout_seconds', 60))
        country_format = (config.get('country_format') or 'code').strip().lower()
        if country_format not in COUNTRY_FORMATS:
            logger.warning(
                'Unknown country_format %r in config; falling back to "code".',
                country_format,
            )
            country_format = 'code'
        self.country_format = country_format
        # Which attribute the Website box maps to varies by tenant, and an
        # unrecognized one 400s the whole lead -- so it stays unset until
        # oracle_field_probe.py confirms the name against this instance.
        self.website_field = (config.get('website_field') or '').strip() or None
        if not self.website_field:
            logger.info(
                'config.oracle.website_field is unset, so derived company '
                'websites are stored locally but not sent to Oracle. '
                'Run "py oracle_field_probe.py" to find the attribute name.'
            )
        # config.oracle.attachment_category pins a single tenant-specific
        # value once one is known to work; unset means try the usual codes.
        category = config.get('attachment_category')
        self.attachment_categories = (
            (category,) if category else DEFAULT_ATTACHMENT_CATEGORIES
        )
        self.username = username
        self.password = password
        self.session = session or requests.Session()
        # Territory Manager name -> Oracle resource, filled on demand.
        # See resolve_owner() for why the lookup exists at all.
        self._owner_cache = {}

    @property
    def leads_url(self):
        return f'{self.base_url}/crmRestApi/resources/{API_VERSION}/leads'

    @property
    def accounts_url(self):
        return f'{self.base_url}/crmRestApi/resources/{API_VERSION}/accounts'

    @property
    def contacts_url(self):
        return f'{self.base_url}/crmRestApi/resources/{API_VERSION}/contacts'

    def _query(self, url, q, fields, limit=MAX_ACCOUNT_CANDIDATES):
        """Read-only lookup whose failure is distinct from "no matches".

        Creating a new account after a transient lookup failure defeats the
        lookup's duplicate-prevention purpose, so callers retry the submission
        later instead of silently treating an outage as an empty result.
        """
        try:
            resp = self.session.get(
                url,
                params={'q': q, 'fields': fields, 'onlyData': 'true', 'limit': str(limit)},
                auth=(self.username, self.password),
                headers={'Accept': 'application/json'},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise OracleApiError(
                    f'Oracle lookup failed ({resp.status_code}): {resp.text}',
                    status_code=resp.status_code, response_text=resp.text,
                )
            try:
                return resp.json().get('items') or []
            except (ValueError, AttributeError) as e:
                raise OracleApiError('Oracle lookup returned invalid JSON') from e
        except (requests.RequestException, ValueError) as e:
            raise OracleApiError(f'Oracle lookup request failed: {e}') from e

    def list_accounts(self, page_size=500):
        """Read every OSC account, following Oracle REST pagination.

        This is used by the review-form company picker.  It deliberately does
        not apply a server-side name filter: the operator must be able to find
        an existing account even when the extracted company spelling is poor.
        """
        accounts = []
        offset = 0
        fields = 'PartyId,PartyNumber,OrganizationName'
        while True:
            try:
                response = self.session.get(
                    self.accounts_url,
                    params={
                        'fields': fields,
                        'onlyData': 'true',
                        'limit': str(page_size),
                        'offset': str(offset),
                    },
                    auth=(self.username, self.password),
                    headers={'Accept': 'application/json'},
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    raise OracleApiError(
                        f'Oracle account list failed ({response.status_code}): '
                        f'{response.text}',
                        status_code=response.status_code,
                        response_text=response.text,
                    )
                payload = response.json()
            except OracleApiError:
                raise
            except requests.RequestException as exc:
                raise OracleApiError(
                    f'Oracle account list request failed: {exc}'
                ) from exc
            except (ValueError, AttributeError) as exc:
                raise OracleApiError('Oracle account list returned invalid JSON') from exc

            items = payload.get('items') or []
            accounts.extend(items)
            if not payload.get('hasMore') or not items:
                break
            # Use the actual count because the final page size and tenant-side
            # caps need not equal the requested limit.
            offset += len(items)
        return accounts

    @property
    def resources_url(self):
        return f'{self.base_url}/crmRestApi/resources/{API_VERSION}/resources'

    def resolve_owner(self, name):
        """
        The Oracle resource for a Territory Manager name, or None.

        Oracle matches OwnerPartyName on create as an exact, case-sensitive
        string. Anything off by one letter's case is discarded in silence --
        no 400, no warning, a 201 like any other -- and the lead is left owned
        by whoever the API authenticated as. Across the first 43 submissions
        the correlation was total: 39 names that matched Oracle's spelling
        exactly kept their owner, and the 3 that differed only in case
        (`Marcus James` for `MARCUS JAMES`, `john Pan`, `jeff Frederick`) all
        landed on the API account instead.

        This query is the part that is case-insensitive, so it is what turns
        whatever the territory map or a hand-typed override says into the
        spelling Oracle will actually match. Returns the canonical PartyName
        alongside PartyId so the caller does not have to care which one it
        sends.

        Cached per client: one submit run routes many leads to the same
        handful of reps, and the set does not change mid-run.
        """
        key = (name or '').strip().lower()
        if not key:
            return None
        if key in self._owner_cache:
            return self._owner_cache[key]

        result = None
        try:
            items = self._query(
                self.resources_url,
                f"PartyName LIKE '{_q_escape(name.strip())}'",
                'PartyName,PartyId,PartyNumber,EndDateActive',
                limit=5,
            )
            # A retired rep keeps their resource record; EndDateActive is what
            # separates them from a current one. Oracle writes 4712-12-31 for
            # "no end date", so a plain date comparison keeps the active rows.
            today = datetime.now(timezone.utc).date().isoformat()
            active = [i for i in items
                      if not i.get('EndDateActive') or i['EndDateActive'] >= today]
            candidates = active or items
            if candidates:
                result = {
                    'name': candidates[0].get('PartyName'),
                    'party_id': candidates[0].get('PartyId'),
                    'party_number': candidates[0].get('PartyNumber'),
                    'ambiguous': len(candidates) > 1,
                }
        except OracleApiError as exc:
            # Never fatal. Failing to look the name up is not a reason to
            # refuse a lead -- the payload falls back to the name as stored,
            # which is exactly what it sent before this existed.
            logger.warning('Could not resolve Oracle owner %r: %s', name, exc)
            return None

        self._owner_cache[key] = result
        return result


    def find_contact_by_email(self, email):
        """The Oracle contact with this exact email, or None.

        Oracle matches EmailAddress case-insensitively (verified against this
        tenant on 2026-07-31), so the lead's casing doesn't matter. An email
        hit is the strongest signal available: the contact record also carries
        its own AccountName, which resolves the company with no guessing.
        """
        email = (email or '').strip()
        if not email:
            return None
        items = self._query(
            self.contacts_url,
            f"EmailAddress='{_q_escape(email)}'",
            'PartyId,PartyNumber,ContactName,EmailAddress,AccountName,AccountPartyId,AccountPartyNumber',
            limit=2,
        )
        return items[0] if items else None

    def find_accounts_by_name(self, organization):
        """
        (exact_matches, prefix_candidates) for a company name.

        Account names are stored uppercase. Only an exact match is trusted for
        linking -- a prefix search for "MOTION" returns 62 accounts and
        "RAYTHEON" 33, including near-duplicates like "RAYTHEON (1375600)", so
        picking one automatically would silently attach the lead to the wrong
        customer.

        Candidate hits are stringently filtered using calculate_company_similarity()
        so generic prefix hits (e.g. "Cat" matching "Caterpillar") do not
        raise false-positive duplicate account warnings.
        """
        organization = (organization or '').strip()
        if not organization:
            return [], []
        name = organization.upper()
        fields = 'PartyId,PartyNumber,OrganizationName'
        exact = self._query(
            self.accounts_url, f"OrganizationName='{_q_escape(name)}'", fields, limit=2,
        )
        raw_prefix = self._query(
            self.accounts_url, f"OrganizationName LIKE '{_q_escape(name)}%'", fields,
        )
        
        # Stringent candidate filtering: keep only candidates whose similarity score is >= 0.80
        prefix = []
        for account in raw_prefix:
            acct_name = account.get('OrganizationName') or ''
            if acct_name.upper() == name:
                continue
            if calculate_company_similarity(organization, acct_name) >= 0.80:
                prefix.append(account)

        return exact, prefix

    def resolve_existing_party(self, lead):
        """
        Decide whether this lead belongs to an account/contact Oracle already
        has. Returns a dict consumed by build_lead_payload():

            {'account_name', 'account_party_id', 'account_party_number',
             'contact_name', 'contact_party_number', 'match_basis', 'warnings'}

        The party identifiers matter more than the names: sending a name is
        what this used to do, and Oracle silently declined to resolve it (see
        build_lead_payload). Every lookup here already reads the identifiers
        back, so carrying them costs nothing.

        Confidence tiers, highest first:
          1. exact contact email  -> link contact AND its account
          2. exactly one account of that exact name -> link the account only
          3. anything else -> create new, and warn with the candidates
        """
        result = {'account_name': None, 'account_party_id': None,
                  'account_party_number': None, 'contact_name': None,
                  'contact_party_number': None,
                  'match_basis': 'none', 'warnings': []}

        # A human selection from the OSC account picker outranks inference.
        # Still retain an exact-email contact when it belongs to that selected
        # account; otherwise honor the reviewed account and avoid attaching a
        # contact from a different employer.
        selected_id = _party_id(lead.get('oracle_account_party_id'))
        selected_name = _clean(lead.get('oracle_account_name'))
        if selected_id is not None and selected_name:
            result['account_name'] = selected_name
            result['account_party_id'] = selected_id
            result['account_party_number'] = _clean(
                lead.get('oracle_account_party_number')
            )
            result['match_basis'] = 'account_selected'
            contact = self.find_contact_by_email(lead.get('email'))
            if contact and _party_id(contact.get('AccountPartyId')) == selected_id:
                result['contact_name'] = contact.get('ContactName')
                result['contact_party_number'] = contact.get('PartyNumber')
                result['match_basis'] = 'account_selected_contact_email'
            elif contact:
                result['warnings'].append(
                    f"Existing Oracle contact {contact.get('ContactName')!r} was not "
                    'linked because it belongs to a different account.'
                )
            result['warnings'].append(
                f"Linked to reviewer-selected Oracle account {selected_name!r}"
                f" (party ID {selected_id}); no new account created."
            )
            return result

        contact = self.find_contact_by_email(lead.get('email'))
        if contact:
            result['contact_name'] = contact.get('ContactName')
            result['contact_party_number'] = contact.get('PartyNumber')
            result['account_name'] = contact.get('AccountName')
            result['account_party_id'] = contact.get('AccountPartyId')
            result['account_party_number'] = contact.get('AccountPartyNumber')
            result['match_basis'] = 'contact_email'
            result['warnings'].append(
                f"Linked to existing Oracle contact {contact.get('ContactName')!r}"
                f" (#{contact.get('PartyNumber')})"
                + (f" at account {contact.get('AccountName')!r}" if contact.get('AccountName') else '')
                + '; no new contact created.'
            )
            return result

        organization = (lead.get('organization') or '').strip()
        exact, prefix = self.find_accounts_by_name(organization)
        if len(exact) == 1:
            account = exact[0]
            result['account_name'] = account.get('OrganizationName')
            result['account_party_id'] = account.get('PartyId')
            result['account_party_number'] = account.get('PartyNumber')
            result['match_basis'] = 'account_name_exact'
            result['warnings'].append(
                f"Linked to existing Oracle account {account.get('OrganizationName')!r}"
                f" (#{account.get('PartyNumber')}); no new account created."
            )
            return result

        if prefix:
            candidates = [a.get('OrganizationName') for a in prefix if a.get('OrganizationName')]
            names = ', '.join(
                repr(name) for name in candidates[:MAX_NAMED_CANDIDATES]
            )
            more = '' if len(candidates) <= MAX_NAMED_CANDIDATES else f' (+{len(candidates) - MAX_NAMED_CANDIDATES} more)'
            result['match_basis'] = 'ambiguous'
            result['candidates'] = candidates
            result['warnings'].append(
                f'Company {organization!r} resembles {len(prefix)} existing Oracle '
                f'account(s) ({names}{more}). '
                f'Choose whether to assign to an existing company or create as a new company.'
            )
        return result

    def submit_lead(self, lead, trade_show_name, source_label='Tradeshow', bypass_dedup=False):
        # Look up the company/contact before creating anything. A lookup outage
        # is not evidence that the party does not exist, so it is allowed to
        # stop this attempt and be retried safely later.
        existing = self.resolve_existing_party(lead)

        if (existing or {}).get('match_basis') == 'ambiguous' and not bypass_dedup:
            warning_msg = (
                existing['warnings'][0]
                if (existing and existing.get('warnings'))
                else 'Company closely matches existing Oracle account(s).'
            )
            raise OracleApiError(f'Submission blocked: {warning_msg}')

        payload, project_name, warnings = build_lead_payload(
            lead, trade_show_name, source_label=source_label,
            lead_source=lead_source_for(lead, self.lead_source),
            campaign_name=self.campaign_name,
            existing=existing,
            country_format=self.country_format,
            website_field=self.website_field,
            owner_resolver=self.resolve_owner,
        )
        logger.info(
            'Submitting lead via Oracle REST API: %s (account/contact match: %s)',
            project_name, (existing or {}).get('match_basis', 'none'),
        )
        
        # --- Pre-submission Dedup Check against Oracle ---
        if bypass_dedup:
            logger.info('Bypassing pre-submission dedup check against Oracle for project %s', project_name)
        else:
            try:
                email = (lead.get('email') or '').strip()
                # Single-quotes inside a value terminate the literal and make
                # Oracle reject the whole query -- doubling them is the escape
                # (O'Brien Medical is a realistic organization name). The query
                # goes through params= so requests URL-encodes the spaces and
                # ampersands a project name routinely contains.
                queries = [f"Name='{_q_escape(project_name)}'"]
                if email:
                    queries.append(f"PrimaryContactEmailAddress='{_q_escape(email)}'")
                q_str = ' OR '.join(queries)

                check_resp = self.session.get(
                    self.leads_url,
                    params={'q': q_str},
                    auth=(self.username, self.password),
                    headers={'Accept': 'application/json'},
                    timeout=self.timeout,
                )
                if check_resp.status_code == 200:
                    existing_items = check_resp.json().get('items', [])
                    if existing_items:
                        existing = existing_items[0]
                        exist_id = existing.get('LeadId')
                        exist_num = existing.get('LeadNumber')
                        logger.warning('Duplicate lead found in Oracle (%s / %s). Skipping creation.', exist_num, exist_id)
                        return {
                            'success': True,
                            'project_name': project_name,
                            'oracle_record_id': str(exist_id or exist_num),
                            'oracle_lead_number': exist_num,
                            'field_warnings': warnings + [f'Lead already exists in Oracle ({exist_num})'],
                            'response': existing,
                        }
                elif check_resp.status_code != 200:
                    raise OracleApiError(
                        f'Oracle lead dedup lookup failed ({check_resp.status_code}): '
                        f'{check_resp.text}', status_code=check_resp.status_code,
                        response_text=check_resp.text,
                    )
            except OracleApiError:
                raise
            except requests.RequestException as check_err:
                raise OracleApiError(
                    f'Oracle lead dedup lookup request failed: {check_err}'
                ) from check_err
            except (ValueError, AttributeError) as check_err:
                raise OracleApiError(
                    f'Oracle lead dedup lookup returned invalid JSON: {check_err}'
                ) from check_err

        description = payload.get('Description') or ''
        if len(description) > description_compactor.ORACLE_DESCRIPTION_MAX_LENGTH:
            compacted, method = description_compactor.compact_description(description)
            payload['Description'] = compacted
            warnings.append(
                f'Lead Details compacted from {len(description)} to {len(compacted)} '
                f'characters to fit Oracle ({"AI" if method == "ai" else "safe fallback"}).'
            )
            logger.info(
                'Compacted Oracle Lead Details from %d to %d characters (%s).',
                len(description), len(compacted), method,
            )

        def post_lead(body):
            try:
                return self.session.post(
                    self.leads_url,
                    auth=(self.username, self.password),
                    headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                    data=json.dumps(body),
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                raise OracleApiError(
                    f'Oracle API request outcome is unknown: {e}',
                    outcome_unknown=True,
                ) from e

        response = post_lead(payload)

        # Linking by party identifier is what actually attaches the lead to the
        # account and contact Oracle already has. If this tenant's leads
        # resource will not take one, that is a 400 rejecting the whole lead
        # over the link -- so it is retried once in the old name form, which at
        # least carries the right names onto a lead that exists. As with the
        # website field below, only a 400/422 naming the attribute qualifies,
        # which means Oracle created nothing and the retry cannot duplicate.
        rejected_links = [
            field for field in LINK_BY_ID_FIELDS
            if field in payload and field.lower() in (response.text or '').lower()
        ]
        if rejected_links and response.status_code in (400, 422):
            logger.warning(
                'Oracle rejected %s ("%s"); resubmitting with the account/contact '
                'named instead. The lead will land, but may not be attached to the '
                'existing records -- check it in Oracle.',
                ', '.join(rejected_links), (response.text or '')[:200],
            )
            warnings.append(
                f'Oracle rejected the account/contact link ({", ".join(rejected_links)}); '
                'lead submitted with the names only, which may leave it unattached.'
            )
            relink_payload_by_name(payload, existing)
            response = post_lead(payload)

        # The website attribute is the one field here whose writability could
        # not be confirmed from Oracle's metadata (leads/describe doesn't list
        # the custom *_c fields). If it turns out to be read-only on create,
        # Oracle rejects the ENTIRE lead over one optional field -- so that
        # specific rejection is retried once without it, and the lead still
        # lands. Only a 400/422 naming the field qualifies, which means Oracle
        # created nothing and a retry cannot duplicate.
        if (self.website_field
                and response.status_code in (400, 422)
                and self.website_field in payload
                and self.website_field.lower() in (response.text or '').lower()):
            logger.warning(
                'Oracle rejected %s ("%s"); resubmitting without the website and '
                'leaving it unset. Clear oracle.website_field in config.json to '
                'stop sending it.',
                self.website_field, (response.text or '')[:200],
            )
            warnings.append(
                f'Oracle rejected the website field ({self.website_field}); '
                'lead submitted without it.'
            )
            payload.pop(self.website_field)
            response = post_lead(payload)

        if response.status_code >= 400:
            raise OracleApiError(
                f'Oracle API rejected lead ({response.status_code}): {response.text}',
                status_code=response.status_code,
                response_text=response.text,
            )

        try:
            data = response.json()
        except ValueError as e:
            raise OracleApiError(
                'Oracle API returned a non-JSON success response; creation outcome is unknown',
                outcome_unknown=True,
            ) from e

        oracle_id = data.get('LeadId')
        lead_number = data.get('LeadNumber')
        if not oracle_id and not lead_number:
            raise OracleApiError(
                'Oracle API success response did not include LeadId or LeadNumber; '
                'creation outcome is unknown', outcome_unknown=True,
            )

        return {
            'success': True,
            'project_name': project_name,
            'oracle_record_id': str(oracle_id or lead_number),
            'oracle_lead_number': lead_number,
            'field_warnings': warnings,
            # Preserve the exact final request (including any field removed
            # after a tenant validation error) for the lead lifetime page.
            'payload': payload,
            'response': data,
        }

    def _attachment_urls(self, oracle_lead_id, oracle_response=None):
        """Ordered list of candidate attachment child-resource URLs to try.

        Oracle's REST responses carry a 'links' array naming every child
        resource that is actually enabled on this tenant, so a link whose name
        mentions "attachment" is authoritative -- it already has the right
        child-resource name AND the right leadsUniqID baked into the href,
        which matters because Oracle documents that key as an opaque hash
        rather than something a client should construct. Discovery first,
        constructed URLs only as a fallback.
        """
        urls = []
        if isinstance(oracle_response, dict):
            for link in oracle_response.get('links') or []:
                if not isinstance(link, dict):
                    continue
                name = (link.get('name') or '').lower()
                rel = (link.get('rel') or '').lower()
                href = link.get('href')
                if href and rel == 'child' and 'attachment' in name:
                    urls.append(href)

        if oracle_lead_id:
            urls.extend(
                f'{self.leads_url}/{oracle_lead_id}/child/{name}'
                for name in ATTACHMENT_CHILD_NAMES
            )

        deduped = []
        for url in urls:
            if url not in deduped:
                deduped.append(url)
        return deduped

    def upload_attachment(self, oracle_lead_id, file_path, file_name=None,
                          oracle_response=None, title=None):
        """Attach a file to an existing Oracle lead's Attachments section.

        Best-effort: returns True on success, False on failure. Never raises --
        by the time this runs the lead already exists in Oracle, so anything
        that escaped here would send an already-created lead back through the
        retry path and duplicate it.

        oracle_response is the raw JSON Oracle returned when the lead was
        created (or found by the dedup check); see _attachment_urls().
        """
        path = Path(file_path) if file_path else None
        if not path or not path.exists():
            logger.warning('Attachment file does not exist: %s', file_path)
            return False

        file_name = file_name or path.name
        try:
            encoded = base64.b64encode(path.read_bytes()).decode('ascii')
        except OSError as e:
            logger.warning('Could not read attachment %s: %s', file_path, e)
            return False

        payload = {
            'DatatypeCode': 'FILE',
            'FileName': file_name,
            # Oracle populates the stored file from UploadedFileName; FileName
            # alone leaves some tenants showing an untitled attachment.
            'UploadedFileName': file_name,
            'UploadedFileContentType': _content_type(path),
            # What the Attachments table actually displays as the row label.
            'Title': title or path.stem,
            'FileContents': encoded,
        }

        urls = self._attachment_urls(oracle_lead_id, oracle_response)
        if not urls:
            logger.warning('No attachment URL could be determined for lead %s', oracle_lead_id)
            return False

        last_error = None
        for url in urls:
            for category in self.attachment_categories:
                body = dict(payload, CategoryName=category)
                try:
                    resp = self.session.post(
                        url,
                        auth=(self.username, self.password),
                        headers={
                            'Content-Type': 'application/json',
                            'Accept': 'application/json',
                        },
                        data=json.dumps(body),
                        timeout=self.timeout,
                    )
                except requests.RequestException as e:
                    # Transport failure says nothing about whether the URL was
                    # right, so fall through to the next candidate rather than
                    # giving up on the whole upload.
                    last_error = f'{url}: {e}'
                    logger.warning('Attachment upload request failed (%s): %s', url, e)
                    break

                if resp.status_code < 400:
                    logger.info(
                        'Attached %s to Oracle lead %s (%s, category %s)',
                        file_name, oracle_lead_id, url, category,
                    )
                    return True

                detail = (resp.text or '')[:500]
                last_error = f'{url} -> {resp.status_code}: {detail}'
                if resp.status_code == 404:
                    # Wrong child-resource name for this tenant; the category
                    # is irrelevant, move on to the next URL.
                    logger.info('Attachment endpoint 404, trying next: %s', url)
                    break
                if _is_category_error(resp.status_code, detail):
                    logger.info(
                        'Oracle rejected attachment category %r; trying next category.',
                        category,
                    )
                    continue
                logger.warning(
                    'Oracle attachment upload failed (%s): %s', resp.status_code, detail,
                )
                return False

        logger.warning(
            'Could not attach %s to Oracle lead %s. Tried %s. Last error: %s',
            file_name, oracle_lead_id, urls, last_error,
        )
        return False
