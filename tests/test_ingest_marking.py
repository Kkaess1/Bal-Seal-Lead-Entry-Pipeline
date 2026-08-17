import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
import outlook_ingest
import review_marking
from dashboard.app import app


class GreenCategoryTests(unittest.TestCase):
    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_ingested_message_gets_the_mailboxes_green_category(
        self, connect, get_inbox, co_initialize, co_uninitialize,
    ):
        store = get_inbox.return_value.Store
        store.Categories.Count = 1
        completed = MagicMock(Name='Completed', Color=5)
        store.Categories.Item.return_value = completed
        item = connect.return_value.GetItemFromID.return_value
        item.Categories = ''

        outlook_ingest.mark_ingested('entry-id')

        self.assertEqual(item.Categories, 'Completed')
        item.Save.assert_called_once_with()
        co_initialize.assert_called_once_with()

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_tagging_never_tears_down_com_for_the_rest_of_the_run(
        self, connect, get_inbox, co_initialize, co_uninitialize,
    ):
        """Regression, 2026-08-07: tagging ran after every ingested message and
        uninitialized the thread's COM apartment, so the next message failed
        with "CoInitialize has not been called" and its email was never
        ingested. Tagging must leave COM up for the messages that follow."""
        store = get_inbox.return_value.Store
        store.Categories.Count = 1
        store.Categories.Item.return_value = MagicMock(Name='Completed', Color=5)
        connect.return_value.GetItemFromID.return_value.Categories = ''

        outlook_ingest.mark_ingested('entry-id')

        co_uninitialize.assert_not_called()

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_existing_categories_are_kept(self, connect, get_inbox, *_):
        store = get_inbox.return_value.Store
        store.Categories.Count = 1
        store.Categories.Item.return_value = MagicMock(Name='Completed', Color=5)
        item = connect.return_value.GetItemFromID.return_value
        item.Categories = 'Urgent'

        outlook_ingest.mark_ingested('entry-id')

        self.assertEqual(item.Categories, 'Urgent, Completed')

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_marking_twice_does_not_re_save(self, connect, get_inbox, *_):
        store = get_inbox.return_value.Store
        store.Categories.Count = 1
        store.Categories.Item.return_value = MagicMock(Name='Completed', Color=5)
        item = connect.return_value.GetItemFromID.return_value
        item.Categories = 'Completed'

        outlook_ingest.mark_ingested('entry-id')

        item.Save.assert_not_called()


class IngestMarkingSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config_path = Path(self.tempdir.name) / 'config.json'
        self.original_path = review_marking.CONFIG_PATH
        review_marking.CONFIG_PATH = self.config_path
        self.addCleanup(lambda: setattr(review_marking, 'CONFIG_PATH', self.original_path))

    def _write_config(self, outlook_config):
        self.config_path.write_text(json.dumps({'outlook': outlook_config}), encoding='utf-8')

    @patch('review_marking.outlook_ingest.mark_ingested')
    def test_marking_is_on_by_default(self, mark_ingested):
        self._write_config({})

        self.assertTrue(review_marking.mark_email_ingested('entry-id'))
        mark_ingested.assert_called_once_with('entry-id')

    @patch('review_marking.outlook_ingest.mark_ingested')
    def test_switching_it_off_leaves_the_mailbox_alone(self, mark_ingested):
        self._write_config({'mark_ingested': False})

        self.assertFalse(review_marking.mark_email_ingested('entry-id'))
        mark_ingested.assert_not_called()

    @patch('review_marking.outlook_ingest.mark_ingested', side_effect=RuntimeError('Outlook shut'))
    def test_a_failed_tag_never_breaks_the_ingest_run(self, mark_ingested):
        self._write_config({'mark_ingested': True})

        # Reported as "not marked", but no exception escapes: the leads are
        # already stored, and an untagged email is just read again next run.
        self.assertFalse(review_marking.mark_email_ingested('entry-id'))


class IngestPipelineMarkingTests(unittest.TestCase):
    """The tag goes on after the message is recorded as processed, never before."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

    def _message(self):
        return {
            'id': 'outlook-entry-id', 'received_at': '2026-08-07T09:00:00',
            'sender_email': 'ada@example.com', 'sender_name': 'Ada',
            'subject': 'Seal enquiry', 'body_text': 'We need a seal.',
            'has_attachments': False, 'attachments': [], 'recipients': None,
            'reviewed_in_outlook': False, 'conversation_id': None,
        }

    @patch('pipeline_ingest.review_marking.mark_email_ingested')
    @patch('pipeline_ingest._process_body_text')
    @patch('pipeline_ingest._export_message_file', return_value=None)
    @patch('pipeline_ingest._cache_body', return_value=None)
    def test_successful_ingest_marks_the_email(self, cache, export, process, mark):
        import pipeline_ingest

        self.assertTrue(pipeline_ingest.process_message(self._message()))

        mark.assert_called_once_with('outlook-entry-id')
        self.assertEqual(
            db.get_message_by_source_id('outlook-entry-id')['status'], 'processed',
        )

    @patch('pipeline_ingest.review_marking.mark_email_ingested')
    @patch('pipeline_ingest._process_body_text', side_effect=RuntimeError('extractor down'))
    @patch('pipeline_ingest._export_message_file', return_value=None)
    @patch('pipeline_ingest._cache_body', return_value=None)
    def test_failed_ingest_leaves_the_email_untagged(self, cache, export, process, mark):
        import pipeline_ingest

        self.assertFalse(pipeline_ingest.process_message(self._message()))

        # Tagging a failed message green would hide it from every later run.
        mark.assert_not_called()
        self.assertEqual(
            db.get_message_by_source_id('outlook-entry-id')['status'], 'error',
        )


class ManualIngestRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def _post(self, token='token'):
        data = {'_csrf_token': token} if token is not None else {}
        return self.client.post('/ingest', data=data)

    @patch('subprocess.Popen')
    def test_button_starts_an_ingest_run(self, popen):
        response = self._post()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/'))
        popen.assert_called_once()
        self.assertIn('pipeline_ingest.py', popen.call_args.args[0][1])
        self.assertIn(b'Ingesting leads', self.client.get('/').data)

    @patch('subprocess.Popen')
    def test_ajax_click_gets_a_toast_message_without_a_redirect(self, popen):
        response = self.client.post(
            '/ingest', data={'_csrf_token': 'token'},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        self.assertIn('automatically', response.get_json()['message'])
        popen.assert_called_once()

    @patch('subprocess.Popen')
    def test_a_second_click_does_not_start_a_second_run(self, popen):
        self._post()
        response = self._post()

        popen.assert_called_once()
        self.assertIn(b'already going', self.client.get(response.location).data)

    @patch('subprocess.Popen')
    def test_a_run_from_long_ago_does_not_block_a_new_one(self, popen):
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        db.set_run_state('manual_ingest_started_at', stale)

        self._post()

        popen.assert_called_once()

    @patch('subprocess.Popen')
    def test_ingest_requires_the_csrf_token(self, popen):
        response = self._post(token=None)

        self.assertEqual(response.status_code, 400)
        popen.assert_not_called()

    def test_pending_review_page_offers_the_button(self):
        page = self.client.get('/')

        self.assertIn(b'Click to ingest leads', page.data)
        self.assertIn(b'action="/ingest"', page.data)
        self.assertIn(b'id="ingest-form"', page.data)
        self.assertIn(b'}, 30000);', page.data)


if __name__ == '__main__':
    unittest.main()
