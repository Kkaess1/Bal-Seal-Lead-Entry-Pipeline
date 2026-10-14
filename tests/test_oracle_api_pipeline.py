"""
Oracle REST API pipeline tests.

Covers the full api-submission path introduced in oracle_api.py and the
corresponding _run_api_submit() logic in pipeline_submit.py.  No network
traffic: the OracleLeadApiClient.session is always replaced with a
MagicMock so these run fully offline.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import db
import oracle_api
import pipeline_submit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _approved_lead(db_path, extra=None):
    lead = {
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'organization': 'Analytical Engines',
        'email': 'ada@example.com',
        'phone': '202-555-0199',
        'market': 'INDUSTRIAL',
        'rank': 'Hot',
        'country': 'United States',
        'assigned_tm': 'Bill Rost',
        **(extra or {}),
    }
    conn = db.get_connection(db_path)
    lead_id = db.insert_lead(lead, 'email_ai_extract', status='approved', conn=conn)
    conn.commit()
    conn.close()
    return lead_id


def _mock_client(status_code=201, json_body=None, exc=None):
    config = {
        'base_url': 'https://fake.oracle.example',
        'lead_source': 'Website',
        'api_timeout_seconds': 10,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if json_body is not None:
        mock_resp.json.return_value = json_body
    mock_session = MagicMock()
    empty_lookup = MagicMock(status_code=200, text='')
    empty_lookup.json.return_value = {'items': []}
    mock_session.get.return_value = empty_lookup
    if exc is not None:
        mock_session.post.side_effect = exc
    else:
        mock_session.post.return_value = mock_resp
    return oracle_api.OracleLeadApiClient(
        config, username='u', password='p', session=mock_session
    )


# ---------------------------------------------------------------------------
# build_lead_payload()
# ---------------------------------------------------------------------------

class BuildLeadPayloadTests(unittest.TestCase):

    def _build(self, lead, trade_show='Website/Email', source='Inquiry',
               lead_source='Website', campaign=None, country_format='code'):
        return oracle_api.build_lead_payload(
            lead, trade_show, source_label=source,
            lead_source=lead_source, campaign_name=campaign,
            country_format=country_format,
        )

    def test_rank_hot_any_case_is_uppercased(self):
        for raw in ('hot', 'Hot', 'HOT'):
            payload, _, _ = self._build({'rank': raw, 'market': 'INDUSTRIAL'})
            self.assertEqual(payload['Rank'], 'HOT', f'failed for rank={raw!r}')

    def test_rank_warm_any_case_is_uppercased(self):
        for raw in ('warm', 'Warm', 'WARM'):
            payload, _, _ = self._build({'rank': raw, 'market': 'INDUSTRIAL'})
            self.assertEqual(payload['Rank'], 'WARM', f'failed for rank={raw!r}')

    def test_rank_cold_omitted_with_warning(self):
        payload, _, warnings = self._build({'rank': 'Cold', 'market': 'INDUSTRIAL'})
        self.assertNotIn('Rank', payload)
        self.assertTrue(any('Cold' in w for w in warnings))

    def test_rank_invalid_omitted_with_warning(self):
        payload, _, warnings = self._build({'rank': 'Lukewarm', 'market': 'INDUSTRIAL'})
        self.assertNotIn('Rank', payload)
        self.assertTrue(any('Lukewarm' in w for w in warnings))

    def test_rank_absent_omitted_silently(self):
        payload, _, warnings = self._build({'market': 'INDUSTRIAL'})
        self.assertNotIn('Rank', payload)
        self.assertFalse(any('rank' in w.lower() for w in warnings))

    def test_empty_org_and_name_project_name_still_valid(self):
        payload, project_name, _ = self._build(
            {'market': 'INDUSTRIAL'}, source='', trade_show='TradeX'
        )
        self.assertIn('TradeX', project_name)
        self.assertEqual(payload['Name'], project_name)

    def test_full_project_name_format(self):
        lead = {
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'organization': 'ACME Corp',
            'market': 'INDUSTRIAL',
        }
        _, project_name, _ = self._build(lead, trade_show='IMTS 2026', source='Tradeshow')
        self.assertEqual(project_name, 'ACME CORP - Ada Lovelace - Tradeshow - IMTS 2026')

    def test_us_phone_populates_all_phone_fields(self):
        payload, _, _ = self._build({'phone': '(202) 555-0199', 'market': 'INDUSTRIAL'})
        self.assertEqual(payload['PhoneAreaCodeBSE_c'], '202')
        self.assertEqual(payload['PhoneNumberBSE_c'], '555-0199')
        self.assertIn('PrimaryPhoneAreaCode', payload)
        self.assertIn('WorkPhoneAreaCode', payload)

    def test_missing_phone_omits_all_phone_fields(self):
        payload, _, _ = self._build({'market': 'INDUSTRIAL'})
        for key in ('PrimaryPhoneAreaCode', 'PrimaryPhoneNumber',
                    'PhoneAreaCodeBSE_c', 'PhoneNumberBSE_c'):
            self.assertNotIn(key, payload)

    def test_us_country_uses_state(self):
        lead = {'country': 'United States', 'state': 'TX', 'market': 'INDUSTRIAL'}
        payload, _, _ = self._build(lead)
        self.assertIn('PrimaryContactState', payload)
        self.assertNotIn('PrimaryContactProvince', payload)

    def test_non_us_country_uses_province(self):
        lead = {'country': 'Canada', 'state': 'ONTARIO', 'market': 'INDUSTRIAL'}
        payload, _, _ = self._build(lead)
        self.assertIn('PrimaryContactProvince', payload)
        self.assertNotIn('PrimaryContactState', payload)
        self.assertEqual(payload['PrimaryContactCountry'], 'CA')

    def test_international_country_maps_to_iso_code(self):
        for raw, expected in (('Germany', 'DE'), ('Japan', 'JP'), ('Brazil', 'BR'),
                              ('United Kingdom', 'GB'), ('Mexico', 'MX')):
            payload, _, warnings = self._build({'country': raw, 'market': 'INDUSTRIAL'})
            self.assertEqual(payload['PrimaryContactCountry'], expected, f'for {raw!r}')
            self.assertFalse(warnings, f'unexpected warning for {raw!r}: {warnings}')

    def test_country_aliases_and_punctuation_resolve(self):
        for raw, expected in (('U.S.A.', 'US'), ('UK', 'GB'), ('England', 'GB'),
                              ('Deutschland', 'DE'), ('Holland', 'NL'),
                              ('South Korea', 'KR'), ("Cote d'Ivoire", 'CI'),
                              ('Turkiye', 'TR'), ('DE', 'DE')):
            payload, _, _ = self._build({'country': raw, 'market': 'INDUSTRIAL'})
            self.assertEqual(payload['PrimaryContactCountry'], expected, f'for {raw!r}')

    def test_unknown_country_omitted_with_warning(self):
        lead = {'country': 'Narnia', 'state': 'Lantern Waste', 'market': 'INDUSTRIAL'}
        payload, _, warnings = self._build(lead)
        self.assertNotIn('PrimaryContactCountry', payload)
        self.assertTrue(any('Narnia' in w for w in warnings), warnings)
        # Not a US alias, so its region belongs in Province, not State.
        self.assertEqual(payload['PrimaryContactProvince'], 'Lantern Waste')
        self.assertNotIn('PrimaryContactState', payload)

    def test_missing_country_with_us_state_defaults_to_us_silently(self):
        payload, _, warnings = self._build({'state': 'TX', 'market': 'INDUSTRIAL'})
        self.assertEqual(payload['PrimaryContactCountry'], 'US')
        self.assertEqual(payload['PrimaryContactState'], 'TX')
        self.assertFalse(any('country' in w.lower() for w in warnings), warnings)

    def test_missing_country_without_us_state_warns(self):
        payload, _, warnings = self._build({'market': 'INDUSTRIAL'})
        self.assertEqual(payload['PrimaryContactCountry'], 'US')
        self.assertTrue(any('defaulted to US' in w for w in warnings), warnings)

    def test_country_format_name_sends_display_label(self):
        payload, _, _ = self._build(
            {'country': 'Germany', 'market': 'INDUSTRIAL'}, country_format='name'
        )
        self.assertEqual(payload['PrimaryContactCountry'], 'Germany')

    def test_country_format_name_still_splits_state_and_province(self):
        us, _, _ = self._build(
            {'country': 'USA', 'state': 'TX', 'market': 'INDUSTRIAL'},
            country_format='name',
        )
        self.assertEqual(us['PrimaryContactState'], 'TX')
        intl, _, _ = self._build(
            {'country': 'Canada', 'state': 'ON', 'market': 'INDUSTRIAL'},
            country_format='name',
        )
        self.assertEqual(intl['PrimaryContactProvince'], 'ON')

    def test_client_defaults_country_format_to_code(self):
        client = _mock_client()
        self.assertEqual(client.country_format, 'code')

    def test_client_rejects_unknown_country_format(self):
        config = {'base_url': 'https://fake.oracle.example', 'country_format': 'iso3'}
        client = oracle_api.OracleLeadApiClient(config, username='u', password='p')
        self.assertEqual(client.country_format, 'code')

    def test_assigned_tm_sets_owner_party_name(self):
        lead = {'assigned_tm': 'John Pan', 'market': 'INDUSTRIAL'}
        payload, _, _ = self._build(lead)
        self.assertEqual(payload['OwnerPartyName'], 'John Pan')

    def test_no_assigned_tm_omits_owner(self):
        payload, _, _ = self._build({'market': 'INDUSTRIAL'})
        self.assertNotIn('OwnerPartyName', payload)

    def test_no_market_key_defaults_to_industrial(self):
        payload, _, warnings = self._build({})
        self.assertEqual(payload['IndustryDescriptionBSE_c'], 'NON-TARGETED SEALS/SPRINGS')
        # This lead has no country either, which warns on its own -- assert only
        # that the market defaulted silently.
        self.assertFalse([w for w in warnings if 'market' in w.lower()])

    def test_campaign_name_omitted_from_payload(self):
        lead = {'market': 'INDUSTRIAL', 'campaign': 'Lead Campaign'}
        payload, _, _ = self._build(lead, campaign='Fallback Campaign')
        self.assertNotIn('CampaignName', payload)

    def test_unverified_lead_source_sends_value_with_warning(self):
        payload, _, warnings = self._build(
            {'market': 'INDUSTRIAL'}, lead_source='NonExistent'
        )
        self.assertEqual(payload['LeadSourceDescriptionBSE_c'], 'NonExistent')
        self.assertTrue(any('NonExistent' in w for w in warnings))

    def test_whitespace_only_fields_are_excluded(self):
        lead = {'first_name': '   ', 'last_name': '', 'title': None, 'market': 'INDUSTRIAL'}
        payload, _, _ = self._build(lead)
        self.assertNotIn('PrimaryContactPersonFirstName', payload)
        self.assertNotIn('PrimaryContactPersonLastName', payload)
        self.assertNotIn('JobTitle', payload)

    def test_enable_create_account_and_contact_always_present(self):
        payload, _, _ = self._build({'market': 'INDUSTRIAL'})
        self.assertEqual(payload['EnableCreateAccount'], 'Y')
        self.assertEqual(payload['EnableCreateContact'], 'Y')

    def test_description_falls_back_to_notes_body(self):
        lead = {'notes_body': 'Some notes', 'market': 'INDUSTRIAL'}
        payload, _, _ = self._build(lead)
        self.assertEqual(payload['Description'], 'Some notes')

    def test_description_falls_back_to_default_string(self):
        payload, _, _ = self._build({'market': 'INDUSTRIAL'})
        self.assertIn('Lead imported', payload['Description'])


# ---------------------------------------------------------------------------
# OracleLeadApiClient.submit_lead
# ---------------------------------------------------------------------------

class OracleLeadApiClientTests(unittest.TestCase):

    def _lead(self):
        return {
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'organization': 'Analytical Engines',
            'email': 'ada@example.com',
            'market': 'INDUSTRIAL',
        }

    def test_reviewer_selected_account_outranks_contact_and_name_inference(self):
        client = _mock_client()
        lead = dict(
            self._lead(),
            oracle_account_name='ANALYTICAL ENGINES HOLDINGS',
            oracle_account_party_id='100000055846517',
            oracle_account_party_number='11620',
        )

        existing = client.resolve_existing_party(lead)

        self.assertEqual(existing['match_basis'], 'account_selected')
        self.assertEqual(existing['account_party_id'], 100000055846517)
        self.assertEqual(existing['account_name'], 'ANALYTICAL ENGINES HOLDINGS')

    def test_matching_contact_is_retained_with_a_reviewer_selected_account(self):
        client = _mock_client()
        contact_response = MagicMock(status_code=200, text='')
        contact_response.json.return_value = {'items': [{
            'PartyNumber': '15001', 'ContactName': 'ADA LOVELACE',
            'AccountPartyId': 100000055846517,
        }]}
        client.session.get.return_value = contact_response

        existing = client.resolve_existing_party({
            'email': 'ada@example.com',
            'oracle_account_name': 'ANALYTICAL ENGINES',
            'oracle_account_party_id': '100000055846517',
        })

        self.assertEqual(existing['match_basis'], 'account_selected_contact_email')
        self.assertEqual(existing['contact_party_number'], '15001')

    def test_account_list_follows_every_oracle_page(self):
        first = MagicMock(status_code=200, text='')
        first.json.return_value = {
            'items': [{'PartyId': 1, 'PartyNumber': 'A1', 'OrganizationName': 'ACME'}],
            'hasMore': True,
        }
        second = MagicMock(status_code=200, text='')
        second.json.return_value = {
            'items': [{'PartyId': 2, 'PartyNumber': 'B2', 'OrganizationName': 'BETA'}],
            'hasMore': False,
        }
        session = MagicMock()
        session.get.side_effect = [first, second]
        client = oracle_api.OracleLeadApiClient(
            {'base_url': 'https://fake.oracle.example', 'api_timeout_seconds': 10},
            username='u', password='p', session=session,
        )

        accounts = client.list_accounts(page_size=1)

        self.assertEqual([account['PartyId'] for account in accounts], [1, 2])
        self.assertEqual(session.get.call_args_list[0].kwargs['params']['offset'], '0')
        self.assertEqual(session.get.call_args_list[1].kwargs['params']['offset'], '1')

    def test_every_account_is_returned_with_the_closest_first(self):
        accounts = [
            {'PartyId': 3, 'OrganizationName': 'ZETA FLUID SYSTEMS'},
            {'PartyId': 1, 'OrganizationName': 'ACME CORPORATION'},
            {'PartyId': 2, 'OrganizationName': 'ACME AEROSPACE'},
        ]

        ranked = oracle_api.rank_company_accounts('Acme Corp', accounts)

        self.assertEqual([item['PartyId'] for item in ranked], [1, 2, 3])
        self.assertEqual(len(ranked), len(accounts))
        self.assertEqual(ranked[0]['similarity'], 1.0)

    def test_same_named_but_distinct_oracle_accounts_are_not_collapsed(self):
        accounts = [
            {'PartyId': 1, 'OrganizationName': 'ACME'},
            {'PartyId': 2, 'OrganizationName': 'ACME'},
            {'PartyId': 1, 'OrganizationName': 'ACME'},
        ]

        ranked = oracle_api.rank_company_accounts('Acme', accounts)

        self.assertEqual([item['PartyId'] for item in ranked], [1, 2])

    def test_happy_path_returns_success_dict(self):
        client = _mock_client(status_code=201, json_body={'LeadId': 99, 'LeadNumber': 'LN-0042'})
        result = client.submit_lead(self._lead(), 'Website/Email', source_label='Inquiry')
        self.assertTrue(result['success'])
        self.assertEqual(result['oracle_record_id'], '99')
        self.assertEqual(result['oracle_lead_number'], 'LN-0042')
        self.assertIsInstance(result['field_warnings'], list)

    @patch('oracle_api.description_compactor.compact_description')
    def test_overlong_description_is_compacted_before_post(self, compact):
        compact.return_value = ('Important specifications retained.', 'ai')
        client = _mock_client(
            status_code=201,
            json_body={'LeadId': 99, 'LeadNumber': 'LN-0042'},
        )

        result = client.submit_lead(
            dict(self._lead(), lead_details='x' * 2001), 'Website/Email',
            source_label='Inquiry',
        )

        compact.assert_called_once_with('x' * 2001)
        posted = json.loads(client.session.post.call_args.kwargs['data'])
        self.assertEqual(posted['Description'], 'Important specifications retained.')
        self.assertTrue(any('2001' in warning and 'AI' in warning
                            for warning in result['field_warnings']))

    def _website_client(self, first_status, first_text, second_status=201):
        """Client whose POST fails once then succeeds, for the website retry."""
        config = {
            'base_url': 'https://fake.oracle.example',
            'lead_source': 'Website',
            'api_timeout_seconds': 10,
            'website_field': 'WebsiteBSE_c',
        }
        rejected = MagicMock(status_code=first_status, text=first_text)
        accepted = MagicMock(status_code=second_status)
        accepted.json.return_value = {'LeadId': 99, 'LeadNumber': 'LN-0042'}
        session = MagicMock()
        empty_lookup = MagicMock(status_code=200, text='')
        empty_lookup.json.return_value = {'items': []}
        session.get.return_value = empty_lookup
        session.post.side_effect = [rejected, accepted]
        return oracle_api.OracleLeadApiClient(
            config, username='u', password='p', session=session
        ), session

    def test_website_rejection_retries_without_it_and_still_lands_the_lead(self):
        client, session = self._website_client(
            400, 'WebsiteBSE_c is not a valid field for this operation.'
        )
        lead = dict(self._lead(), website='https://dcfusionllc.com')
        result = client.submit_lead(lead, 'Website/Email', source_label='Inquiry')

        self.assertTrue(result['success'])
        self.assertEqual(result['oracle_record_id'], '99')
        self.assertEqual(session.post.call_count, 2)
        first, second = session.post.call_args_list
        self.assertIn('WebsiteBSE_c', json.loads(first.kwargs['data']))
        self.assertNotIn('WebsiteBSE_c', json.loads(second.kwargs['data']))
        # Everything else has to survive the retry, not just the website.
        self.assertEqual(
            json.loads(second.kwargs['data'])['PrimaryContactEmailAddress'],
            'ada@example.com',
        )
        self.assertTrue(any('website field' in w for w in result['field_warnings']))

    def _linking_client(self, first_status, first_text):
        """Client whose POST fails once then succeeds, for the link retry.

        Its GETs answer the contact lookup with a match, so submit_lead builds
        a payload that links by identifier.
        """
        contact = MagicMock(status_code=200, text='')
        contact.json.return_value = {'items': [{
            'PartyId': 1, 'PartyNumber': '15001', 'ContactName': 'ADA LOVELACE',
            'EmailAddress': 'ada@example.com', 'AccountName': 'ANALYTICAL ENGINES',
            'AccountPartyId': 100000055846517, 'AccountPartyNumber': '11620',
        }]}
        empty = MagicMock(status_code=200, text='')
        empty.json.return_value = {'items': []}
        rejected = MagicMock(status_code=first_status, text=first_text)
        accepted = MagicMock(status_code=201)
        accepted.json.return_value = {'LeadId': 99, 'LeadNumber': 'LN-0042'}
        session = MagicMock()
        session.get.side_effect = [contact, empty]
        session.post.side_effect = [rejected, accepted]
        return oracle_api.OracleLeadApiClient(
            {'base_url': 'https://fake.oracle.example', 'lead_source': 'Website',
             'api_timeout_seconds': 10},
            username='u', password='p', session=session,
        ), session

    def test_a_matched_lead_is_sent_linked_by_identifier(self):
        client, session = self._linking_client(201, '')
        session.post.side_effect = None
        accepted = MagicMock(status_code=201)
        accepted.json.return_value = {'LeadId': 99, 'LeadNumber': 'LN-0042'}
        session.post.return_value = accepted

        client.submit_lead(self._lead(), 'Website/Email', source_label='Inquiry')

        posted = json.loads(session.post.call_args.kwargs['data'])
        self.assertEqual(posted['CustomerId'], 100000055846517)
        self.assertEqual(posted['ContactPartyNumber'], '15001')

    def test_a_rejected_link_retries_by_name_and_still_lands_the_lead(self):
        # If a tenant will not take the identifier, the lead must still reach
        # Oracle -- losing the link is bad, losing the lead is worse.
        client, session = self._linking_client(
            400, 'CustomerId is not a valid field for this operation.'
        )

        result = client.submit_lead(self._lead(), 'Website/Email', source_label='Inquiry')

        self.assertTrue(result['success'])
        self.assertEqual(session.post.call_count, 2)
        second = json.loads(session.post.call_args_list[1].kwargs['data'])
        self.assertNotIn('CustomerId', second)
        self.assertNotIn('ContactPartyNumber', second)
        self.assertEqual(second['CustomerPartyName'], 'ANALYTICAL ENGINES')
        self.assertEqual(second['PrimaryContactPartyName'], 'ADA LOVELACE')
        self.assertEqual(second['EnableCreateAccount'], 'N')

    def test_the_retry_says_the_lead_may_not_be_attached(self):
        # The name form is the one that quietly did nothing on this tenant, so
        # a lead that falls back to it must not look linked on the dashboard.
        client, _ = self._linking_client(
            400, 'CustomerId is not a valid field for this operation.'
        )

        result = client.submit_lead(self._lead(), 'Website/Email', source_label='Inquiry')

        self.assertTrue(any('may leave it unattached' in w
                            for w in result['field_warnings']))

    def test_a_400_naming_no_link_field_is_not_retried(self):
        client, session = self._linking_client(400, 'Rank is invalid.')

        with self.assertRaises(oracle_api.OracleApiError):
            client.submit_lead(self._lead(), 'Website/Email', source_label='Inquiry')

        self.assertEqual(session.post.call_count, 1)

    def test_unrelated_400_is_not_retried(self):
        client, session = self._website_client(400, 'Rank is invalid.')
        lead = dict(self._lead(), website='https://dcfusionllc.com')
        with self.assertRaises(oracle_api.OracleApiError):
            client.submit_lead(lead, 'Website/Email', source_label='Inquiry')
        self.assertEqual(session.post.call_count, 1)

    def test_http_400_raises_oracle_api_error_with_status_code(self):
        client = _mock_client(status_code=400)
        with self.assertRaises(oracle_api.OracleApiError) as ctx:
            client.submit_lead(self._lead(), 'test')
        self.assertEqual(ctx.exception.status_code, 400)

    def test_http_503_raises_oracle_api_error_with_status_code(self):
        client = _mock_client(status_code=503)
        with self.assertRaises(oracle_api.OracleApiError) as ctx:
            client.submit_lead(self._lead(), 'test')
        self.assertEqual(ctx.exception.status_code, 503)

    def test_network_error_wrapped_as_oracle_api_error(self):
        client = _mock_client(exc=requests.ConnectionError('refused'))
        with self.assertRaises(oracle_api.OracleApiError) as ctx:
            client.submit_lead(self._lead(), 'test')
        self.assertIsNone(ctx.exception.status_code)

    def test_success_without_lead_id_or_number_raises(self):
        client = _mock_client(status_code=200, json_body={})
        with self.assertRaises(oracle_api.OracleApiError):
            client.submit_lead(self._lead(), 'test')

    def test_non_json_success_response_raises(self):
        config = {'base_url': 'https://fake.oracle.example', 'lead_source': 'Website', 'api_timeout_seconds': 10}
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.side_effect = ValueError('not json')
        mock_session = MagicMock()
        empty_lookup = MagicMock(status_code=200, text='')
        empty_lookup.json.return_value = {'items': []}
        mock_session.get.return_value = empty_lookup
        mock_session.post.return_value = mock_resp
        client = oracle_api.OracleLeadApiClient(config, username='u', password='p', session=mock_session)
        with self.assertRaises(oracle_api.OracleApiError):
            client.submit_lead(self._lead(), 'test')

    def test_lead_number_only_still_succeeds(self):
        client = _mock_client(status_code=201, json_body={'LeadNumber': 'LN-1111'})
        result = client.submit_lead(self._lead(), 'test')
        self.assertTrue(result['success'])
        self.assertEqual(result['oracle_record_id'], 'LN-1111')

    def test_request_uses_basic_auth_and_json_headers(self):
        config = {'base_url': 'https://fake.oracle.example', 'lead_source': 'Website', 'api_timeout_seconds': 10}
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {'LeadId': 1, 'LeadNumber': 'LN-001'}
        mock_session = MagicMock()
        empty_lookup = MagicMock(status_code=200, text='')
        empty_lookup.json.return_value = {'items': []}
        mock_session.get.return_value = empty_lookup
        mock_session.post.return_value = mock_resp
        client = oracle_api.OracleLeadApiClient(config, username='myuser', password='mypass', session=mock_session)
        client.submit_lead(self._lead(), 'test')
        call_kwargs = mock_session.post.call_args
        self.assertEqual(call_kwargs.kwargs['auth'], ('myuser', 'mypass'))
        self.assertEqual(call_kwargs.kwargs['headers']['Content-Type'], 'application/json')

    def test_leads_url_uses_correct_api_version(self):
        config = {'base_url': 'https://cagc.fa.us1.oraclecloud.com', 'lead_source': 'Website', 'api_timeout_seconds': 60}
        client = oracle_api.OracleLeadApiClient(config, username='u', password='p')
        self.assertIn(oracle_api.API_VERSION, client.leads_url)
        self.assertTrue(client.leads_url.endswith('/leads'))


# ---------------------------------------------------------------------------
# _run_api_submit() database state transitions
# ---------------------------------------------------------------------------

class RunApiSubmitTests(unittest.TestCase):
    """Patches db.DB_PATH in setUp so all db operations in both test helpers
    and inside _run_api_submit() itself target the same temp database.
    Using explicit conn= on list_ready_for_submission left that connection
    open while _run_api_submit tried to claim the same rows, causing SQLite
    locking conflicts that silently skipped every lead."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = Path(self._tmpdir) / 'test.db'
        db.init_db(self._db_path)
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db_path
        # get_lead_email_attachment() writes a reconstructed email file when a
        # lead has no cached source email -- which is every lead here. Redirect
        # it so test runs don't drop files into the real email_cache/.
        self._original_cache_dir = db.EMAIL_CACHE_DIR
        db.EMAIL_CACHE_DIR = Path(self._tmpdir) / 'email_cache'

    def tearDown(self):
        # No table cleanup here on purpose. Each test gets its own throwaway
        # database from setUp, so there is nothing to clean -- and an
        # unqualified "DELETE FROM leads" in a teardown is one missed
        # monkeypatch away from emptying the real pipeline.db, which is
        # exactly what used to happen while db.get_connection() bound DB_PATH
        # as a default argument.
        db.DB_PATH = self._original_db_path
        db.EMAIL_CACHE_DIR = self._original_cache_dir

    def _insert_approved(self, extra=None):
        """Insert an approved lead using the patched DB_PATH (no explicit conn)."""
        lead = {
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'organization': 'Analytical Engines',
            'email': 'ada@example.com',
            'phone': '202-555-0199',
            'market': 'INDUSTRIAL',
            'rank': 'Hot',
            'country': 'United States',
            'assigned_tm': 'Bill Rost',
            **(extra or {}),
        }
        return db.insert_lead(lead, 'email_ai_extract', status='approved')

    def _get_lead(self, lead_id):
        return db.get_lead(lead_id)

    def _run(self, client):
        leads = db.list_ready_for_submission()
        return pipeline_submit._run_api_submit(client, leads)

    def test_success_marks_submitted_with_oracle_id(self):
        lead_id = self._insert_approved()
        client = _mock_client(status_code=201, json_body={'LeadId': 42, 'LeadNumber': 'LN-0042'})
        results = self._run(client)
        self.assertEqual(results['ok'], 1)
        self.assertEqual(results['fail'], 0)
        lead = self._get_lead(lead_id)
        self.assertEqual(lead['status'], 'submitted')
        self.assertEqual(lead['oracle_record_id'], '42')
        self.assertIsNotNone(lead['submitted_at'])

    def test_4xx_marks_lead_failed(self):
        lead_id = self._insert_approved()
        client = _mock_client(status_code=422)
        results = self._run(client)
        self.assertEqual(results['fail'], 1)
        lead = self._get_lead(lead_id)
        self.assertEqual(lead['status'], 'failed')

    def test_5xx_schedules_retry(self):
        lead_id = self._insert_approved()
        client = _mock_client(status_code=503)
        results = self._run(client)
        self.assertEqual(results['fail'], 1)
        lead = self._get_lead(lead_id)
        # schedule_lead_retry moves the lead back to approved with next_attempt_at set
        self.assertEqual(lead['status'], 'approved')
        self.assertIsNotNone(lead['next_attempt_at'])

    def test_network_error_requires_reconciliation_before_retry(self):
        """A lost POST response cannot prove that Oracle created nothing."""
        lead_id = self._insert_approved()
        client = _mock_client(exc=requests.ConnectionError('refused'))
        results = self._run(client)
        self.assertEqual(results['fail'], 1)
        lead = self._get_lead(lead_id)
        self.assertEqual(lead['status'], 'submission_unknown')
        self.assertIsNone(lead['next_attempt_at'])

    def test_double_claim_skips_second_run(self):
        """A lead already in 'submitting' state should not be double-claimed."""
        lead_id = self._insert_approved()
        db.claim_lead_for_submission(lead_id)
        client = _mock_client(status_code=201, json_body={'LeadId': 1, 'LeadNumber': 'LN-1'})
        results = self._run(client)
        # list_ready_for_submission filters out 'submitting' status -> 0 leads
        self.assertEqual(results['ok'], 0)
        self.assertEqual(results['fail'], 0)

    def test_field_warnings_stored_on_success(self):
        """field_warnings from the API result are persisted in the DB."""
        lead_id = self._insert_approved(extra={'market': 'UNKNOWN_MARKET'})
        client = _mock_client(status_code=201, json_body={'LeadId': 7, 'LeadNumber': 'LN-7'})
        original_submit = client.submit_lead

        def submit_with_warning(lead, trade_show, source_label='Tradeshow', **kwargs):
            r = original_submit(lead, trade_show, source_label=source_label, **kwargs)
            r['field_warnings'] = ['No verified API value for market "UNKNOWN_MARKET"']
            return r

        client.submit_lead = submit_with_warning
        results = self._run(client)
        self.assertEqual(results['ok'], 1)
        lead = self._get_lead(lead_id)
        self.assertEqual(lead['status'], 'submitted')
        import json as _json
        stored = _json.loads(lead['field_warnings'] or '[]')
        self.assertTrue(any('UNKNOWN_MARKET' in w for w in stored))

    def test_next_attempt_at_in_future_excluded_from_ready_queue(self):
        """A lead retried with a future next_attempt_at must not appear in the ready queue."""
        lead_id = self._insert_approved()
        client = _mock_client(status_code=500)
        self._run(client)
        ready = db.list_ready_for_submission()
        ids = [r['id'] for r in ready]
        self.assertNotIn(lead_id, ids)


# ---------------------------------------------------------------------------
# _project_label()
# ---------------------------------------------------------------------------

class ProjectLabelTests(unittest.TestCase):

    def test_excel_attachment_source_gives_tradeshow_label(self):
        label, trade_show = pipeline_submit._project_label({'source': 'email_excel_attachment'})
        self.assertEqual(label, 'Tradeshow')
        self.assertEqual(trade_show, 'Email Import')

    def test_ai_extract_source_gives_inquiry_label(self):
        label, trade_show = pipeline_submit._project_label({'source': 'email_ai_extract'})
        self.assertEqual(label, 'Inquiry')
        self.assertEqual(trade_show, 'Website/Email')

    def test_unknown_source_defaults_to_inquiry(self):
        label, trade_show = pipeline_submit._project_label({'source': 'something_new'})
        self.assertEqual(label, 'Inquiry')
        self.assertEqual(trade_show, 'Website/Email')


# ---------------------------------------------------------------------------
# Source-email attachment upload
# ---------------------------------------------------------------------------

def _resp(status_code, text=''):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


class UploadAttachmentTests(unittest.TestCase):
    """oracle_api.OracleLeadApiClient.upload_attachment(). All offline."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.file_path = Path(self._tmpdir) / 'Bearing inquiry.msg'
        self.file_path.write_bytes(b'raw outlook message bytes')

    def _client(self, session, config_extra=None):
        config = {
            'base_url': 'https://fake.oracle.example',
            'api_timeout_seconds': 10,
            **(config_extra or {}),
        }
        return oracle_api.OracleLeadApiClient(
            config, username='u', password='p', session=session
        )

    def _posted_bodies(self, session):
        import json as _json
        return [_json.loads(c.kwargs['data']) for c in session.post.call_args_list]

    def test_uses_child_url_discovered_from_response_links(self):
        session = MagicMock()
        session.post.return_value = _resp(201)
        client = self._client(session)
        oracle_response = {
            'links': [
                {'rel': 'self', 'name': 'leads', 'href': 'https://x/leads/42'},
                {'rel': 'child', 'name': 'AllLeadAttachments',
                 'href': 'https://x/leads/300100/child/AllLeadAttachments'},
            ]
        }
        ok = client.upload_attachment(42, self.file_path, oracle_response=oracle_response)

        self.assertTrue(ok)
        self.assertEqual(
            session.post.call_args_list[0].args[0],
            'https://x/leads/300100/child/AllLeadAttachments',
        )

    def test_falls_back_to_all_lead_attachments_when_no_links(self):
        session = MagicMock()
        session.post.return_value = _resp(201)
        client = self._client(session)

        self.assertTrue(client.upload_attachment(42, self.file_path))
        self.assertEqual(
            session.post.call_args_list[0].args[0],
            f'{client.leads_url}/42/child/AllLeadAttachments',
        )

    def test_404_advances_to_next_child_resource_name(self):
        session = MagicMock()
        session.post.side_effect = [_resp(404), _resp(404), _resp(201)]
        client = self._client(session)

        self.assertTrue(client.upload_attachment(42, self.file_path))
        tried = [c.args[0] for c in session.post.call_args_list]
        self.assertEqual(
            tried,
            [f'{client.leads_url}/42/child/{name}'
             for name in oracle_api.ATTACHMENT_CHILD_NAMES],
        )

    def test_all_404s_returns_false_without_raising(self):
        session = MagicMock()
        session.post.return_value = _resp(404)
        client = self._client(session)
        self.assertFalse(client.upload_attachment(42, self.file_path))

    def test_payload_carries_base64_contents_and_msg_content_type(self):
        import base64 as _b64
        session = MagicMock()
        session.post.return_value = _resp(201)
        client = self._client(session)

        client.upload_attachment(42, self.file_path, file_name='Bearing inquiry.msg')
        body = self._posted_bodies(session)[0]

        self.assertEqual(body['DatatypeCode'], 'FILE')
        self.assertEqual(body['FileName'], 'Bearing inquiry.msg')
        self.assertEqual(body['UploadedFileName'], 'Bearing inquiry.msg')
        self.assertEqual(body['UploadedFileContentType'], 'application/vnd.ms-outlook')
        self.assertEqual(body['Title'], 'Bearing inquiry')
        self.assertEqual(
            _b64.b64decode(body['FileContents']), b'raw outlook message bytes'
        )

    def test_rejected_category_is_retried_with_next_candidate(self):
        session = MagicMock()
        session.post.side_effect = [
            _resp(400, 'Invalid value for attribute CategoryName'),
            _resp(201),
        ]
        client = self._client(session)

        self.assertTrue(client.upload_attachment(42, self.file_path))
        categories = [b['CategoryName'] for b in self._posted_bodies(session)]
        self.assertEqual(categories, list(oracle_api.DEFAULT_ATTACHMENT_CATEGORIES))

    def test_configured_category_is_the_only_one_tried(self):
        session = MagicMock()
        session.post.return_value = _resp(400, 'bad CategoryName')
        client = self._client(session, {'attachment_category': 'BAL_SEAL_EMAIL'})

        self.assertFalse(client.upload_attachment(42, self.file_path))
        self.assertEqual(
            {b['CategoryName'] for b in self._posted_bodies(session)},
            {'BAL_SEAL_EMAIL'},
        )

    def test_non_category_400_stops_immediately(self):
        session = MagicMock()
        session.post.return_value = _resp(400, 'Lead is locked')
        client = self._client(session)

        self.assertFalse(client.upload_attachment(42, self.file_path))
        self.assertEqual(session.post.call_count, 1)

    def test_missing_file_returns_false_without_posting(self):
        session = MagicMock()
        client = self._client(session)

        self.assertFalse(client.upload_attachment(42, Path(self._tmpdir) / 'gone.msg'))
        session.post.assert_not_called()

    def test_network_error_falls_through_to_next_url(self):
        session = MagicMock()
        session.post.side_effect = [
            requests.RequestException('connection reset'), _resp(201),
        ]
        client = self._client(session)

        self.assertTrue(client.upload_attachment(42, self.file_path))
        self.assertEqual(session.post.call_count, 2)


class AttachSourceEmailTests(unittest.TestCase):
    """pipeline_submit._attach_source_email() -- the step that runs after
    Oracle has already accepted the lead."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = Path(self._tmpdir) / 'test.db'
        db.init_db(self._db_path)
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db_path
        self._original_cache_dir = db.EMAIL_CACHE_DIR
        db.EMAIL_CACHE_DIR = Path(self._tmpdir) / 'email_cache'

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        db.EMAIL_CACHE_DIR = self._original_cache_dir

    def _lead_with_email(self, subject='Spring seal inquiry', file_suffix='.msg'):
        """An approved lead whose message has an exported email file on disk."""
        message_id = db.insert_message('entry-1', subject=subject)
        source = Path(self._tmpdir) / f'original{file_suffix}'
        source.write_bytes(b'email bytes')
        db.set_message_file_path(message_id, str(source))
        return db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com'},
            'email_ai_extract', message_id=message_id, status='approved',
        )

    def test_attachment_named_after_subject_keeping_real_extension(self):
        lead_id = self._lead_with_email()
        client = MagicMock()
        client.upload_attachment.return_value = True

        warning = pipeline_submit._attach_source_email(
            client, db.get_lead(lead_id), '42', {'response': {}}
        )

        self.assertIsNone(warning)
        self.assertEqual(
            client.upload_attachment.call_args.kwargs['file_name'],
            'Spring seal inquiry.msg',
        )

    def test_prefers_exported_msg_over_cached_body(self):
        message_id = db.insert_message('entry-2', subject='Inquiry')
        msg_file = Path(self._tmpdir) / 'original.msg'
        msg_file.write_bytes(b'msg')
        body_file = Path(self._tmpdir) / 'body.txt'
        body_file.write_text('body')
        conn = db.get_connection()
        conn.execute(
            'UPDATE messages SET message_file_path = ?, body_cache_path = ? WHERE id = ?',
            (str(msg_file), str(body_file), message_id),
        )
        conn.commit()
        conn.close()
        lead_id = db.insert_lead({'first_name': 'Ada'}, 'email_ai_extract',
                                 message_id=message_id, status='approved')

        self.assertEqual(db.get_lead_email_attachment(lead_id), str(msg_file))

    def test_falls_back_to_cached_body_when_msg_missing(self):
        message_id = db.insert_message('entry-3', subject='Inquiry')
        body_file = Path(self._tmpdir) / 'body.txt'
        body_file.write_text('body')
        conn = db.get_connection()
        conn.execute(
            'UPDATE messages SET message_file_path = ?, body_cache_path = ? WHERE id = ?',
            (str(Path(self._tmpdir) / 'deleted.msg'), str(body_file), message_id),
        )
        conn.commit()
        conn.close()
        lead_id = db.insert_lead({'first_name': 'Ada'}, 'email_ai_extract',
                                 message_id=message_id, status='approved')

        self.assertEqual(db.get_lead_email_attachment(lead_id), str(body_file))

    def test_reconstructed_file_lands_in_project_cache_dir(self):
        lead_id = db.insert_lead(
            {'first_name': 'Ada', 'lead_details': 'Needs a spring-energized seal.'},
            'email_ai_extract', status='approved',
        )
        path = Path(db.get_lead_email_attachment(lead_id))

        self.assertEqual(path.parent, db.EMAIL_CACHE_DIR)
        self.assertIn('spring-energized seal', path.read_text(encoding='utf-8'))

    def test_upload_failure_returns_warning_not_exception(self):
        lead_id = self._lead_with_email()
        client = MagicMock()
        client.upload_attachment.return_value = False

        warning = pipeline_submit._attach_source_email(
            client, db.get_lead(lead_id), '42', {'response': {}}
        )
        self.assertIn('could not be attached', warning)

    def test_unexpected_error_is_swallowed_into_a_warning(self):
        """A raise here would send an already-created Oracle lead back through
        _run_api_submit's retry path and duplicate it."""
        lead_id = self._lead_with_email()
        client = MagicMock()
        client.upload_attachment.side_effect = RuntimeError('boom')

        warning = pipeline_submit._attach_source_email(
            client, db.get_lead(lead_id), '42', {'response': {}}
        )
        self.assertIn('boom', warning)

    def test_no_oracle_id_skips_attachment_entirely(self):
        lead_id = self._lead_with_email()
        client = MagicMock()

        self.assertIsNone(
            pipeline_submit._attach_source_email(
                client, db.get_lead(lead_id), None, {'response': {}}
            )
        )
        client.upload_attachment.assert_not_called()


class OutlookMarkReviewedToggleTests(unittest.TestCase):
    """config.json -> outlook.mark_reviewed gates whether the pipeline writes
    the green category back to the real Sales mailbox. Off while the system is
    being evaluated, so a test run never alters production mail."""

    def setUp(self):
        import review_marking
        self.review_marking = review_marking
        self._tmpdir = tempfile.mkdtemp()
        self._original = review_marking.CONFIG_PATH

    def tearDown(self):
        self.review_marking.CONFIG_PATH = self._original

    def _config(self, outlook):
        path = Path(self._tmpdir) / 'config.json'
        path.write_text(json.dumps({'outlook': outlook}), encoding='utf-8')
        self.review_marking.CONFIG_PATH = path

    def test_disabled_when_flag_false(self):
        self._config({'mailbox_name': 'Sales', 'mark_reviewed': False})
        self.assertFalse(self.review_marking.marking_enabled())

    def test_enabled_when_flag_true(self):
        self._config({'mailbox_name': 'Sales', 'mark_reviewed': True})
        self.assertTrue(self.review_marking.marking_enabled())

    def test_defaults_to_enabled_when_key_absent(self):
        self._config({'mailbox_name': 'Sales'})
        self.assertTrue(self.review_marking.marking_enabled())

    def test_unreadable_config_defaults_to_enabled(self):
        self.review_marking.CONFIG_PATH = Path(self._tmpdir) / 'missing.json'
        self.assertTrue(self.review_marking.marking_enabled())

    def test_outlook_is_never_touched_while_disabled(self):
        from unittest.mock import patch
        self._config({'mark_reviewed': False})
        with patch('outlook_ingest.mark_reviewed') as marker, \
             patch('db.get_lead') as get_lead:
            self.review_marking.maybe_mark_email_reviewed(123)
        marker.assert_not_called()
        # Bails out before any database work too.
        get_lead.assert_not_called()


class ExistingPartyLookupTests(unittest.TestCase):
    """Linking a lead to an account/contact Oracle already has.

    By party identifier -- CustomerId for the account, ContactPartyNumber for
    the contact -- because the name form this used to send does not link. Ten
    of twelve leads sent with the exact stored name and EnableCreateAccount
    = 'N' reached Oracle with the flag flipped back to 'Y', no CustomerId and
    the company left as loose text (audited 2026-09-18).
    """

    def _client(self, get_responses):
        session = MagicMock()
        session.get.side_effect = get_responses
        return oracle_api.OracleLeadApiClient(
            {'base_url': 'https://fake.oracle.example', 'api_timeout_seconds': 10},
            username='u', password='p', session=session,
        )

    def _items(self, items):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {'items': items}
        return resp

    def _contact(self):
        return {'PartyId': 1, 'PartyNumber': '15001', 'ContactName': 'PAT QUINLAN',
                'EmailAddress': 'pat.quinlan@cascadia-mfg.com',
                'AccountName': 'CASCADIA TECHNOLOGY',
                'AccountPartyId': 100000055846517, 'AccountPartyNumber': '11620'}

    def test_contact_email_match_links_both_contact_and_account(self):
        client = self._client([self._items([self._contact()])])

        result = client.resolve_existing_party({'email': 'Pat.Quinlan@Cascadia-mfg.com',
                                                'organization': 'Cascadia'})

        self.assertEqual(result['match_basis'], 'contact_email')
        self.assertEqual(result['contact_name'], 'PAT QUINLAN')
        self.assertEqual(result['account_name'], 'CASCADIA TECHNOLOGY')

    def test_the_party_identifiers_come_back_with_the_names(self):
        # The names alone are what left leads attached to nothing.
        client = self._client([self._items([self._contact()])])

        result = client.resolve_existing_party({'email': 'pat.quinlan@cascadia-mfg.com',
                                                'organization': 'Cascadia'})

        self.assertEqual(result['account_party_id'], 100000055846517)
        self.assertEqual(result['contact_party_number'], '15001')

    def test_matched_party_payload_links_by_identifier(self):
        existing = {'account_name': 'CASCADIA TECHNOLOGY',
                    'account_party_id': 100000055846517,
                    'account_party_number': '11620',
                    'contact_name': 'PAT QUINLAN',
                    'contact_party_number': '15001', 'warnings': []}
        payload, _, _ = oracle_api.build_lead_payload(
            {'first_name': 'Pat', 'last_name': 'Quinlan', 'organization': 'Cascadia',
             'market': 'INDUSTRIAL'},
            'Website/Email', source_label='Inquiry', existing=existing,
        )

        self.assertEqual(payload['CustomerId'], 100000055846517)
        self.assertEqual(payload['ContactPartyNumber'], '15001')
        self.assertEqual(payload['EnableCreateAccount'], 'N')
        self.assertEqual(payload['EnableCreateContact'], 'N')

    def test_the_matched_names_are_not_sent_alongside_the_identifiers(self):
        # CustomerPartyName is the attribute Oracle reads as "create an account
        # called this", which is the opposite of what a match means.
        existing = {'account_name': 'CASCADIA TECHNOLOGY',
                    'account_party_id': 100000055846517,
                    'contact_name': 'PAT QUINLAN',
                    'contact_party_number': '15001', 'warnings': []}
        payload, _, _ = oracle_api.build_lead_payload(
            {'first_name': 'Pat', 'last_name': 'Quinlan', 'organization': 'Cascadia',
             'market': 'INDUSTRIAL'},
            'Website/Email', source_label='Inquiry', existing=existing,
        )

        self.assertNotIn('CustomerPartyName', payload)
        self.assertNotIn('PrimaryContactPartyName', payload)

    def test_a_party_number_is_never_sent_as_a_customer_id(self):
        # Account party numbers are strings on this tenant (SCE_150572), and
        # CustomerId is an integer attribute -- a 400 that would reject the
        # whole lead.
        existing = {'account_name': 'ARTHREX (MA)', 'account_party_id': 'SCE_150572',
                    'warnings': []}
        payload, _, warnings = oracle_api.build_lead_payload(
            {'first_name': 'Aiden', 'last_name': 'Sim', 'organization': 'ARTHREX (MA)',
             'market': 'MEDICAL'},
            'Website/Email', source_label='Inquiry', existing=existing,
        )

        self.assertNotIn('CustomerId', payload)
        self.assertEqual(payload['CustomerPartyName'], 'ARTHREX (MA)')
        self.assertEqual(payload['EnableCreateAccount'], 'N')
        self.assertTrue(any('No party ID' in w for w in warnings))

    def test_a_match_with_no_identifier_says_so_on_the_lead(self):
        # The name form is the one that quietly did nothing, so a lead that
        # falls back to it carries a warning rather than looking linked.
        existing = {'account_name': 'CASCADIA TECHNOLOGY', 'warnings': []}
        _, _, warnings = oracle_api.build_lead_payload(
            {'first_name': 'Pat', 'organization': 'Cascadia', 'market': 'INDUSTRIAL'},
            'Website/Email', source_label='Inquiry', existing=existing,
        )

        self.assertTrue(any('may not resolve' in w for w in warnings))

    def test_no_match_still_creates_new_account_and_contact(self):
        payload, _, _ = oracle_api.build_lead_payload(
            {'first_name': 'Ada', 'last_name': 'Lovelace', 'organization': 'Kreus Design',
             'market': 'INDUSTRIAL'},
            'Website/Email', source_label='Inquiry', existing=None,
        )

        self.assertEqual(payload['CustomerPartyName'], 'Kreus Design')
        self.assertEqual(payload['EnableCreateAccount'], 'Y')
        self.assertEqual(payload['EnableCreateContact'], 'Y')

    def test_single_exact_account_links_account_only(self):
        account = {'PartyId': 300000107657584, 'PartyNumber': 'SCE_1299118',
                   'OrganizationName': 'SLB'}
        client = self._client([
            self._items([]),           # no contact
            self._items([account]),    # exact
            self._items([account]),    # prefix
        ])

        result = client.resolve_existing_party({'email': 'new@slb.com', 'organization': 'SLB'})

        self.assertEqual(result['match_basis'], 'account_name_exact')
        self.assertEqual(result['account_name'], 'SLB')
        self.assertEqual(result['account_party_id'], 300000107657584)
        self.assertEqual(result['account_party_number'], 'SCE_1299118')
        self.assertIsNone(result['contact_name'])

    def test_company_name_normalization_and_similarity(self):
        self.assertEqual(oracle_api.normalize_company_name('Acme Corporation, Inc.'), 'ACME')
        self.assertEqual(oracle_api.normalize_company_name('Motion Control Products LLC'), 'MOTION CONTROL PRODUCTS')
        
        # Suffix variations are normalized to high similarity
        score = oracle_api.calculate_company_similarity('Acme Corp', 'Acme Corporation Inc.')
        self.assertEqual(score, 1.0)
        
        # Unrelated prefix matches yield low similarity score
        score_unrelated = oracle_api.calculate_company_similarity('Cat', 'Catering Services Inc')
        self.assertLess(score_unrelated, 0.80)

    def test_stringent_company_similarity_filtering_ignores_unrelated_prefix_hits(self):
        raw_prefix = [
            {'PartyNumber': '1', 'OrganizationName': 'Catering Services Inc'},
            {'PartyNumber': '2', 'OrganizationName': 'Catalent Corp'},
            {'PartyNumber': '3', 'OrganizationName': 'Cat Hydraulic Systems Inc'},
        ]
        client = self._client([
            self._items([]),          # no contact
            self._items([]),          # no exact account
            self._items(raw_prefix),  # raw SQL prefix candidates
        ])

        result = client.resolve_existing_party({'email': 'john@cat.com', 'organization': 'Cat'})

        # Only 'Cat Hydraulic Systems Inc' passes stringent similarity matching (>= 0.80)
        self.assertEqual(result['match_basis'], 'ambiguous')
        warning = result['warnings'][0]
        self.assertIn('resembles 1 existing Oracle', warning)
        self.assertIn('Cat Hydraulic Systems Inc', warning)
        self.assertNotIn('Catering Services', warning)

    def test_ambiguous_company_blocks_submission_unless_bypass_dedup(self):
        prefix = [{'PartyNumber': str(i), 'OrganizationName': f'MOTION VARIANT {i}'}
                  for i in range(2)]
        client = self._client([
            self._items([]),        # no contact
            self._items([]),        # no exact account
            self._items(prefix),    # prefix candidates
        ])

        # Without bypass_dedup (force_submit=0), submit_lead blocks creation of a new company
        with self.assertRaises(oracle_api.OracleApiError) as ctx:
            client.submit_lead({'first_name': 'Ada', 'organization': 'Motion',
                                'email': 'x@motion.com', 'market': 'INDUSTRIAL'},
                               'Website/Email', source_label='Inquiry', bypass_dedup=False)
        self.assertIn('Submission blocked', str(ctx.exception))
        self.assertIn('resembles 2 existing Oracle', str(ctx.exception))

    def test_ambiguous_warning_gets_its_own_notification_headline(self):
        from dashboard.app import _build_alert
        lead = {'id': 1, 'status': 'pending_review', 'first_name': 'Ada', 'organization': 'Motion',
                'submit_error': "Submission blocked: Company 'Motion' resembles 2 existing Oracle account(s) ('MOTION VARIANT 0', 'MOTION VARIANT 1'). Choose whether to assign to an existing company or create as a new company."}
        alert = _build_alert(lead)
        self.assertEqual(alert['severity'], 'warning')
        self.assertIn('Ambiguous company match', alert['headline'])
        self.assertEqual(alert['candidates'], ['MOTION VARIANT 0', 'MOTION VARIANT 1'])

    def test_lookup_failure_blocks_creation_so_it_can_retry_safely(self):
        session = MagicMock()
        session.get.side_effect = requests.RequestException('oracle down')
        post = MagicMock(); post.status_code = 201
        post.json.return_value = {'LeadId': 5, 'LeadNumber': 'LN-5'}
        session.post.return_value = post
        client = oracle_api.OracleLeadApiClient(
            {'base_url': 'https://fake.oracle.example', 'api_timeout_seconds': 10},
            username='u', password='p', session=session,
        )

        with self.assertRaises(oracle_api.OracleApiError) as ctx:
            client.submit_lead({'first_name': 'Ada', 'organization': 'ACME',
                                'email': 'a@acme.com', 'market': 'INDUSTRIAL'},
                               'Website/Email', source_label='Inquiry')

        self.assertIn('lookup request failed', str(ctx.exception))
        session.post.assert_not_called()

    def test_non_200_lookup_blocks_creation(self):
        bad = MagicMock(); bad.status_code = 400; bad.text = 'bad query'
        client = self._client([bad, bad, bad])

        with self.assertRaises(oracle_api.OracleApiError) as ctx:
            client.resolve_existing_party({'email': 'a@b.com', 'organization': 'ACME'})
        self.assertEqual(ctx.exception.status_code, 400)


class GreenCategoryGateTests(unittest.TestCase):
    """Emails the operator has tagged green ("already entered") must not be
    ingested again. Matching is by category COLOUR: this mailbox's green tag
    is named "Completed", so the old name-based check for the word "green"
    matched nothing and every entered email was re-ingested."""

    def _item(self, categories):
        item = MagicMock()
        item.Categories = categories
        return item

    def _green(self):
        # What green_category_names() returns for the real Sales mailbox.
        return {'completed', 'green category'}

    def test_completed_tag_is_recognised_as_green(self):
        import outlook_ingest
        self.assertTrue(
            outlook_ingest._has_green_category(self._item('Completed'), self._green())
        )

    def test_literal_green_category_still_recognised(self):
        import outlook_ingest
        self.assertTrue(
            outlook_ingest._has_green_category(self._item('Green category'), self._green())
        )

    def test_green_among_several_categories_is_recognised(self):
        import outlook_ingest
        self.assertTrue(
            outlook_ingest._has_green_category(
                self._item('Second Request, Completed, Filed'), self._green()
            )
        )

    def test_non_green_categories_are_not_skipped(self):
        import outlook_ingest
        for categories in ('', None, 'Filed', 'Second Request', 'Red category'):
            self.assertFalse(
                outlook_ingest._has_green_category(self._item(categories), self._green()),
                f'should not be treated as green: {categories!r}',
            )

    def test_case_and_spacing_do_not_matter(self):
        import outlook_ingest
        self.assertTrue(
            outlook_ingest._has_green_category(self._item('  cOmPlEtEd '), self._green())
        )


class LocalDuplicateGuardTests(unittest.TestCase):
    """The pre-Oracle duplicate guard.

    Oracle's own dedup query cannot see leads it hasn't assigned an owner to --
    including ones this pipeline created moments earlier -- so a second
    approval of the same person used to sail straight through and create a
    duplicate in Oracle. This guard runs off the local record instead.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = Path(self._tmpdir) / 'test.db'
        db.init_db(self._db_path)
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db_path
        self._original_cache_dir = db.EMAIL_CACHE_DIR
        db.EMAIL_CACHE_DIR = Path(self._tmpdir) / 'email_cache'

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        db.EMAIL_CACHE_DIR = self._original_cache_dir

    def _lead(self, **over):
        fields = {'first_name': 'Daniel', 'last_name': 'Rundlett',
                  'organization': 'Teledyne FLIR',
                  'email': 'daniel.rundlett@teledyne.com', 'market': 'INDUSTRIAL',
                  'country': 'United States', 'assigned_tm': 'Bill Rost'}
        fields.update(over)
        return db.insert_lead(fields, 'email_ai_extract', status='approved')

    def test_second_approval_of_same_person_never_reaches_oracle(self):
        first = self._lead()
        db.mark_lead_submitted(first, 'TELEDYNE FLIR - Daniel Rundlett - Inquiry',
                               oracle_record_id='300000153085074')
        second = self._lead()

        client = _mock_client(status_code=201, json_body={'LeadId': 999})
        results = pipeline_submit._run_api_submit(client, db.list_ready_for_submission())

        client.session.post.assert_not_called()
        # 'returned' counts leads sent back to the Input Queue for a correction
        # -- see db.return_lead_for_correction(). A duplicate is not one of them.
        self.assertEqual(results, {'ok': 1, 'fail': 0, 'returned': 0})
        row = db.get_lead(second)
        self.assertEqual(row['status'], 'duplicate')
        self.assertEqual(row['duplicate_of_lead_id'], first)
        self.assertEqual(row['oracle_record_id'], '300000153085074')

    def test_duplicate_raises_a_blocked_notification(self):
        first = self._lead()
        db.mark_lead_submitted(first, 'Project', oracle_record_id='300000153085074')
        second = self._lead()
        pipeline_submit._run_api_submit(
            _mock_client(status_code=201, json_body={'LeadId': 999}),
            db.list_ready_for_submission(),
        )

        from dashboard.app import _build_alert
        alert = _build_alert(db.get_lead(second))
        self.assertEqual(alert['severity'], 'blocked')
        self.assertIn('duplicate', alert['headline'].lower())

    def test_different_person_at_same_company_still_submits(self):
        first = self._lead()
        db.mark_lead_submitted(first, 'Project', oracle_record_id='1')
        self._lead(first_name='Mike', last_name='Toomey',
                   email='mike.toomey@teledyne.com')

        client = _mock_client(status_code=201, json_body={'LeadId': 2, 'LeadNumber': 'LN-2'})
        pipeline_submit._run_api_submit(client, db.list_ready_for_submission())
        self.assertTrue(client.session.post.called)

    def test_prior_lead_that_never_reached_oracle_does_not_block(self):
        """A failed or rejected earlier attempt is not a reason to skip."""
        first = self._lead()
        db.mark_lead_failed(first, 'Oracle rejected the payload')
        self._lead()

        client = _mock_client(status_code=201, json_body={'LeadId': 2})
        pipeline_submit._run_api_submit(client, db.list_ready_for_submission())
        self.assertTrue(client.session.post.called)

    def test_first_ever_lead_is_unaffected(self):
        self._lead()
        client = _mock_client(status_code=201, json_body={'LeadId': 1, 'LeadNumber': 'LN-1'})
        results = pipeline_submit._run_api_submit(client, db.list_ready_for_submission())

        self.assertTrue(client.session.post.called)
        self.assertEqual(results['ok'], 1)


class SubmissionAlertTests(unittest.TestCase):
    """The dashboard bell: leads whose submission needs a human to look at it.

    The case that matters most is a lead Oracle's dedup check matched to an
    existing record -- it is stored as 'submitted' with only a field_warnings
    entry, so before this it left no visible trace anywhere on the dashboard.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = Path(self._tmpdir) / 'test.db'
        db.init_db(self._db_path)
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db_path

    def tearDown(self):
        db.DB_PATH = self._original_db_path

    def _lead(self, **fields):
        lead_id = db.insert_lead({'first_name': 'Ada', 'last_name': 'Lovelace',
                                  'organization': 'Motion'},
                                 'email_ai_extract', status='approved')
        if fields:
            db.update_lead_fields(lead_id, fields)
        return lead_id

    def _alerts(self):
        from dashboard.app import _build_alert
        return [a for a in (_build_alert(l) for l in db.list_submission_alerts()) if a]

    def test_dedup_skipped_lead_raises_a_blocked_alert(self):
        lead_id = self._lead()
        db.mark_lead_submitted(
            lead_id, 'MOTION - Ada Lovelace - Inquiry - Website/Email',
            oracle_record_id='300000152778079',
            field_warnings=['Lead already exists in Oracle (SCE_1895403)'],
        )
        alerts = self._alerts()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['severity'], 'blocked')
        self.assertIn('already in Oracle', alerts[0]['headline'])
        self.assertEqual(alerts[0]['lead_id'], lead_id)

    def test_clean_submission_raises_no_alert(self):
        lead_id = self._lead()
        db.mark_lead_submitted(lead_id, 'Project', oracle_record_id='1')
        self.assertEqual(self._alerts(), [])

    def test_failed_and_unknown_leads_raise_blocked_alerts(self):
        db.mark_lead_failed(self._lead(), 'Oracle rejected the payload')
        db.mark_submission_unknown(self._lead(), 'Crashed after Save')
        alerts = self._alerts()

        self.assertEqual(len(alerts), 2)
        self.assertTrue(all(a['severity'] == 'blocked' for a in alerts))

    def test_mapping_warning_is_a_warning_not_blocked(self):
        lead_id = self._lead()
        db.mark_lead_submitted(lead_id, 'Project', oracle_record_id='1',
                               field_warnings=['No verified API value for market "X"'])
        alerts = self._alerts()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['severity'], 'warning')

    def test_dismissed_alert_disappears(self):
        lead_id = self._lead()
        db.mark_lead_failed(lead_id, 'boom')
        self.assertEqual(len(self._alerts()), 1)

        db.dismiss_lead_alert(lead_id)
        self.assertEqual(self._alerts(), [])

    def test_dismiss_all_clears_every_open_alert(self):
        db.mark_lead_failed(self._lead(), 'boom')
        db.mark_submission_unknown(self._lead(), 'unclear')

        self.assertEqual(db.dismiss_all_lead_alerts(), 2)
        self.assertEqual(self._alerts(), [])

    def test_new_failure_reopens_a_dismissed_alert(self):
        lead_id = self._lead()
        db.mark_lead_failed(lead_id, 'first failure')
        db.dismiss_lead_alert(lead_id)
        self.assertEqual(self._alerts(), [])

        db.mark_lead_failed(lead_id, 'a new, later failure')
        self.assertEqual(len(self._alerts()), 1)

    def test_new_dedup_warning_reopens_a_dismissed_alert(self):
        lead_id = self._lead()
        db.mark_lead_failed(lead_id, 'boom')
        db.dismiss_lead_alert(lead_id)

        db.mark_lead_submitted(lead_id, 'Project', oracle_record_id='1',
                               field_warnings=['Lead already exists in Oracle (SCE_1)'])
        self.assertEqual(len(self._alerts()), 1)


class DedupQueryEscapingTests(unittest.TestCase):

    def test_apostrophe_in_project_name_is_escaped_and_sent_as_a_param(self):
        session = MagicMock()
        empty_lookup = _resp(200)
        empty_lookup.json.return_value = {'items': []}
        session.get.return_value = empty_lookup
        session.post.return_value = _resp(201, '')
        session.post.return_value.json.return_value = {'LeadId': 7}
        client = oracle_api.OracleLeadApiClient(
            {'base_url': 'https://fake.oracle.example', 'api_timeout_seconds': 10},
            username='u', password='p', session=session,
        )

        client.submit_lead({'organization': "O'Brien Medical", 'first_name': 'Ada'},
                           'Website/Email', source_label='Inquiry')

        q = session.get.call_args.kwargs['params']['q']
        self.assertIn("O''BRIEN MEDICAL", q)
        self.assertNotIn("'O'B", q)


class SendAnywayTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()

    def tearDown(self):
        try:
            Path(self._tmp.name).unlink()
        except OSError:
            pass

    def test_mark_lead_for_force_submit(self):
        prior_id = db.insert_lead({'first_name': 'Prior', 'organization': 'PriorOrg'}, source='manual_entry')
        db.mark_lead_submitted(prior_id, 'PriorProject', oracle_record_id='REC-99')
        prior = db.get_lead(prior_id)

        lead_id = db.insert_lead({'first_name': 'Jane', 'organization': 'Acme'}, source='manual_entry')
        db.mark_lead_duplicate_of_submitted(lead_id, prior, 'Duplicate test')
        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'duplicate')

        db.mark_lead_for_force_submit(lead_id)
        updated = db.get_lead(lead_id)
        self.assertEqual(updated['status'], 'approved')
        self.assertEqual(updated['force_submit'], 1)
        self.assertIsNotNone(updated['alert_dismissed_at'])

    def test_send_anyway_route(self):
        from dashboard.app import app
        app.config['TESTING'] = True
        prior_id = db.insert_lead({'first_name': 'Prior', 'organization': 'PriorOrg'}, source='manual_entry')
        db.mark_lead_submitted(prior_id, 'PriorProject', oracle_record_id='REC-99')
        prior = db.get_lead(prior_id)

        lead_id = db.insert_lead({'first_name': 'Jane', 'organization': 'Acme'}, source='manual_entry')
        db.mark_lead_duplicate_of_submitted(lead_id, prior, 'Duplicate test')

        with app.test_client() as client:
            with client.session_transaction() as s:
                s['_csrf_token'] = 'token'
            with patch('dashboard.app._start_submit_run', return_value=True):
                res = client.post(f'/leads/{lead_id}/send-anyway', data={'_csrf_token': 'token'}, follow_redirects=False)
                self.assertEqual(res.status_code, 302)

        updated = db.get_lead(lead_id)
        self.assertEqual(updated['status'], 'approved')
        self.assertEqual(updated['force_submit'], 1)

    def test_force_submit_bypasses_local_duplicate_check(self):
        lead_data = {
            'first_name': 'Jane', 'last_name': 'Doe', 'email': 'dup@example.com',
            'organization': 'Test Corp', 'country': 'United States', 'state': 'CA',
            'assigned_tm': 'Bill Rost',
        }
        # First lead already submitted
        lead1_id = db.insert_lead(lead_data, source='manual_entry')
        db.mark_lead_submitted(lead1_id, 'Project1', oracle_record_id='REC-1')

        # Second lead with same dedup_key, flagged force_submit
        lead2_id = db.insert_lead(lead_data, source='manual_entry')
        db.mark_lead_for_force_submit(lead2_id)

        client = _mock_client(status_code=201, json_body={'LeadId': 42, 'LeadNumber': 'LN-42'})
        leads = db.list_ready_for_submission()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['id'], lead2_id)

        results = pipeline_submit._run_api_submit(client, leads)
        self.assertEqual(results['ok'], 1)
        lead2 = db.get_lead(lead2_id)
        self.assertEqual(lead2['status'], 'submitted')
        self.assertEqual(lead2['force_submit'], 0)

    def test_oracle_api_bypass_dedup_skips_get_lookup(self):
        session = MagicMock()
        empty_lookup = _resp(200)
        empty_lookup.json.return_value = {'items': []}
        session.get.return_value = empty_lookup
        session.post.return_value = _resp(201, '')
        session.post.return_value.json.return_value = {'LeadId': 100}
        client = oracle_api.OracleLeadApiClient(
            {'base_url': 'https://fake.oracle.example', 'api_timeout_seconds': 10},
            username='u', password='p', session=session,
        )

        res = client.submit_lead(
            {'organization': 'Test Org', 'email': 'test@example.com', 'country': 'United States'},
            'Website/Email', source_label='Inquiry', bypass_dedup=True
        )

        # session.get may be called by resolve_existing_party (party lookup), but not for pre-submission dedup (leads endpoint)
        get_calls = session.get.call_args_list
        leads_dedup_calls = [
            c for c in get_calls if 'leads' in c[0][0] and 'q' in c.kwargs.get('params', {})
        ]
        self.assertEqual(len(leads_dedup_calls), 0)
        session.post.assert_called_once()
        self.assertEqual(res['oracle_record_id'], '100')


if __name__ == '__main__':
    unittest.main()
