"""
Local lead-review dashboard.
Run:  py dashboard/app.py          (dev, Flask's own server)
      py -m dashboard.serve        (production-ish, via waitress) -- added later

Binds to 127.0.0.1 only -- single-user local tool, not exposed on the network.
"""

import json
import os
import secrets
import sys
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import email_cleaner  # noqa: E402
import review_marking  # noqa: E402

app = Flask(__name__)
app.secret_key = os.getenv('DASHBOARD_SECRET_KEY') or secrets.token_hex(32)

RISK_ORDER = {'reject': 0, 'review': 1, 'clear': 2, None: 1}


def _parse_reasons(lead):
    raw = lead.get('fraud_reasons')
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _decorate(lead):
    """
    Adds display-only derived fields for the lead-info table. Deliberately
    does NOT surface the raw email/form body text anywhere -- the "Details"
    row runs lead_details through email_cleaner (strips tracking/SafeLinks
    URLs, mailto links, embedded-image references, security/automated-system
    banners, and disclaimers -- see email_cleaner.py) and then condenses it
    with paragraph breaks preserved (see condense_paragraphs), so nothing
    resembling the original email or web-form layout is ever rendered -- just
    the actual content, with the message and signature visibly separated.

    pipeline_ingest.py now cleans lead_details the same way *before* it's ever
    stored (so the dashboard and what actually gets typed into Oracle match --
    they didn't for a while, which was a real bug, not a design choice: the
    dashboard showed a cleaned preview while the raw/dirty version was what
    actually got submitted). The clean_email_body() call here is a defensive
    backstop for leads from before that fix, or any future source that skips
    pipeline_ingest.py -- harmless no-op on text that's already clean.
    """
    lead = dict(lead)
    lead['reasons'] = _parse_reasons(lead)
    lead['display_name'] = (
        f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip()
        or lead.get('organization') or '(no name)'
    )
    location_parts = [p for p in (lead.get('city'), lead.get('state'), lead.get('country')) if p]
    lead['location'] = ', '.join(location_parts) if location_parts else None
    lead['details_flat'] = None
    if lead.get('lead_details'):
        cleaned = email_cleaner.clean_email_body(lead['lead_details'])
        lead['details_flat'] = email_cleaner.condense_paragraphs(cleaned) if cleaned else None
    lead['email_thread'] = db.get_lead_email_thread(lead['id'])
    lead['has_followup'] = len(lead['email_thread']) > 1
    return lead


def _csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


@app.before_request
def protect_post_requests():
    if request.method != 'POST':
        return None
    supplied = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    if not supplied or not secrets.compare_digest(supplied, session.get('_csrf_token', '')):
        abort(400, 'Invalid or missing CSRF token')


@app.context_processor
def inject_counts():
    token = _csrf_token()
    try:
        return {
            'staged_count': db.count_leads('review_approved'),
            'ready_count': db.count_leads('approved'),
            'pending_count': db.count_leads('pending_review'),
            'csrf_token': token,
        }
    except Exception:
        return dict(staged_count=0, ready_count=0, pending_count=0, csrf_token=token)


@app.route('/')
def index():
    leads = db.list_leads(status='pending_review')
    leads = [_decorate(l) for l in leads]
    leads.sort(key=lambda l: RISK_ORDER.get(l.get('fraud_risk'), 1))
    return render_template('index.html', leads=leads)


@app.route('/queue')
def queue():
    staged_leads = db.list_leads(status='review_approved')
    staged_leads = [_decorate(l) for l in staged_leads]
    staged_leads.sort(key=lambda l: l.get('created_at') or '', reverse=True)

    ready_leads = db.list_leads(status='approved')
    ready_leads = [_decorate(l) for l in ready_leads]
    ready_leads.sort(key=lambda l: l.get('created_at') or '', reverse=True)

    return render_template('queue.html', leads=staged_leads, ready_leads=ready_leads)


@app.route('/queue/approve_all', methods=['POST'])
def queue_approve_all():
    staged_leads = db.list_leads(status='review_approved')
    db.finalize_approved_batch(reviewed_by='dashboard')
    for lead in staged_leads:
        review_marking.maybe_mark_email_reviewed(lead['id'])
    import subprocess
    subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent.parent / 'pipeline_submit.py')])
    return redirect(url_for('history'))


@app.route('/queue/<int:lead_id>/unstage', methods=['POST'])
def queue_unstage(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] != 'review_approved':
        abort(409, 'Lead is not staged')
    db.unstage_lead(lead_id)
    return redirect(url_for('queue'))


@app.route('/leads/<int:lead_id>')
def lead_detail(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return 'Lead not found', 404
    lead = _decorate(lead)
    duplicate_of = None
    if lead.get('duplicate_of_lead_id'):
        duplicate_of = db.get_lead(lead['duplicate_of_lead_id'])
    return render_template('lead_detail.html', lead=lead, duplicate_of=duplicate_of,
                            schema_fields=db.LEAD_SCHEMA_FIELDS)


@app.route('/leads/<int:lead_id>/save', methods=['POST'])
def save_lead(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    if lead['status'] not in ('pending_review', 'review_approved'):
        abort(409, 'This lead is no longer editable')
    edited = {}
    for field in db.LEAD_SCHEMA_FIELDS:
        if field in request.form:
            edited[field] = request.form.get(field) or None
    if 'review_notes' in request.form:
        edited['review_notes'] = request.form.get('review_notes') or None
    db.update_lead_fields(lead_id, edited)
    lead = db.get_lead(lead_id)
    if lead and lead.get('status') == 'review_approved':
        return redirect(url_for('queue'))
    return redirect(url_for('index'))


@app.route('/leads/<int:lead_id>/approve', methods=['POST'])
def approve(lead_id):
    edited = {}
    for field in db.LEAD_SCHEMA_FIELDS:
        if field in request.form:
            edited[field] = request.form.get(field) or None
    # Stages lead under 'review_approved' so user can review in /queue before final batch submission
    changed = db.approve_lead(
        lead_id,
        reviewed_by=request.form.get('reviewed_by') or 'dashboard',
        review_notes=request.form.get('review_notes') or None,
        edited_fields=edited,
        status='review_approved',
    )
    if not changed:
        abort(409, 'Lead is no longer awaiting review')
    return redirect(url_for('index'))


@app.route('/leads/<int:lead_id>/reject', methods=['POST'])
def reject(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in ('pending_review', 'review_approved'):
        abort(409, 'Lead is no longer awaiting review')
    db.reject_lead(
        lead_id,
        reviewed_by=request.form.get('reviewed_by') or 'dashboard',
        review_notes=request.form.get('review_notes') or None,
    )
    review_marking.maybe_mark_email_reviewed(lead_id)
    return redirect(url_for('index'))


@app.route('/leads/<int:lead_id>/restore', methods=['POST'])
def restore(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in ('rejected', 'failed'):
        abort(409, 'Lead cannot be restored from its current state')
    db.restore_lead_to_review(lead_id)
    return redirect(url_for('history'))


@app.route('/leads/<int:lead_id>/unknown-confirmed', methods=['POST'])
def unknown_confirmed(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] != 'submission_unknown':
        abort(409, 'Lead does not have an uncertain submission outcome')
    db.mark_lead_submitted(
        lead_id, lead.get('oracle_project_name') or 'Manually confirmed in Oracle'
    )
    return redirect(url_for('history'))


@app.route('/leads/<int:lead_id>/unknown-retry', methods=['POST'])
def unknown_retry(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] != 'submission_unknown':
        abort(409, 'Lead does not have an uncertain submission outcome')
    db.schedule_lead_retry(
        lead_id, 'Operator checked Oracle and explicitly authorized a retry.',
        delay_minutes=0,
    )
    return redirect(url_for('history'))


@app.route('/history')
def history():
    leads = db.list_leads(status=[
        'approved', 'submitting', 'submission_unknown',
        'submitted', 'failed', 'rejected', 'duplicate',
    ])
    leads = [_decorate(l) for l in leads]
    return render_template('history.html', leads=leads)


@app.route('/status')
def status():
    keys = [
        'graph_delta_link', 'outlook_last_checked_at',
        'last_ingest_run_at', 'last_submit_run_at', 'oracle_session_status',
    ]
    state = {k: db.get_run_state(k) for k in keys}
    counts = {}
    for s in db.LEAD_STATUSES:
        counts[s] = db.count_leads(status=s)
    return render_template(
        'status.html', state=state, counts=counts,
        message_counts=db.count_messages_by_status(),
    )


if __name__ == '__main__':
    db.init_db()
    with open(Path(__file__).resolve().parent.parent / 'config.json') as f:
        dash_config = json.load(f).get('dashboard', {})
    app.run(
        host=dash_config.get('host', '127.0.0.1'),
        port=dash_config.get('port', 5151),
        debug=bool(dash_config.get('debug', False)),
    )
