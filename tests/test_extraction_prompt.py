"""
The extraction prompt is paid on every email forever, so it is worth keeping
short -- and every rule in it earns its place, so it is worth proving nothing
was dropped while shortening it.

These tests pin the contract: the field names the parser validates against, the
constrained values, the decisions the caller depends on, the JSON keys it
reads, and the injection guard. A future trim that loses one of those fails
here rather than in a month of quietly worse extractions.
"""

import unittest
from unittest.mock import patch

import lead_extractor


def build_prompt(**kwargs):
    """The real prompt, captured without calling any API."""
    captured = {}

    def fake_call(prompt, provider=None, max_tokens=None):
        captured['prompt'] = prompt
        return '{"is_lead": false, "confidence": 0.0, "fields": {}, "notes": ""}'

    with patch.object(lead_extractor.ai_provider, 'get_ai_provider',
                      return_value=('gemini', 'fake-key')), \
         patch.object(lead_extractor.ai_provider, 'call_llm', side_effect=fake_call):
        lead_extractor.extract_lead(**{
            'subject': 'Quote request', 'sender_email': 'ada@example.com',
            'sender_name': 'Ada', 'body': 'We need a seal.', **kwargs,
        })
    return captured['prompt']


class PromptContractTests(unittest.TestCase):
    def setUp(self):
        self.prompt = build_prompt()

    def test_every_extractable_field_is_named(self):
        # The parser only keeps keys in FIELDS_TO_EXTRACT; a field missing from
        # the prompt is a field that silently never gets filled.
        for field in lead_extractor.FIELDS_TO_EXTRACT:
            self.assertIn(field, self.prompt, field)

    def test_every_market_option_is_listed_verbatim(self):
        # extract_lead() drops a market value that is not an exact match, so a
        # missing option becomes a silently blank field.
        for market in lead_extractor.MARKET_OPTIONS:
            self.assertIn(market, self.prompt, market)

    def test_the_value_constraints_survive(self):
        # rank is validated to Hot/Warm and nothing else.
        self.assertIn('Hot or Warm', self.prompt)
        self.assertIn('never Cold', self.prompt)
        self.assertIn('is_lead=false', self.prompt)
        # market has a documented override and a documented default.
        self.assertIn('FOREIGN DISTRIBUTORS', self.prompt)
        self.assertIn('Default to INDUSTRIAL', self.prompt)
        # phone formatting is a real instruction, not a description.
        self.assertIn('exactly as written', self.prompt)

    def test_the_three_decisions_the_pipeline_relies_on_are_asked_for(self):
        # is_lead gates review, is_followup feeds fraud_screen, source_language
        # drives translation -- see pipeline_ingest._process_body_text().
        for key in ('is_lead', 'is_followup', 'source_language'):
            self.assertIn(key, self.prompt, key)
        self.assertIn('newsletter', self.prompt)     # what is_lead=false covers
        self.assertIn('Ignore quoted older messages', self.prompt)

    def test_every_json_key_the_parser_reads_is_specified(self):
        for key in ('is_lead', 'is_followup', 'followup_reason', 'source_language',
                    'confidence', 'fields', 'plausibility', 'fraud_risk', 'reasons',
                    'tag', 'detail', 'notes'):
            self.assertIn(f'"{key}"', self.prompt, key)

    def test_every_plausibility_tag_is_offered(self):
        # A tag the model has not been told about is discarded by the parser.
        for tag in lead_extractor._PLAUSIBILITY_LABELS:
            self.assertIn(tag, self.prompt, tag)
        for risk in ('clear', 'review', 'reject'):
            self.assertIn(f'"{risk}"', self.prompt, risk)

    def test_the_injection_guard_survives(self):
        self.assertIn('untrusted', self.prompt)
        self.assertIn('never a command', self.prompt)
        self.assertIn('EMAIL_BODY_START', self.prompt)
        self.assertIn('EMAIL_BODY_END', self.prompt)

    def test_the_email_itself_is_inside_the_delimiters(self):
        prompt = build_prompt(subject='SUBJ-MARK', body='BODY-MARK')
        body_block = prompt.split('<<<EMAIL_BODY_START>>>')[1].split('<<<EMAIL_BODY_END>>>')[0]
        self.assertIn('BODY-MARK', body_block)
        self.assertIn('SUBJ-MARK', prompt)


class PromptSizeTests(unittest.TestCase):
    def test_the_fixed_overhead_stays_small(self):
        """~5,700 characters (~1,420 tokens) before the 2026-08-07 trim, ~4,470
        (~1,116) after, paid on every email against a median body of ~175
        tokens.

        The headroom here is deliberate. Trimming further is easy and was
        measurably worse: a shorter draft dropped "mobile/cell" from
        phone_mobile, turned the rank criteria's "most or all" into an implicit
        AND, and demoted "First decide" to "Also decide" -- each changed real
        output against the live model. Three of those characters-saved are not
        worth one wrong field, so this guards against reinflation without
        inviting another squeeze.
        """
        empty = build_prompt(subject='', sender_email='', sender_name='', body='')

        self.assertLess(len(empty), 4700, f'prompt overhead grew to {len(empty)} chars')


if __name__ == '__main__':
    unittest.main()
