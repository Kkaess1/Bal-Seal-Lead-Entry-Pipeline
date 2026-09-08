"""
Opening a lead's attachments.

Until 2026-08-10 attachments were described but never served: the files are
whatever a stranger emailed us, and hosting one makes the dashboard a delivery
mechanism for it. They are now fetched from Outlook one at a time, on demand,
and always handed over as a download -- never rendered in the page, and never
written to disk until somebody actually asks for one.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
import outlook_ingest
from dashboard.app import app


def _item_with_attachments(*names):
    """An Outlook item whose Attachments collection holds these filenames."""
    item = MagicMock()
    attachments = []
    for name in names:
        att = MagicMock()
        att.FileName = name
        att.Size = 1024
        attachments.append(att)
    item.Attachments.Count = len(attachments)
    item.Attachments.Item.side_effect = lambda i: attachments[i - 1]
    return item, attachments


class VisibleAttachmentTests(unittest.TestCase):
    def test_signature_images_are_skipped_and_indexes_still_line_up(self):
        # image001.png is Outlook's name for an embedded signature logo. It is
        # skipped in the list, so a stored position is not Outlook's index --
        # saving by the wrong one would hand over the wrong file.
        item, _ = _item_with_attachments('image001.png', 'drawing.pdf', 'quote.xlsx')

        listed = outlook_ingest.list_attachments(item)
        visible = outlook_ingest._visible_attachments(item)

        self.assertEqual([a['name'] for a in listed], ['drawing.pdf', 'quote.xlsx'])
        # Position 0 of the list is Outlook's attachment 2.
        self.assertEqual(visible[0][0], 2)
        self.assertEqual(visible[1][0], 3)

    def test_kinds_are_described(self):
        item, _ = _item_with_attachments('part.STEP', 'photo.JPG', 'notes.docx')

        kinds = [a['kind'] for a in outlook_ingest.list_attachments(item)]

        self.assertEqual(kinds, ['drawing/CAD', 'image', 'document'])

    def test_inline_signature_images_with_mapi_flag_are_skipped(self):
        item, atts = _item_with_attachments('signature_logo.png', 'seal_drawing.pdf')
        # Simulate MAPI PropertyAccessor PR_ATTACH_FLAGS = 4 for inline signature logo
        atts[0].PropertyAccessor.GetProperty.return_value = 4
        atts[1].PropertyAccessor.GetProperty.return_value = 0

        listed = outlook_ingest.list_attachments(item)
        self.assertEqual([a['name'] for a in listed], ['seal_drawing.pdf'])



class SaveAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_the_right_attachment_is_saved(self, connect, get_inbox, *_):
        item, attachments = _item_with_attachments('image001.png', 'drawing.pdf')
        connect.return_value.GetItemFromID.return_value = item

        path, name = outlook_ingest.save_attachment('entry-id', 0, self.tempdir.name)

        self.assertEqual(name, 'drawing.pdf')
        # Outlook index 2, not 1: the signature image does not count.
        attachments[1].SaveAsFile.assert_called_once()
        attachments[0].SaveAsFile.assert_not_called()
        self.assertTrue(str(path).endswith('drawing.pdf'))

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_a_hostile_filename_cannot_escape_the_folder(self, connect, get_inbox, *_):
        item, attachments = _item_with_attachments(r'..\..\.env')
        connect.return_value.GetItemFromID.return_value = item

        path, name = outlook_ingest.save_attachment('entry-id', 0, self.tempdir.name)

        # The name shown to the operator is what the sender called it; the file
        # written is sanitised and stays inside the cache folder.
        self.assertEqual(name, r'..\..\.env')
        self.assertEqual(Path(path).parent, Path(self.tempdir.name))
        self.assertNotIn('..', Path(path).name)
        self.assertNotIn('\\', Path(path).name)
        self.assertNotIn('/', Path(path).name)

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_two_attachments_with_the_same_name_do_not_collide(self, connect, get_inbox, *_):
        item, attachments = _item_with_attachments('quote.pdf', 'quote.pdf')
        connect.return_value.GetItemFromID.return_value = item

        first, _ = outlook_ingest.save_attachment('entry-id', 0, self.tempdir.name)
        second, _ = outlook_ingest.save_attachment('entry-id', 1, self.tempdir.name)

        self.assertNotEqual(first, second)
        attachments[0].SaveAsFile.assert_called_once()
        attachments[1].SaveAsFile.assert_called_once()

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_a_long_name_is_trimmed_to_fit_windows_max_path(self, connect, get_inbox, *_):
        # A real 63-character attachment name failed with "Path does not
        # exist", which is what SaveAsFile reports when the path is too long.
        long_name = ('Motion_Logo_NEW_100x12_' + 'a' * 120) + '.png'
        item, attachments = _item_with_attachments(long_name)
        connect.return_value.GetItemFromID.return_value = item
        deep = Path(self.tempdir.name) / ('nested' * 12)

        path, name = outlook_ingest.save_attachment('entry-id', 0, deep)

        self.assertEqual(name, long_name)          # shown in full
        self.assertLessEqual(len(str(path)), 240)  # written short enough
        self.assertTrue(str(path).endswith('.png'))
        attachments[0].SaveAsFile.assert_called_once()

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_an_index_past_the_end_is_refused(self, connect, get_inbox, *_):
        item, _ = _item_with_attachments('drawing.pdf')
        connect.return_value.GetItemFromID.return_value = item

        with self.assertRaises(IndexError):
            outlook_ingest.save_attachment('entry-id', 5, self.tempdir.name)

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_a_deleted_email_falls_back_to_the_preserved_msg(self, connect, get_inbox, *_):
        with tempfile.NamedTemporaryFile(suffix='.msg', delete=False) as saved:
            saved_path = saved.name
        self.addCleanup(lambda: Path(saved_path).unlink(missing_ok=True))

        namespace = connect.return_value
        namespace.GetItemFromID.side_effect = RuntimeError('MAPI item not found')
        preserved, attachments = _item_with_attachments('drawing.pdf')
        namespace.OpenSharedItem.return_value = preserved

        _, name = outlook_ingest.save_attachment(
            'stale-entry-id', 0, self.tempdir.name, fallback_message_path=saved_path,
        )

        self.assertEqual(name, 'drawing.pdf')
        namespace.OpenSharedItem.assert_called_once_with(saved_path)
        preserved.Close.assert_called_once_with(1)


class AttachmentRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

        self.message_id = db.insert_message('outlook-entry-id', subject='RFQ')
        db.record_attachments(self.message_id, [
            {'name': 'drawing.pdf', 'size': 2048, 'kind': 'document'},
            {'name': 'quote.xlsx', 'size': 4096, 'kind': 'spreadsheet'},
        ])
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    @patch('dashboard.app.outlook_ingest.save_attachment')
    def test_an_attachment_is_handed_over_as_a_download(self, save):
        real_file = Path(self.tempdir.name) / 'stored.pdf'
        real_file.write_bytes(b'%PDF-1.4 pretend')
        save.return_value = (str(real_file), 'drawing.pdf')

        response = self.client.get(f'/leads/{self.lead_id}/attachment/0')
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'%PDF-1.4 pretend')
        # Never rendered in the page: a stranger's file is downloaded, not shown.
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertIn('drawing.pdf', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    @patch('dashboard.app.outlook_ingest.save_attachment')
    def test_an_index_the_reviewer_was_never_shown_is_refused(self, save):
        # Two attachments were recorded, so 2 is out of range -- and Outlook is
        # never asked, so a guessed index cannot reach anything.
        response = self.client.get(f'/leads/{self.lead_id}/attachment/2')

        self.assertEqual(response.status_code, 404)
        save.assert_not_called()

    @patch('dashboard.app.outlook_ingest.save_attachment',
           side_effect=RuntimeError('item not found'))
    def test_a_missing_email_explains_itself(self, save):
        response = self.client.get(f'/leads/{self.lead_id}/attachment/0')

        self.assertEqual(response.status_code, 302)
        self.assertIn(b'Could not open that attachment',
                      self.client.get(response.location).data)

    @patch('dashboard.app.outlook_ingest.open_in_outlook')
    def test_open_in_outlook_opens_the_source_email(self, open_in_outlook):
        response = self.client.post(
            f'/leads/{self.lead_id}/open-in-outlook', data={'_csrf_token': 'token'},
        )

        open_in_outlook.assert_called_once_with('outlook-entry-id')
        self.assertIn(b'Opened in Outlook', self.client.get(response.location).data)

    @patch('dashboard.app.outlook_ingest.open_in_outlook')
    def test_opening_in_outlook_requires_the_csrf_token(self, open_in_outlook):
        self.assertEqual(
            self.client.post(f'/leads/{self.lead_id}/open-in-outlook').status_code, 400)
        open_in_outlook.assert_not_called()

    def test_the_lead_page_lists_attachments_as_links(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get(f'/leads/{self.lead_id}').get_data(as_text=True)

        self.assertIn('drawing.pdf', page)
        self.assertIn('quote.xlsx', page)
        self.assertIn(f'/leads/{self.lead_id}/attachment/0', page)
        self.assertIn(f'/leads/{self.lead_id}/attachment/1', page)
        self.assertIn('Open email in Outlook', page)


if __name__ == '__main__':
    unittest.main()
