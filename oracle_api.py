"""
Oracle Sales Cloud REST submitter.

This is the API equivalent of oracle_automation.OracleLeadAutomator for the
reviewed lead queue. It deliberately keeps the public shape small: build a
payload, submit it, and return the Oracle identifiers needed for audit/retry.
"""

import json
import logging

import requests

from phone_parser import parse_phone

logger = logging.getLogger(__name__)

API_VERSION = '11.13.18.05'

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

COUNTRY_CODES = {
    'CANADA': 'CA',
    'CA': 'CA',
    'UNITED STATES': 'US',
    'UNITED STATES OF AMERICA': 'US',
    'USA': 'US',
    'U.S.A.': 'US',
    'US': 'US',
}

# Oracle's custom LOV stores a code/value in IndustryDescriptionBSE_c and
# exposes the visible Create Lead dropdown label via
# IndustryDescriptionBSE_cMeaning. Confirmed live through read-only API samples
# from this tenant on 2026-07-27.
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
    def __init__(self, message, status_code=None, response_text=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def build_project_name(lead, trade_show_name, source_label='Tradeshow'):
    org = (lead.get('organization') or '').upper().strip()
    first = (lead.get('first_name') or '').strip()
    last = (lead.get('last_name') or '').strip()
    name = f'{first} {last}'.strip()
    return ' - '.join(
        part for part in (org, name, source_label, trade_show_name)
        if part and str(part).strip()
    )


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _country_code(value):
    country = (value or '').strip().upper()
    if not country:
        return 'US'
    return COUNTRY_CODES.get(country, value)


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
                       market_map=None):
    """
    Convert the pipeline's lead shape into Oracle's REST lead payload.

    Returns (payload, project_name, warnings). Warnings are non-fatal mapping
    notes that should be stored with the lead for review.
    """
    market_map = market_map or MARKET_TO_API_VALUE
    warnings = []
    project_name = build_project_name(lead, trade_show_name, source_label)

    lead_source = (lead_source or 'Website').strip()
    if lead_source not in LEAD_SOURCE_VALUES:
        warnings.append(f'Unverified lead source "{lead_source}"; sending as provided.')

    first = _clean(lead.get('first_name'))
    last = _clean(lead.get('last_name'))
    full_name = ' '.join(part for part in (first, last) if part)
    organization = _clean(lead.get('organization'))
    country = _country_code(lead.get('country'))

    payload = {
        'Name': project_name,
        'OpportunityName': project_name,
        'LeadSourceDescriptionBSE_c': lead_source,
        'Description': _clean(lead.get('lead_details')) or _clean(lead.get('notes_body')) or 'Lead imported by Lead Input Pipeline.',
        'PrimaryContactCountry': country,
        'EnableCreateAccount': 'Y',
        'EnableCreateContact': 'Y',
    }

    if organization:
        payload['CustomerPartyName'] = organization
    if full_name:
        payload['PrimaryContactPartyName'] = full_name
    _put(payload, 'PrimaryContactPersonFirstName', first)
    _put(payload, 'PrimaryContactPersonLastName', last)
    _put(payload, 'JobTitle', lead.get('title'))
    _put(payload, 'PrimaryContactEmailAddress', lead.get('email'))

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
    if country == 'US':
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
        self.username = username
        self.password = password
        self.session = session or requests.Session()

    @property
    def leads_url(self):
        return f'{self.base_url}/crmRestApi/resources/{API_VERSION}/leads'

    def submit_lead(self, lead, trade_show_name, source_label='Tradeshow'):
        payload, project_name, warnings = build_lead_payload(
            lead, trade_show_name, source_label=source_label,
            lead_source=self.lead_source,
            campaign_name=self.campaign_name,
        )
        logger.info('Submitting lead via Oracle REST API: %s', project_name)
        
        # --- Pre-submission Dedup Check against Oracle ---
        try:
            email = (lead.get('email') or '').strip()
            # Construct query to check by exact Name or Email in Oracle
            queries = [f"Name='{project_name}'"]
            if email:
                queries.append(f"PrimaryContactEmailAddress='{email}'")
            q_str = ' OR '.join(queries)
            
            check_resp = self.session.get(
                f"{self.leads_url}?q={q_str}",
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
        except Exception as check_err:
            logger.warning('Pre-submission Oracle dedup check warning: %s', check_err)

        try:
            response = self.session.post(
                self.leads_url,
                auth=(self.username, self.password),
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                data=json.dumps(payload),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise OracleApiError(f'Oracle API request failed: {e}') from e

        if response.status_code >= 400:
            raise OracleApiError(
                f'Oracle API rejected lead ({response.status_code}): {response.text}',
                status_code=response.status_code,
                response_text=response.text,
            )

        try:
            data = response.json()
        except ValueError as e:
            raise OracleApiError('Oracle API returned non-JSON success response') from e

        oracle_id = data.get('LeadId')
        lead_number = data.get('LeadNumber')
        if not oracle_id and not lead_number:
            raise OracleApiError('Oracle API success response did not include LeadId or LeadNumber')

        return {
            'success': True,
            'project_name': project_name,
            'oracle_record_id': str(oracle_id or lead_number),
            'oracle_lead_number': lead_number,
            'field_warnings': warnings,
            'response': data,
        }
