"""Offline tests for the OpenAI provider and legacy key alias."""

import json
import unittest
from unittest.mock import Mock, patch

import ai_provider


class OpenAiProviderTests(unittest.TestCase):
    @patch('ai_provider.secrets_store.get_secret')
    def test_standard_key_name_is_preferred(self, get_secret):
        get_secret.side_effect = lambda name: {
            'OPENAI_API_KEY': 'standard-key',
            'CHATGPT_API': 'legacy-key',
        }.get(name, '')
        self.assertEqual(ai_provider.get_openai_key(), 'standard-key')

    @patch('ai_provider.secrets_store.get_secret')
    def test_existing_chatgpt_api_name_is_supported(self, get_secret):
        get_secret.side_effect = lambda name: (
            'legacy-key' if name == 'CHATGPT_API' else ''
        )
        self.assertEqual(ai_provider.get_openai_key(), 'legacy-key')

    @patch('ai_provider.requests.post')
    def test_responses_api_request_and_output_parsing(self, post):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {
            'output': [{
                'type': 'message',
                'content': [{'type': 'output_text', 'text': '{"ok":true}'}],
            }]
        }
        post.return_value = response

        result = ai_provider._call_openai('prompt', 'secret', max_tokens=123)

        self.assertEqual(result, '{"ok":true}')
        _, kwargs = post.call_args
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer secret')
        self.assertEqual(kwargs['json']['model'], ai_provider.DEFAULT_OPENAI_MODEL)
        self.assertEqual(kwargs['json']['max_output_tokens'], 123)


class ToolLoopTests(unittest.TestCase):
    """The browsing loop behind hq_lookup.py."""

    def setUp(self):
        key = patch('ai_provider.get_openai_key', return_value='secret')
        key.start()
        self.addCleanup(key.stop)

    @staticmethod
    def _call(name, arguments, call_id='call_1'):
        return {'output': [{'type': 'function_call', 'name': name,
                            'call_id': call_id,
                            'arguments': json.dumps(arguments)}]}

    @staticmethod
    def _text(text):
        return {'output': [{'type': 'message',
                            'content': [{'type': 'output_text', 'text': text}]}]}

    @patch('ai_provider._post_responses')
    def test_a_tool_result_is_returned_to_the_model_and_the_answer_comes_back(self, post):
        post.side_effect = [self._call('fetch_page', {'url': 'https://x.example'}),
                            self._text('done')]
        calls = []

        answer = ai_provider.call_llm_with_tools(
            'prompt', [{'type': 'function', 'name': 'fetch_page'}],
            lambda name, arguments: calls.append((name, arguments)) or {'text': 'hi'},
        )

        self.assertEqual(answer, 'done')
        self.assertEqual(calls, [('fetch_page', {'url': 'https://x.example'})])
        sent = post.call_args_list[1][0][0]['input']
        self.assertEqual(sent[-1]['type'], 'function_call_output')
        self.assertEqual(sent[-1]['call_id'], 'call_1')
        self.assertIn('hi', sent[-1]['output'])

    @patch('ai_provider._post_responses')
    def test_a_failing_tool_is_reported_to_the_model_not_raised(self, post):
        post.side_effect = [self._call('fetch_page', {'url': 'https://x.example'}),
                            self._text('done')]

        def explode(name, arguments):
            raise RuntimeError('read timed out')

        ai_provider.call_llm_with_tools(
            'prompt', [{'type': 'function', 'name': 'fetch_page'}], explode,
        )

        handed_back = post.call_args_list[1][0][0]['input'][-1]['output']
        self.assertIn('read timed out', handed_back)

    @patch('ai_provider._post_responses')
    def test_the_last_turn_is_offered_no_tools_so_it_has_to_answer(self, post):
        post.side_effect = [
            self._call('fetch_page', {'url': 'https://x.example'}),
            self._call('fetch_page', {'url': 'https://y.example'}),
            self._text('{"found": false}'),
        ]

        ai_provider.call_llm_with_tools(
            'prompt', [{'type': 'function', 'name': 'fetch_page'}],
            lambda name, arguments: {}, max_rounds=3,
        )

        offered = [call[0][0]['tools'] for call in post.call_args_list]
        self.assertEqual([len(t) for t in offered], [1, 1, 0])

    @patch('ai_provider._post_responses')
    def test_a_json_schema_is_passed_through_as_the_text_format(self, post):
        post.return_value = self._text('{}')
        schema = {'type': 'json_schema', 'name': 'answer', 'schema': {}}

        ai_provider.call_llm_with_tools('prompt', [], lambda *a: {},
                                        text_format=schema)

        self.assertEqual(post.call_args[0][0]['text'], {'format': schema})


class JsonParsingTests(unittest.TestCase):
    def test_a_repeated_answer_parses_as_the_first_one(self):
        # One response can carry the same schema-shaped answer in two
        # output_text parts; joined, that is "Extra data", not bad JSON.
        answer = '{"found": true, "city": "Beachwood"}'
        self.assertEqual(
            ai_provider.parse_json_response(f'{answer}\n{answer}'),
            {'found': True, 'city': 'Beachwood'},
        )


if __name__ == '__main__':
    unittest.main()
