"""
Local lead-review dashboard.
Run:  py dashboard/app.py          (dev, Flask's own server)
      py -m dashboard.serve        (production-ish, via waitress) -- added later

Binds to 127.0.0.1 only -- single-user local tool, not exposed on the network.
"""

import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import email_cleaner  # noqa: E402
import outlook_ingest  # noqa: E402
import review_marking  # noqa: E402

app = Flask(__name__)
app.secret_key = os.getenv('DASHBOARD_SECRET_KEY') or secrets.token_hex(32)

RISK_ORDER = {'reject': 0, 'review': 1, 'clear': 2, None: 1}
EMAIL_ADDRESS_RE = re.compile(r'^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$')


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

    # Only an http(s) URL becomes a clickable link in the template. The field
    # is editable on the review form, so this decides here rather than trusting
    # whatever is stored -- a "javascript:" value typed into that box would
    # otherwise become a live link on the reviewer's own dashboard.
    # Metadata only, and rendered as inert text -- the files are never stored
    # or served. A customer's attachment is a stranger's file; describing it is
    # useful, hosting it would make the dashboard a delivery mechanism for it.
    lead['attachments'] = db.get_message_attachments(lead.get('message_id'))
    source_message = db.get_message(lead.get('message_id')) if lead.get('message_id') else None
    lead['translated_from'] = (
        source_message.get('original_language')
        if source_message and source_message.get('translated_body_cache_path') else None
    )

    website = (lead.get('website') or '').strip()
    lead['website_url'] = website if website.lower().startswith(('http://', 'https://')) else None

    # Extract & format the date the email was received / sent
    raw_date = None
    if lead['email_thread'] and lead['email_thread'][0].get('received_at'):
        raw_date = lead['email_thread'][0]['received_at']
    elif lead.get('created_at'):
        raw_date = lead['created_at']

    if raw_date:
        try:
            # Parse ISO date string and format nicely (e.g., Jul 23, 2026 11:08 AM)
            dt = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
            lead['received_date_formatted'] = dt.strftime('%b %d, %Y %I:%M %p')
            lead['received_date_short'] = dt.strftime('%b %d, %Y')
        except Exception:
            lead['received_date_formatted'] = raw_date[:16].replace('T', ' ')
            lead['received_date_short'] = raw_date[:10]
    else:
        lead['received_date_formatted'] = None
        lead['received_date_short'] = None

    return lead


def _parse_warnings(lead):
    raw = lead.get('field_warnings')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    return parsed if isinstance(parsed, list) else [str(parsed)]


def _build_alert(lead):
    """
    Turn a lead needing attention into a notification the operator can act on.

    'severity' drives the colour: 'blocked' means nothing new reached Oracle,
    'warning' means it did but something about it needs checking.
    """
    warnings = _parse_warnings(lead)
    name = (
        f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip()
        or lead.get('organization') or f"Lead #{lead['id']}"
    )
    status = lead.get('status')

    # Blocked locally: an equivalent lead had already gone to Oracle. Checked
    # before Oracle is contacted, because Oracle's own dedup cannot see leads
    # it has not yet assigned an owner to.
    if status == 'duplicate':
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Not submitted — duplicate of an earlier lead',
            'detail': '; '.join(warnings) or 'An equivalent lead was already sent to Oracle.',
            'organization': lead.get('organization'),
        }

    # Oracle's pre-submission dedup matched an existing lead, so nothing new
    # was created -- the case that used to leave no trace on the dashboard.
    existing = next((w for w in warnings if 'already exists' in w.lower()), None)
    if status == 'submitted' and existing:
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Not submitted — already in Oracle',
            'detail': existing,
            'organization': lead.get('organization'),
        }
    if status == 'failed':
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Submission failed',
            'detail': lead.get('submit_error') or 'No error detail recorded.',
            'organization': lead.get('organization'),
        }
    if status == 'submission_unknown':
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Outcome unknown — check Oracle',
            'detail': lead.get('submit_error') or 'No error detail recorded.',
            'organization': lead.get('organization'),
        }
    if warnings:
        # An ambiguous company match is the one warning that asks the operator
        # to go and do something in Oracle, so name it rather than burying it
        # under the generic heading.
        ambiguous = any('resembles' in w and 'existing Oracle' in w for w in warnings)
        # A rejected website field is a configuration fault, not a fact about
        # this lead: it repeats on every lead until oracle.website_field is
        # fixed. Naming it says what to go and change, instead of leaving the
        # operator to read the same generic warning off a dozen leads.
        website_rejected = any('website field' in w.lower() for w in warnings)
        if ambiguous:
            headline = 'Possible duplicate account — check Oracle'
        elif website_rejected:
            headline = 'Website field rejected — check config.json'
        else:
            headline = 'Submitted with warnings'
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'warning',
            'headline': headline,
            'detail': '; '.join(warnings),
            'organization': lead.get('organization'),
        }
    return None


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
        alerts = [a for a in (_build_alert(l) for l in db.list_submission_alerts()) if a]
        return {
            'staged_count': db.count_leads('review_approved'),
            'ready_count': db.count_leads('approved'),
            'pending_count': db.count_leads('pending_review'),
            'alerts': alerts,
            'alert_count': len(alerts),
            'blocked_count': sum(1 for a in alerts if a['severity'] == 'blocked'),
            'csrf_token': token,
        }
    except Exception:
        return dict(staged_count=0, ready_count=0, pending_count=0,
                    alerts=[], alert_count=0, blocked_count=0, csrf_token=token)


@app.route('/alerts/<int:lead_id>/dismiss', methods=['POST'])
def dismiss_alert(lead_id):
    if not db.get_lead(lead_id):
        abort(404)
    db.dismiss_lead_alert(lead_id)
    return redirect(request.referrer or url_for('index'))


@app.route('/alerts/dismiss_all', methods=['POST'])
def dismiss_all_alerts():
    db.dismiss_all_lead_alerts()
    return redirect(request.referrer or url_for('index'))


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
    # Stay on the Input Queue rather than jumping to History -- the approved
    # leads show up in this page's "ready" section while the submit run works
    # through them, so there's nothing to navigate away for.
    return redirect(url_for('queue'))


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


@app.route('/leads/<int:lead_id>/forward', methods=['POST'])
def forward_lead_email(lead_id):
    """Forward the original Outlook email and dispose of the non-lead."""
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in ('pending_review', 'review_approved'):
        abort(409, 'Lead is no longer awaiting review')

    recipient = (request.form.get('recipient') or '').strip()
    if not EMAIL_ADDRESS_RE.fullmatch(recipient) or '\r' in recipient or '\n' in recipient:
        flash('Enter one valid email address.', 'forward_error')
        return redirect(url_for('lead_detail', lead_id=lead_id))

    forward_note = (request.form.get('forward_note') or '').strip()
    if len(forward_note) > 5000:
        flash('The forwarding message must be 5,000 characters or fewer.', 'forward_error')
        return redirect(url_for('lead_detail', lead_id=lead_id))

    message = db.get_message(lead.get('message_id')) if lead.get('message_id') else None
    if not message or not message.get('source_message_id'):
        flash('The original Outlook email is not available to forward.', 'forward_error')
        return redirect(url_for('lead_detail', lead_id=lead_id))

    try:
        outlook_ingest.forward_message(
            message['source_message_id'], recipient, note=forward_note or None,
            fallback_message_path=message.get('message_file_path'),
        )
    except ValueError as exc:
        flash(str(exc), 'forward_error')
        return redirect(url_for('lead_detail', lead_id=lead_id))
    except outlook_ingest.OutlookForwardError as exc:
        app.logger.exception('Could not forward source email for lead %s', lead_id)
        flash(
            f'Outlook failed while {exc.stage}. The email was not sent and the '
            'lead is still pending.',
            'forward_error',
        )
        return redirect(url_for('lead_detail', lead_id=lead_id))
    except Exception:
        app.logger.exception('Could not forward source email for lead %s', lead_id)
        flash(
            'Outlook could not send the email. Check that Outlook is open and '
            'signed in, then try again.',
            'forward_error',
        )
        return redirect(url_for('lead_detail', lead_id=lead_id))

    db.reject_lead(
        lead_id,
        reviewed_by='dashboard',
        review_notes=f'Original email forwarded to {recipient}; not processed as a lead.',
    )
    review_marking.maybe_mark_email_reviewed(lead_id)
    flash(f'Email forwarded to {recipient}.', 'forward_success')
    return redirect(url_for('lead_detail', lead_id=lead_id, forwarded=1))


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
