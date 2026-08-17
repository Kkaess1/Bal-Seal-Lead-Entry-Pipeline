"""Description compaction is semantic when possible and always length-safe."""

import unittest
from unittest.mock import patch

import description_compactor


class DescriptionCompactorTests(unittest.TestCase):
    def test_short_description_is_untouched_without_ai(self):
        with patch('description_compactor.ai_provider.call_llm') as call_llm:
            result, method = description_compactor.compact_description('Need 12 seals.')
        self.assertEqual(result, 'Need 12 seals.')
        self.assertEqual(method, 'unchanged')
        call_llm.assert_not_called()

    @patch('description_compactor.ai_provider.call_llm')
    def test_long_description_uses_ai_summary(self, call_llm):
        call_llm.return_value = 'Need 12 model X seals by August 30.'
        result, method = description_compactor.compact_description('x' * 2001)
        self.assertEqual(result, 'Need 12 model X seals by August 30.')
        self.assertEqual(method, 'ai')
        self.assertIn('technical requirements', call_llm.call_args.args[0])

    @patch('description_compactor.ai_provider.call_llm')
    def test_overlong_ai_output_is_still_bounded(self, call_llm):
        call_llm.return_value = 'z' * 2500
        result, method = description_compactor.compact_description('x' * 2500)
        self.assertEqual(method, 'ai')
        self.assertLessEqual(len(result), 2000)

    @patch('description_compactor.ai_provider.call_llm')
    def test_json_wrapper_is_tolerated(self, call_llm):
        call_llm.return_value = '{"description":"Need model X seals."}'
        result, method = description_compactor.compact_description('x' * 2001)
        self.assertEqual((result, method), ('Need model X seals.', 'ai'))

    @patch('description_compactor.ai_provider.call_llm', side_effect=RuntimeError('offline'))
    def test_failure_preserves_head_and_tail_within_limit(self, _call_llm):
        source = 'BEGIN IMPORTANT ' + ('x' * 2500) + ' DEADLINE END'
        result, method = description_compactor.compact_description(source)
        self.assertEqual(method, 'fallback')
        self.assertLessEqual(len(result), 2000)
        self.assertTrue(result.startswith('BEGIN IMPORTANT'))
        self.assertTrue(result.endswith('DEADLINE END'))


if __name__ == '__main__':
    unittest.main()
