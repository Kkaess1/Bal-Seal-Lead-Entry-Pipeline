"""Offline tests for the agentic headquarters lookup.

Nothing here reaches the network or the OpenAI API: the model's turns are
scripted as Responses API payloads and the fetch tool is fed canned HTML, so
the whole loop -- prompt, tool call, tool result, answer, validation -- runs
exactly as it does in production.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import db
import hq_lookup
import territory_map
from dashboard.app import app

EATON_CONTACT_PAGE = """
<html><body>
  <h1>Contact Eaton</h1>
  <address>Eaton Corporation<br>1000 Eaton Boulevard<br>
    Beachwood, OH 44122<br>United States</address>
  <a href="/us/en-us/company/about-us.html">About us</a>
  <a href="https://www.eaton.com/us/en-us/products.html">Products</a>
</body></html>
"""

CONTACT_URL = 'https://www.eaton.com/us/en-us/company/contact.html'


def _tool_turn(url, call_id='call_1'):
    """One Responses API turn in which the model calls fetch_page."""
    return {'output': [{
        'type': 'function_call', 'name': 'fetch_page', 'call_id': call_id,
        'arguments': json.dumps({'url': url}),
    }]}


def _answer_turn(payload):
    return {'output': [{
        'type': 'message',
        'content': [{'type': 'output_text', 'text': json.dumps(payload)}],
    }]}


GOOD_ANSWER = {
    'found': True, 'address1': '1000 Eaton Boulevard', 'city': 'Beachwood',
    'state': 'OH', 'zip': '44122', 'country': 'USA', 'source_url': CONTACT_URL,
    'confidence': 0.9, 'note': "Eaton's US head office.",
}


class FetchToolTests(unittest.TestCase):
    @patch('hq_lookup.location_enrichment.get_public_page')
    def test_a_fetched_page_comes_back_as_text_and_links(self, get_page):
        get_page.return_value = (EATON_CONTACT_PAGE, CONTACT_URL)
        tool = hq_lookup._FetchTool(max_fetches=3, budget_seconds=30)

        result = tool('fetch_page', {'url': 'eaton.com/contact'})

        self.assertIn('Beachwood, OH 44122', result['text'])
        self.assertIn('https://www.eaton.com/us/en-us/company/about-us.html',
                      result['links'])
        self.assertEqual(result['pages_left'], 2)
        self.assertEqual(tool.hosts, {'eaton.com'})
        # A bare hostname from the model is still fetched over https.
        self.assertTrue(get_page.call_args[0][0].startswith('https://'))

    @patch('hq_lookup.location_enrichment.get_public_page')
    def test_the_page_budget_is_enforced_not_merely_requested(self, get_page):
        get_page.return_value = (EATON_CONTACT_PAGE, CONTACT_URL)
        tool = hq_lookup._FetchTool(max_fetches=1, budget_seconds=30)

        tool('fetch_page', {'url': CONTACT_URL})
        refused = tool('fetch_page', {'url': 'https://www.eaton.com/other'})

        self.assertIn('budget', refused['error'].lower())
        self.assertEqual(get_page.call_count, 1)

    @patch('hq_lookup.location_enrichment.get_public_page')
    def test_a_site_that_will_not_answer_is_closed_after_two_failures(self, get_page):
        # eaton.com times out on every path. Left to itself the model spends
        # its whole budget on that one host and never answers.
        get_page.side_effect = requests.ReadTimeout('read timed out')
        tool = hq_lookup._FetchTool(max_fetches=6, budget_seconds=30)

        for _ in range(2):
            self.assertIn('error', tool('fetch_page', {'url': 'https://eaton.com'}))
        refused = tool('fetch_page', {'url': 'https://www.eaton.com/contact'})

        self.assertIn('does not answer', refused['error'])
        self.assertEqual(get_page.call_count, 2)
        # Nothing was spent: the pages are still there for a site that works.
        self.assertEqual(tool.fetched, [])

    def test_an_invented_tool_name_is_reported_rather_than_run(self):
        tool = hq_lookup._FetchTool(max_fetches=3, budget_seconds=30)
        self.assertIn('error', tool('search_web', {'query': 'eaton hq'}))


class LookupTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('hq_lookup.ai_provider.get_openai_key', return_value='key')
        patcher.start()
        self.addCleanup(patcher.stop)
        settings = patch('hq_lookup.settings', return_value={
            'enabled': True, 'max_fetches': 4, 'budget_seconds': 30,
            'min_confidence': 0.6,
        })
        settings.start()
        self.addCleanup(settings.stop)

    def _run(self, answer, page=EATON_CONTACT_PAGE):
        with patch('hq_lookup.ai_provider._post_responses') as post, \
             patch('hq_lookup.location_enrichment.get_public_page') as get_page:
            get_page.return_value = (page, CONTACT_URL)
            post.side_effect = [_tool_turn(CONTACT_URL), _answer_turn(answer)]
            found = hq_lookup.find_headquarters(
                organization='Eaton', website='https://eaton.com',
                email='benemrick@eaton.com', phone='+1 301 256-1792',
            )
        return found, post

    def test_the_head_office_it_read_becomes_routable_lead_fields(self):
        found, post = self._run(GOOD_ANSWER)

        self.assertEqual(found['city'], 'Beachwood')
        self.assertEqual(found['state'], 'OH')
        self.assertEqual(found['zip'], '44122')
        self.assertEqual(found['country'], 'USA')
        self.assertEqual(found['address1'], '1000 Eaton Boulevard')
        self.assertEqual(found['_source_url'], CONTACT_URL)

        # The tool's result was handed back to the model on the second turn,
        # and the page it fetched is in it.
        second_input = post.call_args_list[1][0][0]['input']
        outputs = [i for i in second_input if i.get('type') == 'function_call_output']
        self.assertEqual(len(outputs), 1)
        self.assertIn('Beachwood', outputs[0]['output'])
        self.assertEqual(outputs[0]['call_id'], 'call_1')

    def test_a_full_state_name_is_stored_the_way_the_pipeline_writes_states(self):
        found, _ = self._run({**GOOD_ANSWER, 'state': 'Ohio',
                              'country': 'United States'})
        self.assertEqual(found['state'], 'OH')
        self.assertEqual(found['country'], 'USA')

    def test_an_address_cited_to_a_page_it_never_fetched_is_discarded(self):
        found, _ = self._run({**GOOD_ANSWER,
                              'source_url': 'https://en.wikipedia.org/wiki/Eaton'})
        self.assertEqual(found, {})

    def test_a_hedged_answer_is_discarded(self):
        found, _ = self._run({**GOOD_ANSWER, 'confidence': 0.3})
        self.assertEqual(found, {})

    def test_found_false_is_taken_at_its_word(self):
        found, _ = self._run({'found': False, 'note': 'No address on the site.'})
        self.assertEqual(found, {})

    def test_an_address_that_cannot_be_routed_is_not_written_to_the_lead(self):
        # Nevada without a ZIP cannot be split at Clark County, so it would
        # reach the reviewer as "Unassigned" anyway -- with a location that
        # looks researched. Better to leave the lead as it was.
        found, _ = self._run({**GOOD_ANSWER, 'state': 'NV', 'zip': '',
                              'city': 'Reno'})
        self.assertEqual(found, {})

    def test_a_failed_ai_call_leaves_the_lead_alone(self):
        with patch('hq_lookup.ai_provider._post_responses',
                   side_effect=RuntimeError('OpenAI API error (500)')), \
             patch('hq_lookup.location_enrichment.get_public_page',
                   return_value=(EATON_CONTACT_PAGE, CONTACT_URL)):
            self.assertEqual(hq_lookup.find_headquarters(organization='Eaton'), {})

    def test_nothing_runs_without_a_company_to_research(self):
        with patch('hq_lookup.ai_provider._post_responses') as post:
            self.assertEqual(hq_lookup.find_headquarters(email='someone@gmail.com'), {})
        post.assert_not_called()


class ApplyToLeadTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('hq_lookup.is_enabled', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _found(self, **overrides):
        base = {
            'city': 'Beachwood', 'state': 'OH', 'zip': '44122', 'country': 'USA',
            'address1': '1000 Eaton Boulevard', '_source_url': CONTACT_URL,
            '_note': "Eaton's US head office.", '_confidence': 0.9,
        }
        base.update(overrides)
        return base

    @patch('hq_lookup.find_headquarters')
    def test_blank_location_fields_are_filled_and_sourced(self, research):
        research.return_value = self._found()
        lead = {'organization': 'Eaton', 'website': 'https://eaton.com',
                'email': 'benemrick@eaton.com', 'city': ''}

        written = hq_lookup.apply_headquarters(lead)

        self.assertEqual(lead['state'], 'OH')
        self.assertEqual(lead['city'], 'Beachwood')
        self.assertEqual(written['zip'], '44122')
        self.assertEqual(
            lead['_location_source'],
            f"company HQ lookup ({CONTACT_URL}) -- Eaton's US head office.",
        )
        # The private keys are for the basis line, not for the leads table.
        self.assertNotIn('_source_url', lead)

    @patch('hq_lookup.find_headquarters')
    def test_a_stated_city_survives_the_lookup(self, research):
        research.return_value = self._found()
        lead = {'organization': 'Eaton', 'city': 'Frederick'}

        hq_lookup.apply_headquarters(lead)

        self.assertEqual(lead['city'], 'Frederick')
        self.assertEqual(lead['state'], 'OH')

    @patch('hq_lookup.find_headquarters')
    def test_a_lead_that_already_routes_is_never_researched(self, research):
        lead = {'organization': 'Siemens', 'country': 'Germany'}
        self.assertEqual(hq_lookup.apply_headquarters(lead), {})
        research.assert_not_called()

    @patch('hq_lookup.find_headquarters')
    def test_a_lead_with_a_state_is_never_researched(self, research):
        lead = {'organization': 'Eaton', 'state': 'TX'}
        self.assertEqual(hq_lookup.apply_headquarters(lead), {})
        research.assert_not_called()

    @patch('hq_lookup.find_headquarters')
    def test_a_head_office_in_another_country_than_the_lead_states_is_refused(self, research):
        research.return_value = self._found()
        lead = {'organization': 'Eaton', 'country': 'Atlantis'}

        self.assertEqual(hq_lookup.apply_headquarters(lead), {})
        self.assertEqual(lead['country'], 'Atlantis')
        self.assertNotIn('state', lead)


class FindHqRouteTests(unittest.TestCase):
    """The dashboard button: the same lookup, run on a lead already queued."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.lead_id = db.insert_lead(
            {'first_name': 'Ben', 'last_name': 'Emrick', 'organization': 'Eaton',
             'email': 'benemrick@eaton.com', 'website': 'https://eaton.com'},
            'email_ai_extract', status='pending_review',
        )
        db.set_territory_assignment(
            self.lead_id, None, 'No state on file -- cannot determine US territory',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'
        enabled = patch('dashboard.app.hq_lookup.is_enabled', return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _post(self, **data):
        return self.client.post(f'/leads/{self.lead_id}/find-hq',
                                data={'_csrf_token': 'token', **data})

    @patch('dashboard.app.hq_lookup.find_headquarters')
    def test_a_found_head_office_routes_the_lead_and_says_where_it_came_from(self, research):
        research.return_value = {
            'city': 'Beachwood', 'state': 'OH', 'zip': '44122', 'country': 'USA',
            'address1': '1000 Eaton Boulevard', '_source_url': CONTACT_URL,
            '_note': "Eaton's US head office.", '_confidence': 0.9,
        }

        response = self._post()
        self.assertEqual(response.status_code, 302)

        lead = db.get_lead(self.lead_id)
        self.assertEqual(lead['state'], 'OH')
        self.assertEqual(lead['city'], 'Beachwood')
        self.assertEqual(lead['assigned_tm'], territory_map.US_STATE_TO_TM['ohio'])
        self.assertIn('company HQ lookup', lead['assigned_tm_basis'])
        self.assertIn(CONTACT_URL, lead['assigned_tm_basis'])

    @patch('dashboard.app.hq_lookup.find_headquarters', return_value={})
    def test_a_lookup_that_finds_nothing_changes_nothing(self, research):
        response = self._post()

        self.assertEqual(response.status_code, 302)
        lead = db.get_lead(self.lead_id)
        self.assertIsNone(lead['state'])
        self.assertIsNone(lead['assigned_tm'])
        self.assertIn('cannot determine US territory', lead['assigned_tm_basis'])

    @patch('dashboard.app.hq_lookup.find_headquarters')
    def test_only_a_path_on_this_site_is_accepted_as_the_return_page(self, research):
        research.return_value = {}

        stays_home = self._post(next='https://evil.example/phish')
        self.assertTrue(stays_home.location.endswith(f'/leads/{self.lead_id}'))

        goes_back = self._post(next='/queue')
        self.assertTrue(goes_back.location.endswith('/queue'))

    @patch('dashboard.app.hq_lookup.find_headquarters')
    def test_a_submitted_lead_is_not_researched(self, research):
        db.update_lead_fields(self.lead_id, {'status': 'submitted'})

        self.assertEqual(self._post().status_code, 409)
        research.assert_not_called()


if __name__ == '__main__':
    unittest.main()
