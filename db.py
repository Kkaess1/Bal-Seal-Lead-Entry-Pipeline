"""
SQLite data layer for the automated lead pipeline.
Plain sqlite3, no ORM — mirrors the flat function-based style of excel_parser.py etc.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / 'pipeline.db'

# Where get_lead_email_attachment() writes its last-resort reconstructed email
# file. Shares pipeline_ingest.py's cache directory (gitignored -- these files
# contain lead PII). Module-level so tests can redirect it.
EMAIL_CACHE_DIR = Path(__file__).parent / 'email_cache'

# Fields copied 1:1 from excel_parser.py's lead schema so leads can be handed
# straight to oracle_api.build_lead_payload() regardless of source. `assigned_tm`
# and `market` are additions beyond that original schema -- they feed Oracle's
# "Owner" and "Market" fields (via territory_map.assign_tm() and
# lead_extractor.py's market classification, respectively) and need to travel
# with the lead the same way every other Oracle-bound field does.
LEAD_SCHEMA_FIELDS = [
    'first_name', 'last_name', 'organization', 'title', 'phone', 'phone_mobile',
    'email', 'website', 'address1', 'address2', 'city', 'state', 'zip', 'country',
    'notes_action', 'notes_body', 'survey_answers', 'qualifiers', 'rank',
    'scan_device', 'lead_details', 'campaign', 'assigned_tm', 'market',
]

LEAD_STATUSES = (
    'ingested', 'extracted', 'screened', 'pending_review',
    'review_approved', 'approved', 'submitting', 'submission_unknown',
    'rejected', 'submitted', 'failed', 'duplicate',
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path=None):
    # Resolved at call time, NOT bound as a default argument. As a default,
    # db_path=DB_PATH captured the module-level value at import, so tests that
    # point db.DB_PATH at a temp file were silently still connecting to the
    # real pipeline.db -- including their tearDown's DELETE FROM leads.
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 30000')
    return conn


def init_db(db_path=None):
    conn = get_connection(db_path)
    try:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS messages (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id     TEXT UNIQUE NOT NULL,
                conversation_id       TEXT,
                ingest_batch_id       TEXT,
                received_at           TEXT,
                sender_email          TEXT,
                sender_name           TEXT,
                subject               TEXT,
                has_excel_attachment  INTEGER DEFAULT 0,
                attachments           TEXT,
                body_cache_path       TEXT,
                translated_body_cache_path TEXT,
                original_language     TEXT,
                translated_subject    TEXT,
                message_file_path     TEXT,
                status                TEXT NOT NULL DEFAULT 'new',
                error_detail          TEXT,
                processed_at          TEXT,
                attempt_count         INTEGER NOT NULL DEFAULT 0,
                last_attempt_at       TEXT,
                created_at            TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id             INTEGER REFERENCES messages(id),
                source                 TEXT NOT NULL,
                source_row_number      INTEGER,
                ingest_key             TEXT,
                dedup_key              TEXT,
                duplicate_of_lead_id   INTEGER REFERENCES leads(id),

                {', '.join(f'{f} TEXT' for f in LEAD_SCHEMA_FIELDS)},
                assigned_tm_basis      TEXT,

                extraction_confidence  REAL,
                extraction_notes       TEXT,

                fraud_risk             TEXT,
                fraud_reasons          TEXT,
                fraud_score            REAL,

                status                 TEXT NOT NULL DEFAULT 'ingested',
                reviewed_by            TEXT,
                reviewed_at            TEXT,
                review_notes           TEXT,

                submitted_at           TEXT,
                oracle_project_name    TEXT,
                submit_error           TEXT,
                submit_screenshot_path TEXT,
                submission_attempt_count INTEGER NOT NULL DEFAULT 0,
                submitting_started_at  TEXT,
                next_attempt_at        TEXT,
                oracle_record_id       TEXT,
                field_warnings         TEXT,
                alert_dismissed_at     TEXT,

                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_dedup_key ON leads(dedup_key);
            CREATE TABLE IF NOT EXISTS run_state (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute('PRAGMA journal_mode = WAL')
        conn.commit()
        _migrate_leads_columns(conn)
        _migrate_messages_columns(conn)
    finally:
        conn.close()


def _migrate_leads_columns(conn):
    existing = {row[1] for row in conn.execute('PRAGMA table_info(leads)')}
    text_fields = LEAD_SCHEMA_FIELDS + [
        'assigned_tm_basis', 'parent_lead_id', 'ingest_key',
        'submitting_started_at', 'next_attempt_at', 'oracle_record_id',
        'field_warnings', 'alert_dismissed_at',
    ]
    for field in text_fields:
        if field not in existing:
            conn.execute(f'ALTER TABLE leads ADD COLUMN {field} TEXT')
    if 'submission_attempt_count' not in existing:
        conn.execute(
            'ALTER TABLE leads ADD COLUMN submission_attempt_count INTEGER NOT NULL DEFAULT 0'
        )
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_ingest_key '
        'ON leads(ingest_key) WHERE ingest_key IS NOT NULL'
    )
    conn.commit()


def _migrate_messages_columns(conn):
    existing = {row[1] for row in conn.execute('PRAGMA table_info(messages)')}
    if 'attempt_count' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0')
    if 'last_attempt_at' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN last_attempt_at TEXT')
    if 'message_file_path' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN message_file_path TEXT')
    if 'attachments' not in existing:
        # JSON list of {name, size, kind} describing what the sender attached.
        # Metadata only -- the files themselves are deliberately never written
        # to disk or served by the dashboard. See record_attachments().
        conn.execute('ALTER TABLE messages ADD COLUMN attachments TEXT')
    if 'translated_body_cache_path' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN translated_body_cache_path TEXT')
    if 'original_language' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN original_language TEXT')
    if 'translated_subject' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN translated_subject TEXT')
    if 'conversation_id' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN conversation_id TEXT')
    if 'ingest_batch_id' not in existing:
        conn.execute('ALTER TABLE messages ADD COLUMN ingest_batch_id TEXT')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_messages_conversation '
        'ON messages(conversation_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_messages_ingest_batch '
        'ON messages(ingest_batch_id)'
    )
    conn.commit()


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def insert_message(source_message_id, received_at=None, sender_email=None,
                    sender_name=None, subject=None, has_excel_attachment=False,
                    body_cache_path=None, message_file_path=None,
                    conversation_id=None, ingest_batch_id=None, conn=None):
    """
    Insert a new message row. Returns its id, or None if source_message_id
    already exists (hard dedup on the unique constraint). source_message_id
    is whatever the active ingestion backend uses as a stable unique ID for a
    message (a Graph message ID, an Outlook COM EntryID, etc.) -- named
    source-neutrally since the backend is not tied to any one mail provider.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO messages
               (source_message_id, conversation_id, ingest_batch_id,
                received_at, sender_email, sender_name, subject,
                has_excel_attachment, body_cache_path, message_file_path,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
            (source_message_id, conversation_id, ingest_batch_id,
             received_at, sender_email, sender_name, subject,
             int(has_excel_attachment), body_cache_path, message_file_path, _now()),
        )
        if owns_conn:
            conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        if owns_conn:
            conn.close()


def get_message_by_source_id(source_message_id, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            'SELECT * FROM messages WHERE source_message_id = ?', (source_message_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def record_attachments(message_id, attachments, conn=None):
    """
    Store what the sender attached, as metadata only.

    The files themselves are deliberately never written to disk and never
    served by the dashboard: a lead's attachments are whatever a stranger
    chose to email us, and the safe thing to do with that is describe it, not
    host it. A rep who needs the actual file opens the source email in
    Outlook, which keeps the file inside the protections Outlook and the mail
    gateway already apply to it.

    `attachments` is a list of {name, size, kind} dicts.
    """
    update_message_fields(message_id, {'attachments': json.dumps(attachments or [])}, conn=conn)


def get_message_attachments(message_id, conn=None):
    """Attachment metadata for a message, or [] when there is none. Tolerates
    a malformed/legacy value rather than breaking the page that renders it."""
    if not message_id:
        return []
    message = get_message(message_id, conn=conn) or {}
    raw = message.get('attachments')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def update_message_fields(message_id, fields, conn=None):
    """Generic partial update on a message row."""
    if not fields:
        return
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        set_clause = ', '.join(f'{k} = ?' for k in fields)
        conn.execute(
            f'UPDATE messages SET {set_clause} WHERE id = ?',
            list(fields.values()) + [message_id],
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def get_message(message_id, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute('SELECT * FROM messages WHERE id = ?', (message_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def update_message_status(message_id, status, error_detail=None, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            """UPDATE messages SET status = ?, error_detail = ?, processed_at = ?
               WHERE id = ?""",
            (status, error_detail, _now(), message_id),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def set_message_file_path(message_id, message_file_path, conn=None):
    """Record where the original email was exported to (see
    outlook_ingest.save_message_file). Separate from insert_message() because
    insert is INSERT OR IGNORE -- a message ingested before this column
    existed, or before the export succeeded, is backfilled here on a later run.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            'UPDATE messages SET message_file_path = ? WHERE id = ?',
            (message_file_path, message_id),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def claim_message(message_id, conn=None):
    """Atomically claim a new/error message for processing."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            """UPDATE messages
               SET status = 'processing', error_detail = NULL,
                   attempt_count = attempt_count + 1, last_attempt_at = ?
               WHERE id = ? AND status IN ('new', 'error')""",
            (_now(), message_id),
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount == 1
    finally:
        if owns_conn:
            conn.close()


def list_messages(status=None, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        if status is None:
            rows = conn.execute('SELECT * FROM messages ORDER BY received_at, id').fetchall()
        else:
            statuses = [status] if isinstance(status, str) else list(status)
            placeholders = ', '.join('?' for _ in statuses)
            rows = conn.execute(
                f'SELECT * FROM messages WHERE status IN ({placeholders}) '
                'ORDER BY received_at, id',
                statuses,
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def count_messages_by_status(conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            'SELECT status, COUNT(*) AS count FROM messages GROUP BY status'
        ).fetchall()
        return {row['status']: row['count'] for row in rows}
    finally:
        if owns_conn:
            conn.close()


def release_stale_message_claims(max_age_minutes=90, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        cur = conn.execute(
            """UPDATE messages
               SET status = 'error',
                   error_detail = 'Previous ingestion process stopped while handling this message.'
               WHERE status = 'processing'
                 AND (last_attempt_at IS NULL OR last_attempt_at < ?)""",
            (cutoff,),
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# leads
# ---------------------------------------------------------------------------

def compute_dedup_key(lead):
    """Stable hash of (email, first+last name, organization) for soft dedup."""
    email = (lead.get('email') or '').strip().lower()
    name = f"{(lead.get('first_name') or '').strip()} {(lead.get('last_name') or '').strip()}".strip().lower()
    org = (lead.get('organization') or '').strip().lower()
    raw = json.dumps([email, name, org])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def find_duplicate_lead(dedup_key, exclude_lead_id=None, conn=None):
    """Return the most recent existing lead with the same dedup_key, if any."""
    if not dedup_key:
        return None
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        query = 'SELECT * FROM leads WHERE dedup_key = ?'
        params = [dedup_key]
        if exclude_lead_id is not None:
            query += ' AND id != ?'
            params.append(exclude_lead_id)
        query += ' ORDER BY created_at DESC LIMIT 1'
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def find_submitted_duplicate(lead_id, conn=None):
    """
    Another lead, already submitted to Oracle, that looks like the same person
    at the same company as this one (same dedup_key). Returns None if there
    isn't one.

    This is the dedup guard that actually holds. Oracle's own pre-submission
    check queries the leads collection as the API user, and that query only
    returns leads visible to them -- confirmed against this tenant on
    2026-07-30, where leads this pipeline had just created were absent from
    every query (by LeadNumber, by Name, by LIKE) while older leads with an
    assigned owner came back fine. A lead we submitted minutes ago is exactly
    the one most likely to be duplicated, and it is exactly the one Oracle
    will not show us, so the local record has to be the primary source of
    truth about what we already sent.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        lead = get_lead(lead_id, conn=conn)
        if not lead or not lead.get('dedup_key'):
            return None
        row = conn.execute(
            """SELECT * FROM leads
               WHERE dedup_key = ? AND id != ?
                 AND status = 'submitted'
                 AND oracle_record_id IS NOT NULL AND oracle_record_id != ''
               ORDER BY submitted_at DESC, id DESC LIMIT 1""",
            (lead['dedup_key'], lead_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def insert_lead(lead, source, message_id=None, source_row_number=None,
                 status='ingested', ingest_key=None, replace_existing=False,
                 conn=None):
    """
    Insert a new lead. `lead` is a dict using the LEAD_SCHEMA_FIELDS keys
    (same shape excel_parser.parse_excel() already produces).
    Automatically computes dedup_key and flags a duplicate_of_lead_id if a
    match already exists — never blocks the insert.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        if ingest_key:
            existing = conn.execute(
                'SELECT id FROM leads WHERE ingest_key = ?', (ingest_key,)
            ).fetchone()
            if existing:
                lead_id = existing['id']
                if replace_existing:
                    fields = {field: lead.get(field) for field in LEAD_SCHEMA_FIELDS}
                    fields.update({
                        'source_row_number': source_row_number,
                        'dedup_key': compute_dedup_key(lead),
                        'status': status,
                        'submit_error': None,
                    })
                    update_lead_fields(lead_id, fields, conn=conn)
                if owns_conn:
                    conn.commit()
                return lead_id

        dedup_key = compute_dedup_key(lead)
        dup = find_duplicate_lead(dedup_key, conn=conn)
        duplicate_of_lead_id = dup['id'] if dup else None

        now = _now()
        field_values = [lead.get(f) for f in LEAD_SCHEMA_FIELDS]
        columns = (
            ['message_id', 'source', 'source_row_number', 'ingest_key',
             'dedup_key', 'duplicate_of_lead_id']
            + LEAD_SCHEMA_FIELDS
            + ['status', 'created_at', 'updated_at']
        )
        values = (
            [message_id, source, source_row_number, ingest_key,
             dedup_key, duplicate_of_lead_id]
            + field_values
            + [status, now, now]
        )
        placeholders = ', '.join('?' for _ in values)
        cur = conn.execute(
            f"INSERT INTO leads ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        if owns_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def get_lead(lead_id, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute('SELECT * FROM leads WHERE id = ?', (lead_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def list_leads(status=None, conn=None):
    """List leads, optionally filtered by status (str or list of str), newest first."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        if status is None:
            rows = conn.execute('SELECT * FROM leads ORDER BY created_at DESC').fetchall()
        else:
            statuses = [status] if isinstance(status, str) else list(status)
            placeholders = ', '.join('?' for _ in statuses)
            rows = conn.execute(
                f'SELECT * FROM leads WHERE status IN ({placeholders}) ORDER BY created_at DESC',
                statuses,
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def count_leads(status=None, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        if status is None:
            return conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
        statuses = [status] if isinstance(status, str) else list(status)
        placeholders = ', '.join('?' for _ in statuses)
        return conn.execute(
            f'SELECT COUNT(*) FROM leads WHERE status IN ({placeholders})', statuses
        ).fetchone()[0]
    finally:
        if owns_conn:
            conn.close()


def list_ready_for_submission(conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        now = _now()
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE status = 'approved'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY created_at""",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def list_leads_by_message(message_id, conn=None):
    """All leads pulled from a given message row (usually one, but an Excel
    attachment can produce many leads from a single email)."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute('SELECT * FROM leads WHERE message_id = ?', (message_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def consolidate_ingest_batch_threads(ingest_batch_id, conn=None):
    """Keep one pending dashboard lead per Outlook thread in one ingest run.

    Every source message remains stored. Only lead rows for older messages in
    the same conversation and batch are removed; the newest message's lead(s)
    remain. A later batch is intentionally independent and can reintroduce the
    conversation to Pending Review.
    """
    if not ingest_batch_id:
        return 0
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conversations = conn.execute(
            """SELECT m.conversation_id
               FROM messages m JOIN leads l ON l.message_id = m.id
               WHERE m.ingest_batch_id = ? AND m.conversation_id IS NOT NULL
               GROUP BY m.conversation_id
               HAVING COUNT(DISTINCT m.id) > 1""",
            (ingest_batch_id,),
        ).fetchall()
        removed = 0
        for row in conversations:
            messages = conn.execute(
                """SELECT DISTINCT m.id, m.received_at, m.created_at
                   FROM messages m JOIN leads l ON l.message_id = m.id
                   WHERE m.ingest_batch_id = ? AND m.conversation_id = ?
                   ORDER BY COALESCE(m.received_at, m.created_at) DESC, m.id DESC""",
                (ingest_batch_id, row['conversation_id']),
            ).fetchall()
            older_ids = [m['id'] for m in messages[1:]]
            if not older_ids:
                continue
            placeholders = ', '.join('?' for _ in older_ids)
            lead_rows = conn.execute(
                f'SELECT id FROM leads WHERE message_id IN ({placeholders})', older_ids,
            ).fetchall()
            lead_ids = [lead['id'] for lead in lead_rows]
            if lead_ids:
                lead_placeholders = ', '.join('?' for _ in lead_ids)
                conn.execute(
                    f'UPDATE leads SET duplicate_of_lead_id = NULL '
                    f'WHERE duplicate_of_lead_id IN ({lead_placeholders})',
                    lead_ids,
                )
            cur = conn.execute(
                f"""DELETE FROM leads
                    WHERE message_id IN ({placeholders})
                      AND status IN ('ingested', 'extracted', 'screened', 'pending_review')""",
                older_ids,
            )
            removed += cur.rowcount
        if owns_conn:
            conn.commit()
        return removed
    finally:
        if owns_conn:
            conn.close()


def update_lead_fields(lead_id, fields, conn=None):
    """Generic partial update — fields is a dict of column: value."""
    if not fields:
        return
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        fields = dict(fields)
        fields['updated_at'] = _now()
        set_clause = ', '.join(f'{k} = ?' for k in fields)
        conn.execute(
            f'UPDATE leads SET {set_clause} WHERE id = ?',
            list(fields.values()) + [lead_id],
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def set_territory_assignment(lead_id, assigned_tm, basis, conn=None):
    """assigned_tm may be None -- that's a valid result (needs manual assignment),
    always paired with a basis explaining why, shown in the dashboard."""
    update_lead_fields(lead_id, {'assigned_tm': assigned_tm, 'assigned_tm_basis': basis}, conn=conn)


def set_extraction_result(lead_id, confidence, notes, conn=None):
    update_lead_fields(
        lead_id,
        {'extraction_confidence': confidence, 'extraction_notes': notes, 'status': 'extracted'},
        conn=conn,
    )


def set_fraud_screen_result(lead_id, fraud_risk, fraud_reasons, fraud_score, conn=None):
    """fraud_reasons: list of {tag, label, detail} dicts — stored as JSON."""
    update_lead_fields(
        lead_id,
        {
            'fraud_risk': fraud_risk,
            'fraud_reasons': json.dumps(fraud_reasons),
            'fraud_score': fraud_score,
            'status': 'pending_review',
        },
        conn=conn,
    )


def approve_lead(lead_id, reviewed_by=None, review_notes=None, edited_fields=None, status='review_approved', conn=None):
    """Mark a lead approved (defaulting to review_approved stage), optionally applying edits."""
    fields = dict(edited_fields or {})
    fields.update({
        'status': status,
        'reviewed_by': reviewed_by,
        'reviewed_at': _now(),
        'review_notes': review_notes,
    })
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        current = conn.execute('SELECT status FROM leads WHERE id = ?', (lead_id,)).fetchone()
        if not current or current['status'] not in ('pending_review', 'review_approved'):
            return False
        update_lead_fields(lead_id, fields, conn=conn)
        if owns_conn:
            conn.commit()
        return True
    finally:
        if owns_conn:
            conn.close()


def finalize_approved_batch(reviewed_by=None, conn=None):
    """Transition all staged 'review_approved' leads to final 'approved' status ready for Oracle submission."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        now = _now()
        cur = conn.execute(
            """UPDATE leads SET status = 'approved', updated_at = ?
               WHERE status = 'review_approved'""",
            (now,)
        )
        count = cur.rowcount
        if owns_conn:
            conn.commit()
        return count
    finally:
        if owns_conn:
            conn.close()


def unstage_lead(lead_id, conn=None):
    """Return a staged lead from 'review_approved' back to 'pending_review'."""
    update_lead_fields(lead_id, {'status': 'pending_review'}, conn=conn)


def reject_lead(lead_id, reviewed_by=None, review_notes=None, conn=None):
    update_lead_fields(
        lead_id,
        {'status': 'rejected', 'reviewed_by': reviewed_by, 'reviewed_at': _now(), 'review_notes': review_notes},
        conn=conn,
    )


def restore_lead_to_review(lead_id, conn=None):
    """Restore a rejected (or failed) lead back to 'pending_review' status."""
    update_lead_fields(lead_id, {'status': 'pending_review'}, conn=conn)


def get_lead_email_attachment(lead_id, conn=None):
    """
    Return the path of the file representing this lead's source email, for
    upload into the Oracle lead's Attachments section. Returns None only if
    the lead itself doesn't exist.

    Three tiers, best first:
      1. The exported .msg saved at ingest (outlook_ingest.save_message_file)
         -- the real email: formatting, headers, thread, sender's own files.
      2. The cached body .txt written by pipeline_ingest._cache_body(), for
         leads ingested before .msg export existed or when Outlook couldn't
         export the item.
      3. A .txt reconstructed from the lead's own stored fields, so a lead
         still reaches Oracle with *some* provenance attached even when both
         cached files are gone.
    """
    lead = get_lead(lead_id, conn=conn)
    if not lead:
        return None

    msg_id = lead.get('message_id')
    if msg_id:
        msg = get_message(msg_id, conn=conn) or {}
        for key in ('message_file_path', 'body_cache_path'):
            candidate = msg.get(key)
            if candidate and Path(candidate).exists():
                return str(candidate)

    # Tier 3. EMAIL_CACHE_DIR is anchored to the project directory, not the
    # process's working directory -- a Task Scheduler run starts in
    # C:\Windows\System32, where a relative path would either fail outright or
    # scatter lead PII outside the project.
    fallback_dir = Path(EMAIL_CACHE_DIR)
    fallback_dir.mkdir(parents=True, exist_ok=True)
    file_path = fallback_dir / f'lead_{lead_id}_reconstructed.txt'
    content = (
        f"From: {lead.get('first_name') or ''} {lead.get('last_name') or ''} "
        f"<{lead.get('email') or ''}>\n"
        f"Company: {lead.get('organization') or ''}\n"
        f"Phone: {lead.get('phone') or ''}\n"
        f"Date: {lead.get('created_at') or ''}\n"
        f"{'=' * 60}\n\n"
        f"{lead.get('lead_details') or lead.get('notes_body') or ''}"
    )
    file_path.write_text(content, encoding='utf-8')
    return str(file_path)


# Statuses where the lead did not land in Oracle the way the operator expected
# when they approved it.
ALERT_STATUSES = ('failed', 'submission_unknown', 'duplicate')


def list_submission_alerts(conn=None):
    """
    Leads whose submission needs a human to look at it, newest first.

    Two kinds, and the first is the one that is otherwise invisible:

      - A lead reported as 'submitted' that Oracle's pre-submission dedup check
        matched to an existing lead, so no new lead was created. Recorded only
        as a field_warnings entry, which nothing on the dashboard used to show.
      - A lead that outright failed, or whose outcome is unknown.

    Anything the operator has dismissed is excluded.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        placeholders = ', '.join('?' for _ in ALERT_STATUSES)
        rows = conn.execute(
            f"""SELECT * FROM leads
                WHERE alert_dismissed_at IS NULL
                  AND (status IN ({placeholders})
                       OR (field_warnings IS NOT NULL AND field_warnings != ''))
                ORDER BY COALESCE(submitted_at, updated_at) DESC, id DESC""",
            ALERT_STATUSES,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def dismiss_lead_alert(lead_id, conn=None):
    update_lead_fields(lead_id, {'alert_dismissed_at': _now()}, conn=conn)


def dismiss_all_lead_alerts(conn=None):
    """Dismiss every currently-open alert. Returns how many were cleared."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        placeholders = ', '.join('?' for _ in ALERT_STATUSES)
        now = _now()
        cur = conn.execute(
            f"""UPDATE leads SET alert_dismissed_at = ?, updated_at = ?
                WHERE alert_dismissed_at IS NULL
                  AND (status IN ({placeholders})
                       OR (field_warnings IS NOT NULL AND field_warnings != ''))""",
            (now, now, *ALERT_STATUSES),
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount
    finally:
        if owns_conn:
            conn.close()


def mark_lead_submitted(lead_id, oracle_project_name, oracle_record_id=None,
                        field_warnings=None, conn=None):
    fields = {
        'status': 'submitted', 'submitted_at': _now(),
        'oracle_project_name': oracle_project_name,
        'submitting_started_at': None, 'next_attempt_at': None,
        'submit_error': None,
    }
    if oracle_record_id:
        fields['oracle_record_id'] = str(oracle_record_id)
    if field_warnings:
        fields['field_warnings'] = (
            json.dumps(field_warnings)
            if not isinstance(field_warnings, str)
            else field_warnings
        )
        # New warnings are a new event -- re-raise the alert even if an older
        # one on this lead was already dismissed.
        fields['alert_dismissed_at'] = None
    update_lead_fields(lead_id, fields, conn=conn)


def mark_lead_duplicate_of_submitted(lead_id, prior_lead, detail, conn=None):
    """
    Park a lead that was never sent to Oracle because an equivalent one already
    was. Deliberately NOT 'submitted': nothing new was created, and recording
    it as submitted is what made the previous duplicate invisible.

    oracle_record_id points at the record that already exists, so the operator
    can open the real lead from here.
    """
    update_lead_fields(
        lead_id,
        {
            'status': 'duplicate',
            'duplicate_of_lead_id': prior_lead['id'],
            'oracle_record_id': prior_lead.get('oracle_record_id'),
            'oracle_project_name': prior_lead.get('oracle_project_name'),
            'field_warnings': json.dumps([detail]),
            'submitting_started_at': None,
            'next_attempt_at': None,
            'submit_error': None,
            'alert_dismissed_at': None,
        },
        conn=conn,
    )


def mark_lead_failed(lead_id, submit_error, submit_screenshot_path=None, conn=None):
    update_lead_fields(
        lead_id,
        {
            'status': 'failed', 'submit_error': submit_error,
            'submit_screenshot_path': submit_screenshot_path,
            'submitting_started_at': None,
            'alert_dismissed_at': None,
        },
        conn=conn,
    )


def claim_lead_for_submission(lead_id, conn=None):
    """Atomically move one approved lead into the submitting state."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        now = _now()
        cur = conn.execute(
            """UPDATE leads
               SET status = 'submitting', submitting_started_at = ?,
                   submission_attempt_count = submission_attempt_count + 1,
                   updated_at = ?
               WHERE id = ? AND status = 'approved'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)""",
            (now, now, lead_id, now),
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount == 1
    finally:
        if owns_conn:
            conn.close()


def release_stale_submission_claims(max_age_minutes=90, conn=None):
    """Recover claims left behind by a crashed submit process."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        cur = conn.execute(
            """UPDATE leads
               SET status = 'submission_unknown', updated_at = ?,
                   submit_error = COALESCE(
                       submit_error,
                       'Submission process stopped while this lead was in progress; reconcile in Oracle before retrying.'
                   )
               WHERE status = 'submitting' AND submitting_started_at < ?""",
            (_now(), cutoff),
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount
    finally:
        if owns_conn:
            conn.close()


def mark_submission_unknown(lead_id, detail, screenshot_path=None,
                            oracle_project_name=None, conn=None):
    update_lead_fields(
        lead_id,
        {
            'status': 'submission_unknown', 'submit_error': detail,
            'submit_screenshot_path': screenshot_path,
            'submitting_started_at': None,
            'oracle_project_name': oracle_project_name,
            'alert_dismissed_at': None,
        },
        conn=conn,
    )


def schedule_lead_retry(lead_id, detail, delay_minutes=15, conn=None):
    next_attempt = (
        datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    ).isoformat()
    update_lead_fields(
        lead_id,
        {
            'status': 'approved', 'submit_error': detail,
            'submitting_started_at': None, 'next_attempt_at': next_attempt,
        },
        conn=conn,
    )


def get_lead_email_thread(lead_id, conn=None):
    """
    Return all messages associated with this lead (original message + any follow-up messages).
    Returns list of dicts with keys: sender_name, sender_email, subject, received_at, body_text.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        lead = get_lead(lead_id, conn=conn)
        if not lead:
            return []

        # Follow-up emails from the same sender land as their own lead row
        # (each email = one lead), not as children of the original via a
        # parent_lead_id link -- nothing in the ingest pipeline ever sets
        # that column. dedup_key (same email/name/organization, set on every
        # insert_lead call) is what actually ties a reply back to the
        # original inquiry, so use that to pull the whole conversation.
        source_message = get_message(lead.get('message_id'), conn=conn)
        if source_message and source_message.get('conversation_id'):
            rows = conn.execute(
                """SELECT * FROM messages
                   WHERE conversation_id = ?
                   ORDER BY received_at ASC, id ASC""",
                (source_message['conversation_id'],),
            ).fetchall()
        else:
            rows = conn.execute("""
                SELECT DISTINCT m.* FROM messages m
                JOIN leads l ON l.message_id = m.id
                WHERE l.id = ? OR (l.dedup_key = ? AND l.dedup_key IS NOT NULL)
                ORDER BY m.received_at ASC, m.id ASC
            """, (lead_id, lead.get('dedup_key'))).fetchall()

        thread = []
        for idx, r in enumerate(rows):
            body = ""
            display_cache_path = r['translated_body_cache_path'] or r['body_cache_path']
            if display_cache_path and Path(display_cache_path).exists():
                try:
                    raw_body = Path(display_cache_path).read_text(encoding='utf-8', errors='replace')
                    import email_cleaner
                    body = email_cleaner.clean_email_body(raw_body)
                except Exception:
                    body = ""

            sender = r['sender_email'] or ''
            if r['sender_name']:
                sender = f"{r['sender_name']} <{sender}>"

            recipient = "sales@balseal.com"
            if r['sender_email'] and 'balseal.com' in r['sender_email'].lower() and lead.get('email'):
                recipient = lead['email']

            thread.append({
                'id': r['id'],
                'is_original': (idx == 0),
                'index': idx + 1,
                'sender': sender,
                'sender_email': r['sender_email'] or '',
                'recipient': recipient,
                'subject': r['translated_subject'] or r['subject'],
                'translated_from': r['original_language'] if r['translated_body_cache_path'] else None,
                'received_at': r['received_at'],
                'body_text': body,
            })
        return thread
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# run_state (simple key/value store)
# ---------------------------------------------------------------------------

def get_run_state(key, default=None, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute('SELECT value FROM run_state WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default
    finally:
        if owns_conn:
            conn.close()


def set_run_state(key, value, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            """INSERT INTO run_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


if __name__ == '__main__':
    init_db()
    print(f'Initialized database at {DB_PATH}')
