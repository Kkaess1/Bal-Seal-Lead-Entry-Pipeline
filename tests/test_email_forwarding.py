import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
import outlook_ingest
from dashboard.app import app


class OutlookForwardTests(unittest.TestCase):
    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_forwards_original_item_and_resolves_recipient(
        self, connect, get_inbox, co_initialize, co_uninitialize,
    ):
        namespace = connect.return_value
        inbox = get_inbox.return_value
        forwarded = namespace.GetItemFromID.return_value.Forward.return_value
        forwarded.Recipients.ResolveAll.return_value = True

        outlook_ingest.forward_message('entry-id', 'owner@example.com')

        namespace.GetItemFromID.assert_called_once_with('entry-id', inbox.StoreID)
        co_initialize.assert_called_once_with()
        co_uninitialize.assert_called_once_with()
        self.assertEqual(forwarded.SentOnBehalfOfName, 'sales@balseal.com')
        forwarded.Recipients.Add.assert_called_once_with('owner@example.com')
        forwarded.Send.assert_called_once_with()

    def test_note_is_escaped_and_prepended_to_html_message(self):
        forwarded = MagicMock()
        forwarded.BodyFormat = 2
        forwarded.HTMLBody = '<p>Original email</p>'

        outlook_ingest._prepend_forward_note(
            forwarded, 'Please handle this.\n<script>alert(1)</script>',
        )

        self.assertTrue(forwarded.HTMLBody.endswith('<p>Original email</p>'))
        self.assertIn('Please handle this.<br>', forwarded.HTMLBody)
        self.assertIn('&lt;script&gt;', forwarded.HTMLBody)
        self.assertNotIn('<script>', forwarded.HTMLBody)

    def test_note_is_prepended_to_plain_text_message(self):
        forwarded = MagicMock()
        forwarded.BodyFormat = 1
        forwarded.Body = 'Original email'

        outlook_ingest._prepend_forward_note(forwarded, 'Please handle this.')

        self.assertEqual(forwarded.Body, 'Please handle this.\r\n\r\nOriginal email')

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_missing_outlook_item_falls_back_to_preserved_msg(self, connect, get_inbox):
        with tempfile.NamedTemporaryFile(suffix='.msg') as saved_message:
            namespace = connect.return_value
            namespace.GetItemFromID.side_effect = RuntimeError('MAPI item not found')
            preserved = namespace.OpenSharedItem.return_value
            forwarded = preserved.Forward.return_value
            forwarded.Recipients.ResolveAll.return_value = True

            outlook_ingest.forward_message(
                'stale-entry-id', 'owner@example.com',
                fallback_message_path=saved_message.name,
            )

        namespace.OpenSharedItem.assert_called_once_with(saved_message.name)
        forwarded.Send.assert_called_once_with()
        preserved.Close.assert_called_once_with(1)

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_does_not_send_an_unresolved_recipient(self, connect, get_inbox):
        forwarded = connect.return_value.GetItemFromID.return_value.Forward.return_value
        forwarded.Recipients.ResolveAll.return_value = False

        with self.assertRaises(ValueError):
            outlook_ingest.forward_message('entry-id', 'missing@example.com')
        forwarded.Send.assert_not_called()
        forwarded.Close.assert_called_once_with(1)

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_send_error_reports_stage_and_discards_draft(self, connect, get_inbox):
        forwarded = connect.return_value.GetItemFromID.return_value.Forward.return_value
        forwarded.Recipients.ResolveAll.return_value = True
        forwarded.Send.side_effect = RuntimeError('send denied')

        with self.assertRaises(outlook_ingest.OutlookForwardError) as raised:
            outlook_ingest.forward_message('entry-id', 'owner@example.com')

        self.assertEqual(raised.exception.stage, 'sending the forwarded email')
        forwarded.Close.assert_called_once_with(1)


class ForwardRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.message_id = db.insert_message('outlook-entry-id', subject='Not a lead')
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    @patch('dashboard.app.review_marking.maybe_mark_email_reviewed')
    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_successful_forward_rejects_non_lead(self, forward, mark_reviewed):
        response = self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={
                'recipient': 'owner@example.com',
                'forward_note': 'Please take care of this.',
                '_csrf_token': 'token',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(f'/leads/{self.lead_id}?forwarded=1'))
        forward.assert_called_once_with(
            'outlook-entry-id', 'owner@example.com', note='Please take care of this.',
            fallback_message_path=None,
        )
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'rejected')
        self.assertIn('owner@example.com', db.get_lead(self.lead_id)['review_notes'])
        mark_reviewed.assert_called_once_with(self.lead_id)

        confirmation = self.client.get(response.location)
        self.assertIn(b'back to pending review', confirmation.data)

    @patch('dashboard.app.outlook_ingest.forward_message', side_effect=RuntimeError('offline'))
    def test_send_failure_leaves_lead_pending(self, forward):
        response = self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={'recipient': 'owner@example.com', '_csrf_token': 'token'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_invalid_recipient_is_not_sent(self, forward):
        self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={'recipient': 'not-an-email', '_csrf_token': 'token'},
        )

        forward.assert_not_called()
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

    def test_pending_review_card_and_detail_offer_forward_button(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            index_response = self.client.get('/')
            detail_response = self.client.get(f'/leads/{self.lead_id}')

        self.assertIn(b'open-forward-dialog', index_response.data)
        self.assertIn(b'open-forward-dialog', detail_response.data)
        self.assertIn(
            f'/leads/{self.lead_id}/forward'.encode(), index_response.data
        )


if __name__ == '__main__':
    unittest.main()
