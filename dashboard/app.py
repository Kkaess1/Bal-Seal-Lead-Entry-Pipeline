"""
Local lead-review dashboard.
Run:  py dashboard/app.py          (dev, Flask's own server)
      py -m dashboard.serve        (production-ish, via waitress) -- added later

Binds to 127.0.0.1 only -- single-user local tool, not exposed on the network.
"""

import json
import sys
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import email_cleaner  # noqa: E402
import review_marking  # noqa: E402

app = Flask(__name__)

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
    return lead


@app.route('/')
def index():
    leads = db.list_leads(status='pending_review')
    leads = [_decorate(l) for l in leads]
    leads.sort(key=lambda l: RISK_ORDER.get(l.get('fraud_risk'), 1))
    return render_template('index.html', leads=leads)


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


@app.route('/leads/<int:lead_id>/approve', methods=['POST'])
def approve(lead_id):
    edited = {}
    for field in db.LEAD_SCHEMA_FIELDS:
        if field in request.form:
            edited[field] = request.form.get(field) or None
    db.approve_lead(
        lead_id,
        reviewed_by=request.form.get('reviewed_by') or 'dashboard',
        review_notes=request.form.get('review_notes') or None,
        edited_fields=edited,
    )
    review_marking.maybe_mark_email_reviewed(lead_id)
    return redirect(url_for('index'))


@app.route('/leads/<int:lead_id>/reject', methods=['POST'])
def reject(lead_id):
    db.reject_lead(
        lead_id,
        reviewed_by=request.form.get('reviewed_by') or 'dashboard',
        review_notes=request.form.get('review_notes') or None,
    )
    review_marking.maybe_mark_email_reviewed(lead_id)
    return redirect(url_for('index'))


@app.route('/history')
def history():
    leads = db.list_leads(status=['approved', 'submitted', 'failed', 'rejected', 'duplicate'])
    leads = [_decorate(l) for l in leads]
    return render_template('history.html', leads=leads)


@app.route('/status')
def status():
    keys = ['graph_delta_link', 'last_ingest_run_at', 'last_submit_run_at', 'oracle_session_status']
    state = {k: db.get_run_state(k) for k in keys}
    counts = {}
    for s in db.LEAD_STATUSES:
        counts[s] = len(db.list_leads(status=s))
    return render_template('status.html', state=state, counts=counts)


if __name__ == '__main__':
    db.init_db()
    with open(Path(__file__).resolve().parent.parent / 'config.json') as f:
        dash_config = json.load(f).get('dashboard', {})
    app.run(
        host=dash_config.get('host', '127.0.0.1'),
        port=dash_config.get('port', 5151),
        debug=True,
    )
