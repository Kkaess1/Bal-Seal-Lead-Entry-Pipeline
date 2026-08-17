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

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_copies_cc_recipients_rather_than_addressing_them(self, connect, get_inbox):
        forwarded = connect.return_value.GetItemFromID.return_value.Forward.return_value
        forwarded.Recipients.ResolveAll.return_value = True
        added = {}
        forwarded.Recipients.Add.side_effect = lambda address: added.setdefault(
            address, MagicMock()
        )

        outlook_ingest.forward_message(
            'entry-id', ['owner@example.com', 'second@example.com'],
            cc_addresses=['manager@example.com'],
        )

        self.assertEqual(
            list(added), ['owner@example.com', 'second@example.com', 'manager@example.com'],
        )
        # Only the copy carries olCC; the addressed pair keeps Outlook's default.
        self.assertEqual(added['manager@example.com'].Type, outlook_ingest._OL_RECIPIENT_CC)
        for address in ('owner@example.com', 'second@example.com'):
            self.assertNotEqual(added[address].Type, outlook_ingest._OL_RECIPIENT_CC)
        forwarded.Send.assert_called_once_with()

    def test_forwarding_nowhere_is_refused_before_outlook_is_touched(self):
        with self.assertRaises(ValueError):
            outlook_ingest.forward_message('entry-id', [], cc_addresses=['cc@example.com'])

    def test_note_is_escaped_and_prepended_to_html_message(self):
        forwarded = MagicMock()
        forwarded.BodyFormat = 2
        forwarded.HTMLBody = '<p>Original email</p>'

        outlook_ingest._prepend_operator_note(
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

        outlook_ingest._prepend_operator_note(forwarded, 'Please handle this.')

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
            'outlook-entry-id', ['owner@example.com'], note='Please take care of this.',
            cc_addresses=[], fallback_message_path=None,
        )
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'rejected')
        self.assertIn('owner@example.com', db.get_lead(self.lead_id)['review_notes'])
        mark_reviewed.assert_called_once_with(self.lead_id)

        confirmation = self.client.get(response.location)
        self.assertIn(b'back to pending review', confirmation.data)

    @patch('dashboard.app.review_marking.maybe_mark_email_reviewed')
    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_forward_copies_in_the_cc_list(self, forward, mark_reviewed):
        response = self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={
                'recipient': 'owner@example.com, second@example.com',
                'cc_recipients': 'manager@example.com',
                '_csrf_token': 'token',
            },
        )

        self.assertEqual(response.status_code, 302)
        forward.assert_called_once_with(
            'outlook-entry-id', ['owner@example.com', 'second@example.com'],
            note=None, cc_addresses=['manager@example.com'], fallback_message_path=None,
        )
        # Who it went to is the record of what happened to the non-lead.
        self.assertIn(
            'owner@example.com, second@example.com (cc manager@example.com)',
            db.get_lead(self.lead_id)['review_notes'],
        )

    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_someone_already_addressed_is_not_copied_as_well(self, forward):
        self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={
                'recipient': 'owner@example.com',
                'cc_recipients': 'OWNER@example.com',
                '_csrf_token': 'token',
            },
        )

        forward.assert_called_once_with(
            'outlook-entry-id', ['owner@example.com'], note=None,
            cc_addresses=[], fallback_message_path=None,
        )

    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_a_cc_with_nobody_addressed_is_not_sent(self, forward):
        self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={'cc_recipients': 'manager@example.com', '_csrf_token': 'token'},
        )

        forward.assert_not_called()
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_an_invalid_cc_stops_the_whole_forward(self, forward):
        self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={
                'recipient': 'owner@example.com',
                'cc_recipients': 'not-an-email',
                '_csrf_token': 'token',
            },
        )

        forward.assert_not_called()
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

    @patch('dashboard.app.review_marking.maybe_mark_email_reviewed')
    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_dialog_forward_stays_on_the_review_page(self, forward, mark_reviewed):
        """The card's dialog posts by fetch: no redirect to a lead page the
        operator then has to come back from."""
        response = self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={'recipient': 'owner@example.com', '_csrf_token': 'token'},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['message'], 'Email forwarded to owner@example.com.')
        # Forwarding disposes of this lead only -- siblings from the same email
        # are somebody else's decision, unlike deleting the email itself.
        self.assertEqual(payload['dropped'], [self.lead_id])
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'rejected')

    @patch('dashboard.app.outlook_ingest.forward_message')
    def test_dialog_forward_reports_a_bad_address_without_navigating(self, forward):
        response = self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={'recipient': 'not-an-email', '_csrf_token': 'token'},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['ok'])
        self.assertIn('not-an-email', payload['error'])
        forward.assert_not_called()
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

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
