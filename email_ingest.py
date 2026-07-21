"""
Microsoft Graph email ingestion.

Fetches new inbox messages since the last run (via Graph's delta query, so
each run only sees mail that arrived since the previous one) and downloads
any Excel attachments. Leaves body-text lead extraction to lead_extractor.py
and orchestration (dedup, DB writes, fraud screening) to pipeline_ingest.py --
this module only talks to Graph.
"""

import base64
import re
from pathlib import Path

import requests

import db
import graph_auth

GRAPH_BASE = 'https://graph.microsoft.com/v1.0'
ATTACHMENT_DIR = Path(__file__).parent / 'email_attachments'

_DELTA_LINK_KEY = 'graph_delta_link'
_INITIAL_DELTA_URL = (
    f'{GRAPH_BASE}/me/mailFolders/inbox/messages/delta'
    '?$select=id,receivedDateTime,from,subject,body,hasAttachments'
)


class GraphAuthError(RuntimeError):
    """Raised when there's no valid Graph token -- distinct from a Graph API
    error, so callers (pipeline_ingest.py) can tell "auth is broken, stop and
    flag it" apart from "one request failed, maybe log and continue"."""


def _headers():
    token = graph_auth.get_access_token()
    if not token:
        raise GraphAuthError('No valid Graph token -- run: py graph_auth.py --login')
    return {'Authorization': f'Bearer {token}'}


def _strip_html(html):
    """
    Small, dependency-free HTML->text fallback for message bodies that come
    back as HTML (Graph returns body.contentType of 'html' or 'text'; most
    real mail is HTML). Not a full parser -- good enough for readable inquiry
    text. lead_extractor.py's AI extraction is tolerant of leftover noise.
    """
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def fetch_new_messages():
    """
    Returns a list of dicts: {id, received_at, sender_email, sender_name,
    subject, body_text, has_attachments}.

    Persists the Graph delta link in db.run_state so repeated runs only see
    genuinely new mail. Falls back to a fresh (non-delta) baseline if the
    stored delta link has gone stale -- Graph signals this with HTTP 410 Gone.
    """
    url = db.get_run_state(_DELTA_LINK_KEY) or _INITIAL_DELTA_URL
    headers = _headers()
    messages = []

    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 410:
            db.set_run_state(_DELTA_LINK_KEY, '')
            url = _INITIAL_DELTA_URL
            continue
        resp.raise_for_status()
        payload = resp.json()

        for item in payload.get('value', []):
            body = item.get('body') or {}
            body_text = body.get('content') or ''
            if (body.get('contentType') or '').lower() == 'html':
                body_text = _strip_html(body_text)

            sender = (item.get('from') or {}).get('emailAddress') or {}
            messages.append({
                'id': item['id'],
                'received_at': item.get('receivedDateTime'),
                'sender_email': sender.get('address'),
                'sender_name': sender.get('name'),
                'subject': item.get('subject'),
                'body_text': body_text,
                'has_attachments': bool(item.get('hasAttachments')),
            })

        if '@odata.nextLink' in payload:
            url = payload['@odata.nextLink']
        elif '@odata.deltaLink' in payload:
            db.set_run_state(_DELTA_LINK_KEY, payload['@odata.deltaLink'])
            url = None
        else:
            url = None

    return messages


def download_excel_attachments(message_id):
    """Returns local file paths for any .xlsx/.xls attachments on the given
    message. Non-Excel attachments (images, PDFs, etc.) are skipped."""
    headers = _headers()
    resp = requests.get(f'{GRAPH_BASE}/me/messages/{message_id}/attachments',
                         headers=headers, timeout=30)
    resp.raise_for_status()

    ATTACHMENT_DIR.mkdir(exist_ok=True)
    paths = []
    for att in resp.json().get('value', []):
        name = att.get('name') or ''
        if not name.lower().endswith(('.xlsx', '.xls')):
            continue
        content_bytes = att.get('contentBytes')
        if not content_bytes:
            continue
        path = ATTACHMENT_DIR / f'{message_id}_{name}'
        path.write_bytes(base64.b64decode(content_bytes))
        paths.append(str(path))
    return paths


if __name__ == '__main__':
    try:
        msgs = fetch_new_messages()
        print(f'{len(msgs)} new message(s).')
        for m in msgs[:5]:
            print(f"  {m['received_at']}  {m['sender_email']}  {m['subject']!r}"
                  f"  attachments={m['has_attachments']}")
    except RuntimeError as e:
        # Covers both GraphAuthError (no cached token) and the config-missing
        # RuntimeError graph_auth.py raises when client_id/tenant_id aren't set yet.
        print(f'ERROR: {e}')
