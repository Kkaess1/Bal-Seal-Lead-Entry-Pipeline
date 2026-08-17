import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import db
import oracle_sync


def _response(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.text = ''
    response.json.return_value = payload
    return response


class ReportingClientTests(unittest.TestCase):
    def test_reads_lead_and_all_opportunity_pages_without_writes(self):
        session = MagicMock()
        session.get.side_effect = [
            _response({'LeadId': 42, 'StatusCode': 'QUALIFIED'}),
            _response({'items': [{'OptyNumber': 'O-1'}], 'hasMore': True, 'limit': 1}),
            _response({'items': [{'OptyNumber': 'O-2'}], 'hasMore': False, 'limit': 1}),
        ]
        client = oracle_sync.OracleSalesReportingClient(
            {'base_url': 'https://oracle.example', 'api_timeout_seconds': 12},
            'user', 'password', session=session,
        )

        lead, opportunities, order_lines = client.get_lead_outcome('42')

        self.assertEqual(lead['StatusCode'], 'QUALIFIED')
        self.assertEqual([item['OptyNumber'] for item in opportunities], ['O-1', 'O-2'])
        self.assertEqual(order_lines, [])
        self.assertEqual(session.get.call_count, 3)
        self.assertFalse(session.post.called)
        self.assertEqual(session.get.call_args_list[2].kwargs['params']['offset'], '1')

    def test_reads_sales_order_lines_by_opportunity_id(self):
        session = MagicMock()
        session.get.side_effect = [
            _response({'LeadId': 42}),
            _response({'items': [{'OptyId': 99, 'OptyNumber': 'O-1'}],
                       'hasMore': False, 'limit': 100}),
            _response({'items': [{'OptyId': 99, 'OrderLineId': 501,
                                  'OrderNumber': 'SO-1', 'Amount': 50}],
                       'hasMore': False, 'limit': 100}),
        ]
        client = oracle_sync.OracleSalesReportingClient(
            {'base_url': 'https://oracle.example'}, 'user', 'password', session=session,
        )

        _, _, order_lines = client.get_lead_outcome('42')

        self.assertEqual(order_lines[0]['OrderNumber'], 'SO-1')
        params = session.get.call_args_list[2].kwargs['params']
        self.assertEqual(params['q'], 'OptyId=99')
        self.assertIn('salesOrderLines', session.get.call_args_list[2].args[0])


class ReportingDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'last_name': 'Lovelace',
             'organization': 'Analytical Engines', 'email': 'ada@example.com'},
            'email_ai_extract', status='submitted',
        )
        db.update_lead_fields(self.lead_id, {'oracle_record_id': '42'})

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tempdir.cleanup()

    def test_snapshot_builds_dashboard_search_and_lifecycle(self):
        db.save_oracle_snapshot(
            self.lead_id,
            {
                'oracle_lead_id': '42', 'oracle_lead_number': 'L-42',
                'oracle_status': 'QUALIFIED', 'qualified_at': '2026-08-01T10:00:00Z',
                'converted_at': '2026-08-02T10:00:00Z',
                'qualification_score': 85, 'eligible_for_conversion': True,
                'last_synced_at': '2026-08-03T10:00:00Z',
            },
            [{
                'oracle_opportunity_id': '99', 'opportunity_number': 'O-99',
                'name': 'Engine order', 'status': 'WON', 'sales_stage': 'Closed',
                'revenue_amount': 125000, 'expected_amount': 125000,
                'currency_code': 'USD',
            }],
            [{
                'oracle_opportunity_id': '99', 'order_line_id': '501',
                'order_number': 'SO-10', 'order_line_number': '1',
                'product_name': 'Seal', 'line_status': 'BOOKED',
                'net_amount': 118000, 'currency_code': 'USD',
            }],
        )

        summary = db.oracle_dashboard_summary()
        self.assertEqual(summary['qualified'], 1)
        self.assertEqual(summary['converted'], 1)
        self.assertEqual(summary['won'], 1)
        self.assertEqual(summary['currencies'][0]['won_value'], 125000)
        self.assertEqual(summary['order_currencies'][0]['order_value'], 118000)
        result = db.search_lead_outcomes('O-99')[0]
        self.assertEqual(result['id'], self.lead_id)
        self.assertEqual(result['revenue_amount'], 125000)
        self.assertEqual(result['order_value'], 118000)
        lifecycle = db.get_lead_lifecycle(self.lead_id)
        self.assertEqual(len(lifecycle['opportunities']), 1)
        self.assertEqual(len(lifecycle['order_lines']), 1)
        self.assertTrue(any(event['event_type'] == 'qualified'
                            for event in lifecycle['events']))

    def test_outcome_search_is_pipeline_only_and_filterable(self):
        db.save_oracle_snapshot(
            self.lead_id,
            {
                'oracle_lead_id': '42', 'oracle_status': 'QUALIFIED',
                'qualified_at': '2026-08-01T10:00:00Z',
                'converted_at': '2026-08-02T10:00:00Z',
                'last_synced_at': '2026-08-03T10:00:00Z',
            },
            [{'oracle_opportunity_id': '99', 'opportunity_number': 'O-99'}],
        )
        db.insert_lead(
            {'first_name': 'Pending', 'last_name': 'Person'},
            'email_ai_extract', status='pending_review',
        )
        preexisting = db.insert_lead(
            {'first_name': 'Existing', 'last_name': 'Oracle'},
            'email_ai_extract', status='submitted',
        )
        db.update_lead_fields(preexisting, {
            'oracle_record_id': '100',
            'field_warnings': '["Lead already exists in Oracle (L-100)"]',
        })

        self.assertEqual([row['id'] for row in db.search_lead_outcomes()], [self.lead_id])
        self.assertEqual(
            [row['id'] for row in db.search_lead_outcomes(outcome='qualified')],
            [self.lead_id],
        )
        self.assertEqual(
            [row['id'] for row in db.search_lead_outcomes(outcome='converted')],
            [self.lead_id],
        )

    def test_later_snapshot_replaces_removed_opportunities(self):
        snapshot = {'oracle_lead_id': '42', 'last_synced_at': '2026-08-03T10:00:00Z'}
        db.save_oracle_snapshot(
            self.lead_id, snapshot,
            [{'opportunity_number': 'O-1'}, {'opportunity_number': 'O-2'}],
        )
        db.save_oracle_snapshot(self.lead_id, snapshot, [{'opportunity_number': 'O-2'}])
        lifecycle = db.get_lead_lifecycle(self.lead_id)
        self.assertEqual(
            [item['opportunity_number'] for item in lifecycle['opportunities']], ['O-2']
        )

    def test_exact_oracle_payload_is_retained_for_new_submissions(self):
        payload = {
            'Name': 'Analytical Engines - Ada Lovelace - Inquiry',
            'PrimaryContactEmailAddress': 'ada@example.com',
        }
        db.mark_lead_submitted(
            self.lead_id, payload['Name'], oracle_record_id='42',
            oracle_payload=payload,
        )
        saved = db.get_lead(self.lead_id)
        self.assertEqual(__import__('json').loads(saved['oracle_payload']), payload)


if __name__ == '__main__':
    unittest.main()
