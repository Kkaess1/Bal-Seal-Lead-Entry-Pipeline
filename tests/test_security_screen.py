import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import attachment_security
import fraud_screen
from dashboard.app import _parse_reasons


def _lead(email='buyer@example.com', details='Need a 2 inch PTFE seal.'):
    return {
        'first_name': 'Ada', 'last_name': 'Lovelace',
        'organization': 'Analytical Engines', 'email': email,
        'lead_details': details,
    }


def _screen(**kwargs):
    lead = kwargs.pop('lead', _lead())
    return fraud_screen.screen_lead(
        lead, sender_email=lead['email'],
        precomputed_llm_result={'fraud_risk': 'clear', 'reasons': []},
        **kwargs,
    )


class SecuritySeverityTests(unittest.TestCase):
    def test_gmail_is_an_informational_gray_flag(self):
        result = _screen(lead=_lead('person@gmail.com'))

        reason = next(r for r in result['fraud_reasons']
                      if r['tag'] == 'personal_email_domain')
        self.assertEqual(reason['severity'], 'info')
        self.assertEqual(result['fraud_risk'], 'clear')

    def test_disposable_mail_is_high_concern(self):
        result = _screen(lead=_lead('person@mailinator.com'))

        reason = next(r for r in result['fraud_reasons']
                      if r['tag'] == 'disposable_email_domain')
        self.assertEqual(reason['severity'], 'high')
        self.assertEqual(result['fraud_risk'], 'review')

    def test_phishing_lure_is_critical_red(self):
        result = _screen(
            subject='Urgent action required',
            body_text='Verify your mailbox password immediately. Click the link below.',
        )

        reason = next(r for r in result['fraud_reasons'] if r['tag'] == 'phishing')
        self.assertEqual(reason['severity'], 'critical')
        self.assertEqual(result['fraud_risk'], 'reject')

    def test_disguised_executable_is_critical(self):
        result = _screen(
            attachments=[{'name': 'drawing.pdf.exe', 'size': 100, 'kind': 'exe'}],
            attachment_scan={'status': 'clean'},
        )

        reason = next(r for r in result['fraud_reasons']
                      if r['tag'] == 'disguised_executable_attachment')
        self.assertEqual(reason['severity'], 'critical')

    def test_archive_is_high_concern(self):
        result = _screen(
            attachments=[{'name': 'drawings.zip', 'size': 100, 'kind': 'file'}],
            attachment_scan={'status': 'clean'},
        )

        reason = next(r for r in result['fraud_reasons']
                      if r['tag'] == 'suspicious_attachment')
        self.assertEqual(reason['severity'], 'high')

    def test_defender_detection_is_critical(self):
        result = _screen(
            attachments=[{'name': 'drawing.pdf', 'size': 100, 'kind': 'document'}],
            attachment_scan={'status': 'malicious', 'detail': 'Threat found'},
        )

        reason = next(r for r in result['fraud_reasons']
                      if r['tag'] == 'malicious_attachment')
        self.assertEqual(reason['severity'], 'critical')

    def test_old_gmail_reasons_also_render_as_information(self):
        reasons = _parse_reasons({
            'fraud_reasons': json.dumps([{
                'tag': 'personal_email_domain', 'label': 'Personal email domain',
                'detail': 'gmail.com',
            }]),
        })

        self.assertEqual(reasons[0]['severity'], 'info')

    def test_dashboard_keeps_the_existing_badge_shape_with_four_colors(self):
        root = Path(__file__).resolve().parent.parent
        table = (root / 'dashboard/templates/_lead_table.html').read_text(encoding='utf-8')
        css = (root / 'dashboard/templates/base.html').read_text(encoding='utf-8')

        self.assertIn('badge badge-reason badge-flag-{{ reason.severity }}', table)
        for severity in ('info', 'caution', 'high', 'critical'):
            self.assertIn(f'.badge-flag-{severity}', css)


class DefenderScannerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / 'message.msg'
        self.path.write_bytes(b'harmless test message')

    @patch('attachment_security._defender_command', return_value=Path('MpCmdRun.exe'))
    @patch('attachment_security.subprocess.run')
    def test_clean_scan(self, run, _command):
        run.return_value = MagicMock(returncode=0, stdout='clean', stderr='')
        self.assertEqual(attachment_security.scan_file(self.path)['status'], 'clean')

    @patch('attachment_security._defender_command', return_value=Path('MpCmdRun.exe'))
    @patch('attachment_security.subprocess.run')
    def test_detection_is_not_reported_as_clean(self, run, _command):
        run.return_value = MagicMock(returncode=2, stdout='Threat found', stderr='')
        result = attachment_security.scan_file(self.path)
        self.assertEqual(result['status'], 'malicious')
        self.assertIn('Threat found', result['detail'])


if __name__ == '__main__':
    unittest.main()
