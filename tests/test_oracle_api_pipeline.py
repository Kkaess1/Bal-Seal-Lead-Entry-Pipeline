"""
Oracle REST API pipeline tests.

Covers the full api-submission path introduced in oracle_api.py and the
corresponding _run_api_submit() logic in pipeline_submit.py.  No network
traffic: the OracleLeadApiClient.session is always replaced with a
MagicMock so these run fully offline.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
               lead_source='Website', campaign=None):
        return oracle_api.build_lead_payload(
            lead, trade_show, source_label=source,
            lead_source=lead_source, campaign_name=campaign,
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

    def test_unknown_country_string_passed_through(self):
        lead = {'country': 'Narnia', 'market': 'INDUSTRIAL'}
        payload, _, _ = self._build(lead)
        self.assertEqual(payload['PrimaryContactCountry'], 'Narnia')

    def test_missing_country_defaults_to_us(self):
        payload, _, _ = self._build({'market': 'INDUSTRIAL'})
        self.assertEqual(payload['PrimaryContactCountry'], 'US')

    def test_assigned_tm_sets_owner_party_name(self):
        lead = {'assigned_tm': 'Daniel Smoliga', 'market': 'INDUSTRIAL'}
        payload, _, _ = self._build(lead)
        self.assertEqual(payload['OwnerPartyName'], 'Daniel Smoliga')

    def test_no_assigned_tm_omits_owner(self):
        payload, _, _ = self._build({'market': 'INDUSTRIAL'})
        self.assertNotIn('OwnerPartyName', payload)

    def test_no_market_key_defaults_to_industrial(self):
        payload, _, warnings = self._build({})
        self.assertEqual(payload['IndustryDescriptionBSE_c'], 'NON-TARGETED SEALS/SPRINGS')
        self.assertFalse(warnings)

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

    def test_happy_path_returns_success_dict(self):
        client = _mock_client(status_code=201, json_body={'LeadId': 99, 'LeadNumber': 'LN-0042'})
        result = client.submit_lead(self._lead(), 'Website/Email', source_label='Inquiry')
        self.assertTrue(result['success'])
        self.assertEqual(result['oracle_record_id'], '99')
        self.assertEqual(result['oracle_lead_number'], 'LN-0042')
        self.assertIsInstance(result['field_warnings'], list)

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

    def tearDown(self):
        db.DB_PATH = self._original_db_path

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

    def test_network_error_schedules_retry(self):
        """Network errors are wrapped as OracleApiError with status_code=None.
        _run_api_submit should schedule a retry (same as 5xx) since the request
        never reached Oracle -- it would be wrong to permanently fail a lead
        because of a transient connection problem."""
        lead_id = self._insert_approved()
        client = _mock_client(exc=requests.ConnectionError('refused'))
        results = self._run(client)
        self.assertEqual(results['fail'], 1)
        lead = self._get_lead(lead_id)
        self.assertEqual(lead['status'], 'approved')
        self.assertIsNotNone(lead['next_attempt_at'])

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

        def submit_with_warning(lead, trade_show, source_label='Tradeshow'):
            r = original_submit(lead, trade_show, source_label=source_label)
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


if __name__ == '__main__':
    unittest.main()
