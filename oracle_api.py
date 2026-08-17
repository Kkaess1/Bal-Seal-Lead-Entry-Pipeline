"""
Oracle Sales Cloud REST submitter.

The only path by which reviewed leads reach Oracle. It deliberately keeps the
public shape small: build a payload, submit it, and return the Oracle
identifiers needed for audit/retry.
"""

import base64
import json
import logging
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

# How to express PrimaryContactCountry. 'code' sends the ISO alpha-2 territory
# code (US, DE), which is what this tenant has accepted for US/CA to date;
# 'name' sends Oracle's dropdown label ('Germany') instead. Overridable via
# config.oracle.country_format so a tenant that rejects codes is a config
# change rather than a code change.
COUNTRY_FORMATS = ('code', 'name')

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


def build_lead_payload(lead, trade_show_name, source_label='Tradeshow',
                       lead_source='Website', campaign_name=None,
                       market_map=None, existing=None, country_format='code',
                       website_field=None):
    """
    Convert the pipeline's lead shape into Oracle's REST lead payload.

    `existing` is the result of OracleLeadApiClient.resolve_existing_party():
    when it names an account or contact already in Oracle, the payload points
    at that record with EnableCreateAccount/EnableCreateContact set to 'N'
    instead of creating a fresh one.

    `website_field` names the attribute behind the Website box on this
    tenant's lead layout (config.json oracle.website_field). It is configured
    rather than assumed, for the same reason the party IDs above are not sent:
    an attribute this tenant doesn't recognize comes back "not a valid field"
    as a 400 that rejects the whole lead, so a guess here would cost far more
    than the field is worth. Run oracle_field_probe.py once to read the real
    name off the tenant. Unset simply means no website is sent.

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
    # Oracle create them. This tenant exposes no CustomerPartyId /
    # PrimaryContactPartyId attribute on leads (all four ID forms return
    # "not a valid field"), so linking is done by sending the record's exact
    # stored name with EnableCreate* = 'N' and letting Oracle resolve it.
    existing_account = _clean(existing.get('account_name'))
    existing_contact = _clean(existing.get('contact_name'))

    if existing_account:
        payload['CustomerPartyName'] = existing_account
        payload['EnableCreateAccount'] = 'N'
    elif organization:
        payload['CustomerPartyName'] = organization

    if existing_contact:
        payload['PrimaryContactPartyName'] = existing_contact
        payload['EnableCreateContact'] = 'N'
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
        # This field exists on create in Oracle's public schema. If this tenant
        # rejects owner-by-name, the 400 will surface and the lead will retry
        # after we map OwnerPartyNumber/OwnerId.
        payload['OwnerPartyName'] = assigned_tm

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
        customer. Prefix hits are returned purely so they can be shown to a
        human.
        """
        organization = (organization or '').strip()
        if not organization:
            return [], []
        name = organization.upper()
        fields = 'PartyId,PartyNumber,OrganizationName'
        exact = self._query(
            self.accounts_url, f"OrganizationName='{_q_escape(name)}'", fields, limit=2,
        )
        prefix = self._query(
            self.accounts_url, f"OrganizationName LIKE '{_q_escape(name)}%'", fields,
        )
        return exact, prefix

    def resolve_existing_party(self, lead):
        """
        Decide whether this lead belongs to an account/contact Oracle already
        has. Returns a dict consumed by build_lead_payload():

            {'account_name', 'contact_name', 'match_basis', 'warnings'}

        Confidence tiers, highest first:
          1. exact contact email  -> link contact AND its account
          2. exactly one account of that exact name -> link the account only
          3. anything else -> create new, and warn with the candidates
        """
        result = {'account_name': None, 'contact_name': None,
                  'match_basis': 'none', 'warnings': []}

        contact = self.find_contact_by_email(lead.get('email'))
        if contact:
            result['contact_name'] = contact.get('ContactName')
            result['account_name'] = contact.get('AccountName')
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
            result['match_basis'] = 'account_name_exact'
            result['warnings'].append(
                f"Linked to existing Oracle account {account.get('OrganizationName')!r}"
                f" (#{account.get('PartyNumber')}); no new account created."
            )
            return result

        if prefix:
            names = ', '.join(
                repr(a.get('OrganizationName')) for a in prefix[:MAX_NAMED_CANDIDATES]
            )
            more = '' if len(prefix) <= MAX_NAMED_CANDIDATES else f' (+{len(prefix) - MAX_NAMED_CANDIDATES} more)'
            result['match_basis'] = 'ambiguous'
            result['warnings'].append(
                f'Company {organization!r} resembles {len(prefix)} existing Oracle '
                f'account(s) but none matched exactly, so a new account was created. '
                f'Candidates: {names}{more}. Merge in Oracle if this is a duplicate.'
            )
        return result

    def submit_lead(self, lead, trade_show_name, source_label='Tradeshow'):
        # Look up the company/contact before creating anything. A lookup outage
        # is not evidence that the party does not exist, so it is allowed to
        # stop this attempt and be retried safely later.
        existing = self.resolve_existing_party(lead)

        payload, project_name, warnings = build_lead_payload(
            lead, trade_show_name, source_label=source_label,
            lead_source=self.lead_source,
            campaign_name=self.campaign_name,
            existing=existing,
            country_format=self.country_format,
            website_field=self.website_field,
        )
        logger.info(
            'Submitting lead via Oracle REST API: %s (account/contact match: %s)',
            project_name, (existing or {}).get('match_basis', 'none'),
        )
        
        # --- Pre-submission Dedup Check against Oracle ---
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

        try:
            response = self.session.post(
                self.leads_url,
                auth=(self.username, self.password),
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                data=json.dumps(payload),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise OracleApiError(
                f'Oracle API request outcome is unknown: {e}',
                outcome_unknown=True,
            ) from e

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
            try:
                response = self.session.post(
                    self.leads_url,
                    auth=(self.username, self.password),
                    headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                    data=json.dumps(payload),
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                raise OracleApiError(
                    f'Oracle API request outcome is unknown: {e}',
                    outcome_unknown=True,
                ) from e

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
