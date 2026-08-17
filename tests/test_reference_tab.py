import io
import tempfile
import unittest
from pathlib import Path

import dashboard.app as dashboard_app
from dashboard.app import app


PNG_BYTES = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000'
    '0049454e44ae426082'
)


class ReferenceTabTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        # Registered as a cleanup rather than done in tearDown so it runs LIFO
        # *after* each test's response.close(). Windows will not delete a file
        # the served response still holds open.
        self.addCleanup(self.tempdir.cleanup)
        self.reference_dir = Path(self.tempdir.name) / 'reference'
        self.reference_dir.mkdir()
        self.original_dir = dashboard_app.REFERENCE_DIR
        dashboard_app.REFERENCE_DIR = self.reference_dir
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()

    def tearDown(self):
        dashboard_app.REFERENCE_DIR = self.original_dir

    def test_empty_folder_does_not_expose_filesystem_path(self):
        response = self.client.get('/reference')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No reference documents yet', response.data)
        self.assertNotIn(str(self.reference_dir).encode(), response.data)

    def test_dropped_image_is_listed_and_displayed(self):
        (self.reference_dir / 'CRR_Territory_Map.png').write_bytes(PNG_BYTES)

        page = self.client.get('/reference')
        served = self.client.get('/reference/file/CRR_Territory_Map.png')
        self.addCleanup(served.close)

        # Listed by filename, with the preview inside its (collapsed) row.
        self.assertIn(b'CRR_Territory_Map.png', page.data)
        self.assertIn(b'<img class="reference-image"', page.data)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.data, PNG_BYTES)
        self.assertEqual(served.headers['X-Content-Type-Options'], 'nosniff')

    def test_subfolders_become_sections(self):
        (self.reference_dir / 'Territories').mkdir()
        (self.reference_dir / 'Territories' / 'map.png').write_bytes(PNG_BYTES)

        page = self.client.get('/reference')

        served = self.client.get('/reference/file/Territories/map.png')
        self.addCleanup(served.close)

        self.assertIn(b'<h3 class="reference-section">Territories</h3>', page.data)
        self.assertEqual(served.status_code, 200)

    def test_text_document_is_shown_on_the_page(self):
        (self.reference_dir / 'notes.txt').write_text('NE -> Kurt Weber', encoding='utf-8')

        page = self.client.get('/reference')

        self.assertIn(b'NE -&gt; Kurt Weber', page.data)

    def test_the_folders_own_readme_is_not_a_document(self):
        # It is instructions for using the folder, and rendering it made the
        # tab open on a page of monospaced text that read as code.
        (self.reference_dir / 'README.md').write_text('How to use this folder',
                                                      encoding='utf-8')

        page = self.client.get('/reference')

        self.assertNotIn(b'How to use this folder', page.data)
        self.assertIn(b'No reference documents yet', page.data)

    def test_hidden_files_are_not_documents(self):
        (self.reference_dir / '.DS_Store').write_bytes(b'junk')

        self.assertIn(b'No reference documents yet', self.client.get('/reference').data)

    def test_unrenderable_types_download_instead_of_opening(self):
        # An .html here would otherwise run as a page in the dashboard's origin.
        (self.reference_dir / 'notes.html').write_text('<script>x</script>', encoding='utf-8')

        served = self.client.get('/reference/file/notes.html')
        self.addCleanup(served.close)

        self.assertEqual(served.status_code, 200)
        self.assertIn('attachment', served.headers.get('Content-Disposition', ''))

    def test_traversal_cannot_reach_files_outside_the_folder(self):
        secret = Path(self.tempdir.name) / '.env'
        secret.write_text('ORACLE_PASSWORD=hunter2', encoding='utf-8')

        for attempt in (
            '/reference/file/../.env',
            '/reference/file/..%2f.env',
            '/reference/file/nested/../../.env',
        ):
            response = self.client.get(attempt)
            self.assertNotIn(b'hunter2', response.data, attempt)
            self.assertNotEqual(response.status_code, 200, attempt)

    def test_missing_file_is_not_found(self):
        self.assertEqual(self.client.get('/reference/file/nope.png').status_code, 404)

    def test_missing_folder_does_not_break_the_page(self):
        dashboard_app.REFERENCE_DIR = self.reference_dir / 'not-created-yet'

        self.assertEqual(self.client.get('/reference').status_code, 200)

    def test_every_page_links_to_the_reference_tab(self):
        self.assertIn(b'>Reference<', self.client.get('/reference').data)


class ReferenceListTests(unittest.TestCase):
    """The tab is a file list; a preview belongs to the row you open."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.reference_dir = Path(self.tempdir.name) / 'reference'
        self.reference_dir.mkdir()
        self.original_dir = dashboard_app.REFERENCE_DIR
        dashboard_app.REFERENCE_DIR = self.reference_dir
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()

    def tearDown(self):
        dashboard_app.REFERENCE_DIR = self.original_dir

    def test_documents_are_collapsed_rows_in_one_accordion(self):
        (self.reference_dir / 'east.png').write_bytes(PNG_BYTES)
        (self.reference_dir / 'west.png').write_bytes(PNG_BYTES)

        page = self.client.get('/reference').get_data(as_text=True)

        self.assertEqual(page.count('<details class="file-row"'), 2)
        # No row starts open, and one shared name makes them exclusive.
        self.assertNotIn('<details class="file-row" name="reference-documents" open', page)
        self.assertEqual(page.count('name="reference-documents"'), 2)

    def test_a_previewed_file_may_be_framed_by_the_dashboard_itself(self):
        """Regression: the PDF preview showed "refused to connect".

        The preview is an <iframe> back to /reference/file/..., and the
        app-wide X-Frame-Options: DENY / frame-ancestors 'none' refuse all
        framing -- the dashboard's own page included -- so the browser blocked
        it and drew an error in place of the document.
        """
        (self.reference_dir / 'territories.pdf').write_bytes(b'%PDF-1.4 fake')
        (self.reference_dir / 'map.png').write_bytes(PNG_BYTES)

        for name in ('territories.pdf', 'map.png'):
            served = self.client.get('/reference/file/' + name)
            self.addCleanup(served.close)

            self.assertEqual(served.headers['X-Frame-Options'], 'SAMEORIGIN', name)
            self.assertIn("frame-ancestors 'self'",
                          served.headers['Content-Security-Policy'], name)

    def test_only_this_origin_may_frame_a_previewed_file(self):
        # SAMEORIGIN, never a blanket allow: these are internal documents.
        (self.reference_dir / 'territories.pdf').write_bytes(b'%PDF-1.4 fake')

        served = self.client.get('/reference/file/territories.pdf')
        self.addCleanup(served.close)
        policy = served.headers['Content-Security-Policy']

        self.assertNotIn('frame-ancestors *', policy)
        # The rest of the policy is unchanged -- only framing was relaxed.
        self.assertIn("default-src 'self'", policy)
        self.assertIn("script-src 'self' 'unsafe-inline'", policy)

    def test_pages_and_downloads_still_refuse_to_be_framed(self):
        # Relaxing the served files must not have relaxed the pages carrying
        # the approve/reject buttons, which is what DENY is there to protect.
        (self.reference_dir / 'notes.html').write_text('<b>x</b>', encoding='utf-8')

        page = self.client.get('/reference')
        download = self.client.get('/reference/file/notes.html')
        self.addCleanup(download.close)

        for response in (page, download):
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertIn("frame-ancestors 'none'",
                          response.headers['Content-Security-Policy'])

    def test_pdf_preview_waits_until_its_row_is_opened(self):
        (self.reference_dir / 'territories.pdf').write_bytes(b'%PDF-1.4 fake')

        page = self.client.get('/reference').get_data(as_text=True)

        # data-src, not src: three collapsed PDFs must not all load at once.
        self.assertIn('data-src="/reference/file/territories.pdf"', page)
        self.assertNotIn('<iframe class="reference-pdf" title="territories" src=', page)


class ReferenceDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.reference_dir = Path(self.tempdir.name) / 'reference'
        self.reference_dir.mkdir()
        self.original_dir = dashboard_app.REFERENCE_DIR
        dashboard_app.REFERENCE_DIR = self.reference_dir
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        dashboard_app.REFERENCE_DIR = self.original_dir

    def _delete(self, relpath, token='token'):
        data = {'relpath': relpath}
        if token is not None:
            data['_csrf_token'] = token
        return self.client.post('/reference/delete', data=data)

    def test_document_is_deleted_and_leaves_the_list(self):
        (self.reference_dir / 'old_map.png').write_bytes(PNG_BYTES)

        response = self._delete('old_map.png')

        self.assertEqual(response.status_code, 302)
        self.assertFalse((self.reference_dir / 'old_map.png').exists())
        page = self.client.get('/reference')
        self.assertIn(b'Deleted old_map.png', page.data)
        self.assertNotIn(b'old_map.png</span>', page.data)

    def test_document_in_a_subfolder_can_be_deleted(self):
        (self.reference_dir / 'Territories').mkdir()
        (self.reference_dir / 'Territories' / 'map.png').write_bytes(PNG_BYTES)

        self._delete('Territories/map.png')

        self.assertFalse((self.reference_dir / 'Territories' / 'map.png').exists())

    def test_delete_cannot_escape_the_reference_folder(self):
        secret = Path(self.tempdir.name) / '.env'
        secret.write_text('ORACLE_PASSWORD=hunter2', encoding='utf-8')

        for attempt in ('../.env', '../../.env', 'nested/../../.env'):
            self._delete(attempt)
            self.assertTrue(secret.exists(), attempt)

    def test_the_folders_own_readme_cannot_be_deleted(self):
        readme = self.reference_dir / 'README.md'
        readme.write_text('How to use this folder', encoding='utf-8')

        self._delete('README.md')

        self.assertTrue(readme.exists())

    def test_delete_requires_the_csrf_token(self):
        (self.reference_dir / 'map.png').write_bytes(PNG_BYTES)

        response = self._delete('map.png', token=None)

        self.assertEqual(response.status_code, 400)
        self.assertTrue((self.reference_dir / 'map.png').exists())

    def test_missing_document_is_reported_not_crashed(self):
        response = self._delete('nope.png')

        self.assertEqual(response.status_code, 302)
        self.assertIn(b'could not be found', self.client.get(response.location).data)


class ReferenceUploadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.reference_dir = Path(self.tempdir.name) / 'reference'
        self.reference_dir.mkdir()
        self.original_dir = dashboard_app.REFERENCE_DIR
        dashboard_app.REFERENCE_DIR = self.reference_dir
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        dashboard_app.REFERENCE_DIR = self.original_dir

    def _upload(self, filename, content=PNG_BYTES, token='token'):
        data = {'document': (io.BytesIO(content), filename)}
        if token is not None:
            data['_csrf_token'] = token
        return self.client.post(
            '/reference/upload', data=data, content_type='multipart/form-data',
        )

    def test_uploaded_screenshot_appears_on_the_page(self):
        response = self._upload('CRR Territory Map.png')

        self.assertEqual(response.status_code, 302)
        saved = self.reference_dir / 'CRR_Territory_Map.png'
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), PNG_BYTES)
        self.assertIn(b'CRR_Territory_Map.png', self.client.get('/reference').data)

    def test_several_documents_upload_at_once(self):
        self.client.post(
            '/reference/upload',
            data={
                'document': [
                    (io.BytesIO(PNG_BYTES), 'east.png'),
                    (io.BytesIO(PNG_BYTES), 'west.png'),
                ],
                '_csrf_token': 'token',
            },
            content_type='multipart/form-data',
        )

        self.assertTrue((self.reference_dir / 'east.png').is_file())
        self.assertTrue((self.reference_dir / 'west.png').is_file())

    def test_a_second_upload_never_overwrites_the_first(self):
        self._upload('map.png', content=PNG_BYTES)
        self._upload('map.png', content=b'different bytes')

        self.assertEqual((self.reference_dir / 'map.png').read_bytes(), PNG_BYTES)
        self.assertEqual(
            (self.reference_dir / 'map (2).png').read_bytes(), b'different bytes',
        )

    def test_types_the_page_cannot_display_are_refused(self):
        for filename in ('territory.svg', 'notes.html', 'installer.exe'):
            self._upload(filename, content=b'x')
            self.assertFalse((self.reference_dir / filename).exists(), filename)

    def test_upload_cannot_choose_where_it_lands(self):
        self._upload('../../.env', content=b'ORACLE_PASSWORD=hunter2')

        escaped = Path(self.tempdir.name) / '.env'
        self.assertFalse(escaped.exists())
        self.assertEqual(list(self.reference_dir.iterdir()), [])

    def test_upload_requires_the_csrf_token(self):
        response = self._upload('map.png', token=None)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.reference_dir.iterdir()), [])

    def test_an_oversized_file_is_reported_on_the_page(self):
        original_limit = app.config['MAX_CONTENT_LENGTH']
        app.config['MAX_CONTENT_LENGTH'] = 64
        self.addCleanup(lambda: app.config.update(MAX_CONTENT_LENGTH=original_limit))

        response = self._upload('huge.png', content=b'x' * 4096)

        # Redirected back to the page with an explanation, not a bare 413.
        self.assertEqual(response.status_code, 302)
        self.assertIn(b'too large', self.client.get(response.location).data)

    def test_empty_submission_is_reported(self):
        response = self.client.post(
            '/reference/upload', data={'_csrf_token': 'token'},
            content_type='multipart/form-data',
        )

        self.assertIn(b'Choose a file to add', self.client.get(response.location).data)


if __name__ == '__main__':
    unittest.main()
