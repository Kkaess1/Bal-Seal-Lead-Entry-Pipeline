"""
A lead missing a field Oracle requires cannot be approved, and cannot get lost.

Two gates, both reading the same list from oracle_api.submission_blockers():

  - approve() refuses the approval and says what to fix, so the operator finds
    out while looking at the lead rather than through a notification minutes
    later. This is the gate that should catch everything.
  - The submitter still checks, because a lead can be edited after it was
    staged. Anything that slips through goes back to Pending Review carrying
    the reason, instead of being parked in 'failed' where it sat in History
    until somebody noticed the bell and manually returned it.

Deliberately not applied to an outcome Oracle may have acted on -- see
test_an_unknown_outcome_is_never_returned_automatically.
"""

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
import pipeline_submit
from dashboard.app import _build_alert, _extract_company_candidates, app
import territory_map


class _AssignedTmSelectParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tm_select = False
        self.options = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select' and attrs.get('name') == 'assigned_tm':
            self.in_tm_select = True
        elif tag == 'option' and self.in_tm_select:
            self.options.append((attrs.get('value'), 'selected' in attrs))

    def handle_endtag(self, tag):
        if tag == 'select' and self.in_tm_select:
            self.in_tm_select = False


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()

    def _lead(self, status='approved', **overrides):
        fields = {
            'first_name': 'Ben', 'last_name': 'Emrick', 'organization': 'Eaton',
            'email': 'benemrick@eaton.com', 'country': 'United States',
            'state': 'MD', 'assigned_tm': 'Wade Kahout',
        }
        fields.update(overrides)
        return db.insert_lead(fields, 'email_ai_extract', status=status)

    def _approve(self, lead_id, **form):
        with self.client.session_transaction() as s:
            s['_csrf_token'] = 'token'
        return self.client.post(
            f'/leads/{lead_id}/approve',
            data={'_csrf_token': 'token', **form},
            follow_redirects=False,
        )

    def _post(self, path, **form):
        with self.client.session_transaction() as s:
            s['_csrf_token'] = 'token'
        return self.client.post(path, data={'_csrf_token': 'token', **form})


class ApprovalIsRefusedTests(_Base):
    """The front gate: an incomplete lead never becomes approved at all."""

    def test_a_lead_with_no_country_cannot_be_approved(self):
        lead_id = self._lead(status='pending_review', country=None, state=None)

        self._approve(lead_id)

        self.assertEqual(db.get_lead(lead_id)['status'], 'pending_review')

    def test_the_reason_is_recorded_on_the_lead(self):
        lead_id = self._lead(status='pending_review', country=None, state=None)

        self._approve(lead_id)

        self.assertIn('Country is missing', db.get_lead(lead_id)['submit_error'])

    def test_the_operator_is_sent_to_the_page_they_can_fix_it_on(self):
        lead_id = self._lead(status='pending_review', country=None, state=None)

        response = self._approve(lead_id)

        self.assertEqual(response.status_code, 302)
        self.assertIn(f'/leads/{lead_id}', response.headers['Location'])

    def test_the_reason_is_shown_on_that_page(self):
        lead_id = self._lead(status='pending_review', country=None, state=None)
        self._approve(lead_id)

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get(f'/leads/{lead_id}').get_data(as_text=True)

        self.assertIn('Cannot approve', page)
        self.assertIn('Country is missing', page)

    def test_a_country_with_no_derivable_territory_refuses(self):
        # Correcting the country but leaving the state blank re-derives the
        # manager as nothing, so the lead is still not sendable -- for a
        # different reason, which is the one now reported.
        lead_id = self._lead(status='pending_review', country=None, state=None)

        self._approve(lead_id, country='United States')

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'pending_review')
        self.assertIn('Territory Manager is unassigned', lead['submit_error'])

    def test_an_unassigned_territory_manager_also_refuses(self):
        lead_id = self._lead(status='pending_review', assigned_tm=None)

        self._approve(lead_id)

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'pending_review')
        self.assertIn('Territory Manager is unassigned', lead['submit_error'])

    def test_fixing_it_in_the_same_submission_approves_normally(self):
        # The check runs against the lead as it would be saved, so correcting
        # the location on the approve form is enough -- no separate save first.
        # State as well as country: changing the geography re-derives the
        # territory manager, and 'United States' with no state derives none.
        lead_id = self._lead(status='pending_review', country=None, state=None)

        self._approve(lead_id, country='United States', state='MD')

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'review_approved')
        self.assertEqual(lead['country'], 'United States')
        self.assertEqual(lead['assigned_tm'], 'Mark Copeland')

    def test_approving_clears_a_previous_refusal(self):
        lead_id = self._lead(status='pending_review', country=None, state=None)
        self._approve(lead_id)
        self.assertIsNotNone(db.get_lead(lead_id)['submit_error'])

        self._approve(lead_id, country='United States', state='MD')

        self.assertIsNone(db.get_lead(lead_id)['submit_error'])

    def test_a_complete_lead_is_unaffected(self):
        lead_id = self._lead(status='pending_review')

        self._approve(lead_id)

        self.assertEqual(db.get_lead(lead_id)['status'], 'review_approved')


class BatchApprovalTests(_Base):
    """The second gate: a lead edited after staging must not go out."""

    def test_a_staged_lead_broken_after_staging_is_held_back(self):
        good = self._lead(status='review_approved')
        bad = self._lead(status='review_approved', first_name='Bella',
                         last_name='Qin', email='bq@example.com')
        db.update_lead_fields(bad, {'country': None, 'state': None})

        with patch('dashboard.app._start_submit_run', return_value=True):
            self._post('/queue/approve_all')

        self.assertEqual(db.get_lead(bad)['status'], 'pending_review')
        self.assertEqual(db.get_lead(good)['status'], 'approved')

    def test_the_held_back_lead_carries_its_reason(self):
        bad = self._lead(status='review_approved', country=None, state=None)

        with patch('dashboard.app._start_submit_run', return_value=True):
            self._post('/queue/approve_all')

        self.assertIn('Country is missing', db.get_lead(bad)['submit_error'])


class SubmitterBackstopTests(_Base):
    """The last line: reached only if a lead changed after approval."""

    def _run_submit(self, lead_id):
        client = MagicMock()
        pipeline_submit._run_api_submit(client, [db.get_lead(lead_id)])
        return client

    def test_it_goes_back_to_pending_review(self):
        lead_id = self._lead(country=None, state=None)

        self._run_submit(lead_id)

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'pending_review')
        self.assertIn('Country is missing', lead['submit_error'])

    def test_oracle_is_never_contacted(self):
        lead_id = self._lead(country=None, state=None)

        client = self._run_submit(lead_id)

        client.submit_lead.assert_not_called()

    def test_it_appears_on_pending_review_with_the_reason(self):
        lead_id = self._lead(country=None, state=None)
        self._run_submit(lead_id)

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/').get_data(as_text=True)

        self.assertIn('benemrick@eaton.com', page)
        self.assertIn('Not sent to Oracle', page)
        self.assertIn('Country is missing', page)

    def test_it_does_not_land_in_history(self):
        lead_id = self._lead(country=None, state=None)
        self._run_submit(lead_id)

        history_statuses = [
            'approved', 'submitting', 'submission_unknown',
            'submitted', 'failed', 'rejected', 'duplicate',
        ]
        ids = [l['id'] for l in db.list_leads(status=history_statuses)]

        self.assertNotIn(lead_id, ids)

    def test_a_stale_review_note_does_not_ride_along(self):
        # The review card prefills its notes box from review_notes, so a note
        # left on the previous approval would read as one a reviewer just wrote.
        lead_id = self._lead(country=None, state=None)
        db.update_lead_fields(lead_id, {
            'review_notes': 'Looks fine, sending.', 'reviewed_by': 'dashboard',
        })

        self._run_submit(lead_id)

        lead = db.get_lead(lead_id)
        self.assertIsNone(lead['review_notes'])
        self.assertIsNone(lead['reviewed_by'])

    def test_an_unknown_outcome_is_never_returned_automatically(self):
        # Oracle may have created the record; a human has to reconcile it
        # before anything is retried, so this must NOT go back.
        lead_id = self._lead()
        db.mark_submission_unknown(lead_id, 'Connection reset after POST.')

        self.assertEqual(db.get_lead(lead_id)['status'], 'submission_unknown')


class RecallFromQueueTests(_Base):
    """An approved lead that cannot go out must not become a dead end.

    'approved' is not in lead_stages.EDITABLE, so the lead page shows no form;
    the lead is not on Pending Review, so it has no review card; and the
    queue's other escape hatch (check_oracle) needs 'submitting'. A lead
    approved without a Territory Manager could therefore be opened but never
    corrected -- the field that would fix it was uneditable everywhere.
    """

    def _detail(self, lead_id):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            return self.client.get(f'/leads/{lead_id}').get_data(as_text=True)

    def test_the_lead_page_offers_a_way_back(self):
        lead_id = self._lead(status='approved', assigned_tm=None)

        self.assertIn('Take back to review', self._detail(lead_id))

    def test_the_queue_offers_a_way_back(self):
        self._lead(status='approved', assigned_tm=None)

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/queue').get_data(as_text=True)

        self.assertIn('Take back to review', page)

    def test_the_queue_says_why_it_will_never_be_sent(self):
        # It reads as "waiting for the submission worker" otherwise, when in
        # fact no worker will ever send it.
        self._lead(status='approved', assigned_tm=None)

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/queue').get_data(as_text=True)

        self.assertIn('cannot be sent', page)
        self.assertIn('Territory Manager is unassigned', page)

    def test_recalling_it_makes_it_editable_again(self):
        lead_id = self._lead(status='approved', assigned_tm=None)

        self._post(f'/leads/{lead_id}/recall')

        self.assertEqual(db.get_lead(lead_id)['status'], 'pending_review')
        self.assertIn('name="assigned_tm"', self._detail(lead_id))

    def test_it_lands_on_the_page_the_fix_is_made_on(self):
        lead_id = self._lead(status='approved', assigned_tm=None)

        response = self._post(f'/leads/{lead_id}/recall')

        self.assertEqual(response.status_code, 302)
        self.assertIn(f'/leads/{lead_id}', response.headers['Location'])

    def test_the_manager_can_then_be_set_and_saved(self):
        lead_id = self._lead(status='approved', assigned_tm=None)
        self._post(f'/leads/{lead_id}/recall')

        self._post(f'/leads/{lead_id}/save', assigned_tm='Mark Copeland')

        self.assertEqual(db.get_lead(lead_id)['assigned_tm'], 'Mark Copeland')

    def test_manager_field_is_a_dropdown_with_every_canonical_tm(self):
        lead_id = self._lead(status='pending_review', assigned_tm='Wade Kahout')
        parser = _AssignedTmSelectParser()
        parser.feed(self._detail(lead_id))

        self.assertEqual(
            [value for value, _selected in parser.options],
            ['', *territory_map.TERRITORY_MANAGERS],
        )
        self.assertIn(('Wade Kahout', True), parser.options)

    def test_manager_save_rejects_wrong_capitalization(self):
        lead_id = self._lead(status='pending_review', assigned_tm='Wade Kahout')

        response = self._post(
            f'/leads/{lead_id}/save', assigned_tm='wade kahout'
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.get_lead(lead_id)['assigned_tm'], 'Wade Kahout')

    def test_a_lead_a_run_is_sending_is_never_taken_back(self):
        # It may be mid-POST; its way out is check_oracle, which asks Oracle
        # what happened first.
        lead_id = self._lead(status='submitting')

        response = self._post(f'/leads/{lead_id}/recall')

        self.assertEqual(response.status_code, 409)
        self.assertEqual(db.get_lead(lead_id)['status'], 'submitting')

    def test_a_lead_claimed_after_the_page_was_drawn_is_not_yanked_back(self):
        # The button was rendered while the lead was still 'approved'; a run
        # claimed it before the click landed.
        lead_id = self._lead(status='approved')
        db.claim_lead_for_submission(lead_id)

        self.assertFalse(db.recall_approved_lead(lead_id))
        self.assertEqual(db.get_lead(lead_id)['status'], 'submitting')

    def test_the_review_stamp_does_not_ride_along(self):
        # Same reasoning as restore_lead_to_review(): the review card prefills
        # its notes box, so a stale note would read as one just written.
        lead_id = self._lead(status='approved', assigned_tm=None)
        db.update_lead_fields(lead_id, {
            'review_notes': 'Looks fine, sending.', 'reviewed_by': 'dashboard',
        })

        self._post(f'/leads/{lead_id}/recall')

        lead = db.get_lead(lead_id)
        self.assertIsNone(lead['review_notes'])
        self.assertIsNone(lead['reviewed_by'])


class SubmitNowIsGatedTests(_Base):
    """"Send to Oracle now" acts on leads that are *already* approved, so it
    has to run the same check the approval did -- otherwise it is the one
    button that can park an unsendable lead in the queue."""

    def test_an_unsendable_approved_lead_goes_back_for_correction(self):
        lead_id = self._lead(status='approved', assigned_tm=None)

        with patch('dashboard.app._start_submit_run', return_value=True):
            self._post('/queue/submit_now')

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'pending_review')
        self.assertIn('Territory Manager is unassigned', lead['submit_error'])

    def test_no_run_is_started_for_it(self):
        self._lead(status='approved', assigned_tm=None)

        with patch('dashboard.app._start_submit_run', return_value=True) as run:
            self._post('/queue/submit_now')

        run.assert_not_called()

    def test_a_complete_lead_still_goes(self):
        lead_id = self._lead(status='approved')

        with patch('dashboard.app._start_submit_run', return_value=True) as run:
            self._post('/queue/submit_now')

        self.assertTrue(run.called)
        self.assertEqual(db.get_lead(lead_id)['status'], 'approved')


class ReturnedLeadAlertTests(_Base):
    def test_the_bell_reports_it(self):
        lead_id = self._lead(country=None, state=None)
        db.return_lead_for_correction(lead_id, 'Submission blocked: Country is missing.')

        alerts = db.list_submission_alerts()

        self.assertEqual([a['id'] for a in alerts], [lead_id])

    def test_the_headline_says_it_is_waiting_not_lost(self):
        lead_id = self._lead(country=None, state=None)
        db.return_lead_for_correction(lead_id, 'Submission blocked: Country is missing.')

        alert = _build_alert(db.get_lead(lead_id))

        self.assertIn('Returned to Pending Review', alert['headline'])
        self.assertIn('Country is missing', alert['detail'])
        # Amber, not red: the lead is fine and waiting, nothing was lost.
        self.assertEqual(alert['severity'], 'warning')

    def test_the_alert_can_be_dismissed(self):
        # list_submission_alerts() and dismiss_all_lead_alerts() have to match
        # arm for arm, or the bell keeps a count nothing can clear.
        lead_id = self._lead(country=None, state=None)
        db.return_lead_for_correction(lead_id, 'Submission blocked: Country is missing.')

        cleared = db.dismiss_all_lead_alerts()

        self.assertEqual(cleared, 1)
        self.assertEqual(db.list_submission_alerts(), [])

    def test_an_ordinary_pending_lead_raises_nothing(self):
        self._lead(status='pending_review')

        self.assertEqual(db.list_submission_alerts(), [])


class AmbiguousCompanyAssignmentTests(_Base):
    """"Assign to <account>" has to end the ambiguous-match block, not restage it.

    An Oracle account blocked a lead as ambiguous, the operator picked it, and
    the lead came straight back with the same block -- round and round. Two
    reasons: the account name was read out of the warning text with a regex
    that stopped at the first bracket, so 'LOCKHEED MARTIN (1389119)' was
    written onto the lead as 'LOCKHEED MARTIN (1389119', which matches no
    account and resembles exactly the one it was cut from; and the lead was
    left staged in the Input Queue while a submit run that only looks at
    'approved' was started for it.
    """

    BLOCK = (
        "Submission blocked: Company 'LOCKHEED MARTIN' resembles 1 existing Oracle "
        "account(s) ('LOCKHEED MARTIN (1389119)'). Choose whether to assign to an "
        "existing company or create as a new company."
    )

    def _blocked_lead(self, **overrides):
        lead_id = self._lead(status='approved', organization='LOCKHEED MARTIN', **overrides)
        db.return_lead_for_correction(lead_id, self.BLOCK)
        return lead_id

    def test_a_bracketed_account_name_survives_being_read_off_the_warning(self):
        self.assertEqual(
            _extract_company_candidates(self.BLOCK), ['LOCKHEED MARTIN (1389119)'],
        )

    def test_an_account_name_holding_a_comma_is_one_candidate_not_two(self):
        text = ("Company 'Acme' resembles 1 existing Oracle account(s) "
                "('ACME, INC (77)'). Choose whether to assign")

        self.assertEqual(_extract_company_candidates(text), ['ACME, INC (77)'])

    def test_an_apostrophe_in_a_name_is_read_from_its_double_quotes(self):
        # repr() quotes "O'BRIEN MEDICAL" with double quotes, and O'Brien is a
        # realistic customer.
        text = ("""Company 'OBrien' resembles 1 existing Oracle """
                """account(s) ("O'BRIEN MEDICAL"). Choose whether to assign""")

        self.assertEqual(_extract_company_candidates(text), ["O'BRIEN MEDICAL"])

    def test_the_name_written_onto_the_lead_is_the_one_oracle_holds(self):
        lead_id = self._blocked_lead()

        with patch('dashboard.app._start_submit_run', return_value=True):
            self._post(f'/leads/{lead_id}/assign_company',
                       company_name='LOCKHEED MARTIN (1389119)')

        self.assertEqual(db.get_lead(lead_id)['organization'], 'LOCKHEED MARTIN (1389119)')

    def test_the_lead_is_approved_not_left_staged(self):
        # 'review_approved' sits in the Input Queue and pipeline_submit only
        # ever claims 'approved', so the run started for it did nothing.
        lead_id = self._blocked_lead()

        with patch('dashboard.app._start_submit_run', return_value=True):
            self._post(f'/leads/{lead_id}/assign_company',
                       company_name='LOCKHEED MARTIN (1389119)')

        self.assertEqual(db.get_lead(lead_id)['status'], 'approved')

    def test_a_submit_run_is_started_for_it(self):
        lead_id = self._blocked_lead()

        with patch('dashboard.app._start_submit_run', return_value=True) as run:
            self._post(f'/leads/{lead_id}/assign_company',
                       company_name='LOCKHEED MARTIN (1389119)')

        self.assertTrue(run.called)

    def test_the_old_block_does_not_follow_it_forward(self):
        lead_id = self._blocked_lead()

        with patch('dashboard.app._start_submit_run', return_value=True):
            self._post(f'/leads/{lead_id}/assign_company',
                       company_name='LOCKHEED MARTIN (1389119)')

        lead = db.get_lead(lead_id)
        self.assertIsNone(lead['submit_error'])
        self.assertEqual(lead['force_submit'], 0)
        self.assertIsNone(_build_alert(lead))

    def test_a_lead_already_in_the_queue_is_reassigned_in_place(self):
        # The button is on the Input Queue too, where a failed lead keeps its
        # error; approve_lead() refuses that status, which left the rename
        # applied and the lead parked.
        lead_id = self._lead(status='failed', organization='LOCKHEED MARTIN')
        db.mark_lead_failed(lead_id, self.BLOCK)

        with patch('dashboard.app._start_submit_run', return_value=True):
            self._post(f'/leads/{lead_id}/assign_company',
                       company_name='LOCKHEED MARTIN (1389119)')

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'approved')
        self.assertEqual(lead['organization'], 'LOCKHEED MARTIN (1389119)')

    def test_a_lead_a_run_is_sending_is_not_renamed_underneath_it(self):
        # It may be mid-POST; changing the company now is the one edit that
        # cannot be taken back.
        lead_id = self._lead(status='submitting', organization='LOCKHEED MARTIN')

        with patch('dashboard.app._start_submit_run', return_value=True) as run:
            self._post(f'/leads/{lead_id}/assign_company',
                       company_name='LOCKHEED MARTIN (1389119)')

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'submitting')
        self.assertEqual(lead['organization'], 'LOCKHEED MARTIN')
        run.assert_not_called()

    def test_an_assignment_onto_an_unsendable_lead_goes_back_for_correction(self):
        # Same gate as every other route to Oracle: the rename does not make a
        # lead with no territory sendable.
        lead_id = self._blocked_lead(assigned_tm=None)

        with patch('dashboard.app._start_submit_run', return_value=True) as run:
            self._post(f'/leads/{lead_id}/assign_company',
                       company_name='LOCKHEED MARTIN (1389119)')

        lead = db.get_lead(lead_id)
        self.assertEqual(lead['status'], 'pending_review')
        self.assertIn('Territory Manager is unassigned', lead['submit_error'])
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
