"""
The lead owner Oracle actually accepts.

Oracle matches OwnerPartyName on create as an exact, case-sensitive string,
and discards a near-miss in silence -- 201 Created, no warning, and the lead
owned by whoever the API authenticated as instead of by the rep. Nothing in
the pipeline noticed for 43 submissions; it surfaced only because someone
opened a lead in Oracle and read the Owner box.

Measured over those 43, the correlation was total:

    39  name matched Oracle's spelling exactly  -> owner set at creation
     0  name matched exactly                    -> owner dropped
     0  name differed                           -> owner set at creation
     3  name differed only in case              -> owner dropped
         'Marcus James' for Oracle's 'MARCUS JAMES'  (every UK/Nordic lead)
         'john Pan'      hand-typed in the dashboard
         'jeff Frederick' hand-typed in the dashboard
"""

import unittest
from unittest.mock import patch

from oracle_api import OracleLeadApiClient, OracleApiError, build_lead_payload

CONFIG = {'base_url': 'https://fake.oracle.example', 'country_format': 'code'}


def _lead(**over):
    lead = {'first_name': 'Joona', 'last_name': 'Laitala',
            'organization': 'Finnish meteorological institute',
            'country': 'Finland', 'city': 'Helsinki',
            'assigned_tm': 'Marcus James'}
    lead.update(over)
    return lead


class OwnerResolutionTests(unittest.TestCase):
    def setUp(self):
        self.client = OracleLeadApiClient(CONFIG, 'user', 'pass')

    def _stub(self, items):
        return patch.object(self.client, '_query', return_value=items)

    def test_a_name_is_sent_in_oracles_own_spelling(self):
        """The bug in one line: the map says 'Marcus James', Oracle says
        'MARCUS JAMES', and Oracle silently keeps the lead for itself."""
        with self._stub([{'PartyName': 'MARCUS JAMES', 'PartyId': 300000040198775,
                          'PartyNumber': 'SCE_163088', 'EndDateActive': '4712-12-31'}]):
            resolved = self.client.resolve_owner('Marcus James')
        self.assertEqual(resolved['name'], 'MARCUS JAMES')
        self.assertEqual(resolved['party_id'], 300000040198775)

    def test_a_hand_typed_override_is_corrected_too(self):
        """The dashboard's Territory box takes free text, so 'john Pan' for
        'John Pan' is a lead quietly reassigned to the API account."""
        with self._stub([{'PartyName': 'John Pan', 'PartyId': 300000145872109,
                          'PartyNumber': 'SCE_1538342'}]):
            self.assertEqual(self.client.resolve_owner('john Pan')['name'], 'John Pan')

    def test_the_payload_carries_the_resolved_spelling(self):
        payload, _, warnings = build_lead_payload(
            _lead(), 'Website/Email', source_label='Inquiry',
            owner_resolver=lambda n: {'name': 'MARCUS JAMES', 'party_id': 1,
                                      'party_number': 'SCE_163088', 'ambiguous': False})
        self.assertEqual(payload['OwnerPartyName'], 'MARCUS JAMES')
        # A case-only correction is the resolver doing its job, not news --
        # otherwise every Nordic lead carries the same notice forever.
        self.assertEqual(warnings, [])

    def test_a_name_oracle_does_not_know_is_said_out_loud(self):
        """The defect was never the wrong owner so much as the silence. A rep
        who has left, or a typo'd override, must not look like a success."""
        payload, _, warnings = build_lead_payload(
            _lead(assigned_tm='Nobody McGhost'), 'Website/Email',
            source_label='Inquiry', owner_resolver=lambda n: None)
        self.assertEqual(payload['OwnerPartyName'], 'Nobody McGhost')
        self.assertTrue(any('No Oracle resource matches' in w for w in warnings))
        self.assertTrue(any('belong to the submitting account' in w for w in warnings))

    def test_matching_a_genuinely_different_name_is_flagged(self):
        payload, _, warnings = build_lead_payload(
            _lead(assigned_tm='M James'), 'Website/Email', source_label='Inquiry',
            owner_resolver=lambda n: {'name': 'MARCUS JAMES', 'party_id': 1,
                                      'party_number': 'SCE_163088', 'ambiguous': False})
        self.assertTrue(any('check it is the right person' in w for w in warnings))

    def test_two_people_with_one_name_is_flagged(self):
        payload, _, warnings = build_lead_payload(
            _lead(), 'Website/Email', source_label='Inquiry',
            owner_resolver=lambda n: {'name': 'MARCUS JAMES', 'party_id': 1,
                                      'party_number': 'SCE_163088', 'ambiguous': True})
        self.assertTrue(any('More than one Oracle resource' in w for w in warnings))

    def test_a_retired_rep_is_passed_over_for_a_current_one(self):
        # A resource record outlives the person's employment; EndDateActive is
        # what separates them, and 4712-12-31 is Oracle's "no end date".
        with self._stub([
            {'PartyName': 'OLD REP', 'PartyId': 1, 'EndDateActive': '2019-01-01'},
            {'PartyName': 'MARCUS JAMES', 'PartyId': 2, 'EndDateActive': '4712-12-31'},
        ]):
            self.assertEqual(self.client.resolve_owner('Marcus James')['name'],
                             'MARCUS JAMES')

    def test_a_lookup_outage_never_costs_the_submission(self):
        """Falling back to the stored name is exactly what the pipeline did
        before this resolver existed, so an Oracle hiccup is no worse than the
        old behaviour -- it must not turn into a refused lead."""
        with patch.object(self.client, '_query',
                          side_effect=OracleApiError('Oracle lookup failed (503)')):
            self.assertIsNone(self.client.resolve_owner('Marcus James'))

        payload, _, _ = build_lead_payload(
            _lead(), 'Website/Email', source_label='Inquiry',
            owner_resolver=lambda n: None)
        self.assertEqual(payload['OwnerPartyName'], 'Marcus James')

    def test_one_run_asks_oracle_once_per_rep(self):
        # A submit run routes many leads to the same handful of people.
        with self._stub([{'PartyName': 'MARCUS JAMES', 'PartyId': 1}]) as q:
            for _ in range(5):
                self.client.resolve_owner('Marcus James')
                self.client.resolve_owner('marcus james')   # same person
        self.assertEqual(q.call_count, 1)

    def test_no_resolver_behaves_exactly_as_before(self):
        # build_lead_payload() stays a pure function for every other caller.
        payload, _, warnings = build_lead_payload(
            _lead(), 'Website/Email', source_label='Inquiry')
        self.assertEqual(payload['OwnerPartyName'], 'Marcus James')
        self.assertEqual(warnings, [])

    def test_the_submitter_hands_its_resolver_to_the_payload(self):
        """Wiring test: the resolver is useless if submit_lead forgets it, and
        the symptom of forgetting is invisible -- leads keep submitting fine
        and keep landing on the wrong owner."""
        import inspect
        source = inspect.getsource(OracleLeadApiClient.submit_lead)
        self.assertIn('owner_resolver=self.resolve_owner', source)


if __name__ == '__main__':
    unittest.main()
