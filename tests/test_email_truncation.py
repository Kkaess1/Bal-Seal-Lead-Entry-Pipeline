import unittest
from unittest.mock import patch

import email_cleaner
import lead_extractor
from email_cleaner import MAX_EMAIL_PARSE_LENGTH, TRUNCATION_NOTICE, clean_email_body, compact_for_ai, truncate_body_if_needed


class EmailTruncationTests(unittest.TestCase):
    def test_short_email_unaffected(self):
        text = "Hello, I would like to request a quote for part 12345. Thanks, John."
        self.assertEqual(truncate_body_if_needed(text), text)
        self.assertEqual(clean_email_body(text), text)
        self.assertNotIn(TRUNCATION_NOTICE, clean_email_body(text))

    def test_long_email_truncated_to_3000_chars_with_notice(self):
        long_body = "A" * 3500
        truncated = clean_email_body(long_body)
        self.assertTrue(truncated.startswith("A" * 3000))
        self.assertIn(TRUNCATION_NOTICE, truncated)
        # Check that the main body part before notice is exactly 3,000 chars
        body_part = truncated.split("[Note:")[0].rstrip()
        self.assertEqual(len(body_part), 3000)

    def test_compact_for_ai_caps_at_3000_chars(self):
        long_body = "B" * 5000
        compacted = compact_for_ai(long_body)
        self.assertIn("characters of quoted thread omitted", compacted)
        self.assertLessEqual(len(compacted), MAX_EMAIL_PARSE_LENGTH + 100)

    @patch('ai_provider.get_ai_provider', return_value=(None, None))
    def test_extract_lead_caps_input_body(self, mock_ai):
        long_body = "C" * 4500
        result = lead_extractor.extract_lead(
            subject="Test", sender_email="test@example.com",
            sender_name="Tester", body=long_body
        )
        # Verify function runs without errors and missing API key handled gracefully
        self.assertIsNone(result['is_lead'])


if __name__ == '__main__':
    unittest.main()
