import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import outlook_ingest
from dashboard.app import app


class OutlookDeleteTests(unittest.TestCase):
    def setUp(self):
        enabled = patch('outlook_ingest.deletion_enabled', return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_deletes_item_from_the_sales_store(
        self, connect, get_inbox, co_initialize, co_uninitialize,
    ):
        namespace = connect.return_value
        inbox = get_inbox.return_value
        inbox.StoreID = 'sales-store'
        item = namespace.GetItemFromID.return_value
        item.Parent.StoreID = 'sales-store'

        self.assertTrue(outlook_ingest.delete_message('entry-id'))

        namespace.GetItemFromID.assert_called_once_with('entry-id', 'sales-store')
        item.Delete.assert_called_once_with()
        co_initialize.assert_called_once_with()
        co_uninitialize.assert_called_once_with()

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_missing_item_reports_nothing_deleted(self, connect, get_inbox):
        connect.return_value.GetItemFromID.side_effect = RuntimeError('MAPI item not found')

        self.assertFalse(outlook_ingest.delete_message('stale-entry-id'))

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_refuses_an_item_from_another_mailbox(self, connect, get_inbox):
        get_inbox.return_value.StoreID = 'sales-store'
        item = connect.return_value.GetItemFromID.return_value
        item.Parent.StoreID = 'personal-store'

        with self.assertRaises(ValueError):
            outlook_ingest.delete_message('entry-id')
        item.Delete.assert_not_called()

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_delete_error_reports_the_stage(self, connect, get_inbox):
        get_inbox.return_value.StoreID = 'sales-store'
        item = connect.return_value.GetItemFromID.return_value
        item.Parent.StoreID = 'sales-store'
        item.Delete.side_effect = RuntimeError('delete denied')

        with self.assertRaises(outlook_ingest.OutlookDeleteError) as raised:
            outlook_ingest.delete_message('entry-id')

        self.assertEqual(raised.exception.stage, 'deleting the email')


class DeletionSwitchTests(unittest.TestCase):
    @patch('outlook_ingest.deletion_enabled', return_value=False)
    @patch('outlook_ingest._connect')
    def test_disabled_deletion_never_reaches_outlook(self, connect, enabled):
        with self.assertRaises(ValueError):
            outlook_ingest.delete_message('entry-id')
        connect.assert_not_called()

    def test_unreadable_config_fails_closed(self):
        with patch('outlook_ingest.open', side_effect=OSError('no config')):
            self.assertFalse(outlook_ingest.deletion_enabled())


class DeleteRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.message_id = db.insert_message('outlook-entry-id', subject='Junk mail')
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'
        enabled = patch('dashboard.app.outlook_ingest.deletion_enabled', return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _post_delete(self):
        return self.client.post(
            f'/leads/{self.lead_id}/delete-email', data={'_csrf_token': 'token'},
        )

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_successful_delete_rejects_the_lead(self, delete_message):
        response = self._post_delete()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(f'/leads/{self.lead_id}?deleted=1'))
        delete_message.assert_called_once_with('outlook-entry-id')
        lead = db.get_lead(self.lead_id)
        self.assertEqual(lead['status'], 'rejected')
        self.assertIn('deleted from the Sales inbox', lead['review_notes'])

        confirmation = self.client.get(response.location)
        self.assertIn(b'Deleted email from &#34;Junk mail&#34;.', confirmation.data)

    def _post_delete_from_the_dialog(self):
        """The review page's delete dialog: a fetch that stays on the page."""
        return self.client.post(
            f'/leads/{self.lead_id}/delete-email', data={'_csrf_token': 'token'},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_dialog_delete_answers_with_the_subject_and_stays_put(self, delete_message):
        sibling_id = db.insert_lead(
            {'first_name': 'Grace', 'email': 'grace@example.com'},
            'excel_attachment', message_id=self.message_id, status='pending_review',
        )

        response = self._post_delete_from_the_dialog()

        # No redirect: the page the operator is on does not change.
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(
            payload['message'],
            'Deleted email from "Junk mail". 1 other lead from the same email '
            'was dropped too.',
        )
        # Every card that came from the email, so the caller can clear them all.
        self.assertCountEqual(payload['dropped'], [self.lead_id, sibling_id])
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'rejected')

    @patch('dashboard.app.outlook_ingest.delete_message',
           side_effect=ValueError('Deleting email from the Sales inbox is turned off.'))
    def test_dialog_delete_reports_a_refusal_without_navigating(self, delete_message):
        response = self._post_delete_from_the_dialog()

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['ok'])
        self.assertIn('turned off', payload['error'])
        # Nothing was deleted, so the lead is still there to decide on.
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_an_email_with_no_subject_still_reads_sensibly(self, delete_message):
        db.update_message_fields(self.message_id, {'subject': None})

        payload = self._post_delete_from_the_dialog().get_json()

        self.assertEqual(payload['message'], 'Deleted email with no subject.')

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_every_lead_from_the_email_is_dropped(self, delete_message):
        sibling_id = db.insert_lead(
            {'first_name': 'Grace', 'email': 'grace@example.com'},
            'excel_attachment', message_id=self.message_id, status='pending_review',
        )

        self._post_delete()

        self.assertEqual(db.get_lead(sibling_id)['status'], 'rejected')
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'rejected')

    @patch('dashboard.app.outlook_ingest.delete_message',
           side_effect=outlook_ingest.OutlookDeleteError('deleting the email', RuntimeError('x')))
    def test_delete_failure_leaves_lead_pending(self, delete_message):
        response = self._post_delete()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

    @patch('dashboard.app.outlook_ingest.delete_message',
           side_effect=ValueError('Deleting email from the Sales inbox is turned off.'))
    def test_disabled_deletion_leaves_lead_pending(self, delete_message):
        response = self._post_delete()

        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')
        self.assertIn(
            b'turned off', self.client.get(response.location).data,
        )

    @patch('dashboard.app.outlook_ingest.delete_message')
    def test_already_decided_lead_cannot_be_deleted(self, delete_message):
        db.reject_lead(self.lead_id, reviewed_by='dashboard')

        response = self._post_delete()

        self.assertEqual(response.status_code, 409)
        delete_message.assert_not_called()

    def test_pending_card_and_detail_offer_delete_button(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            index_response = self.client.get('/')
            detail_response = self.client.get(f'/leads/{self.lead_id}')

        self.assertIn(b'open-delete-dialog', index_response.data)
        self.assertIn(b'open-delete-dialog', detail_response.data)
        self.assertIn(f'/leads/{self.lead_id}/delete-email'.encode(), index_response.data)

    def test_delete_button_is_hidden_when_deletion_is_off(self):
        with patch('dashboard.app.outlook_ingest.deletion_enabled', return_value=False), \
                patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            index_response = self.client.get('/')
            detail_response = self.client.get(f'/leads/{self.lead_id}')

        self.assertNotIn(b'open-delete-dialog', index_response.data)
        self.assertNotIn(b'open-delete-dialog', detail_response.data)


if __name__ == '__main__':
    unittest.main()
