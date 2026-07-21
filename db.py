"""
SQLite data layer for the automated lead pipeline.
Plain sqlite3, no ORM — mirrors the flat function-based style of excel_parser.py etc.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / 'pipeline.db'

# Fields copied 1:1 from excel_parser.py's lead schema so leads can be handed
# straight to oracle_automation.create_lead() regardless of source. `assigned_tm`
# and `market` are additions beyond that original schema -- they feed Oracle's
# "Owner" and "Market" fields (via territory_map.assign_tm() and
# lead_extractor.py's market classification, respectively) and need to travel
# with the lead the same way every other Oracle-bound field does.
LEAD_SCHEMA_FIELDS = [
    'first_name', 'last_name', 'organization', 'title', 'phone', 'phone_mobile',
    'email', 'address1', 'address2', 'city', 'state', 'zip', 'country',
    'notes_action', 'notes_body', 'survey_answers', 'qualifiers', 'rank',
    'scan_device', 'lead_details', 'assigned_tm', 'market',
]

LEAD_STATUSES = (
    'ingested', 'extracted', 'screened', 'pending_review',
    'approved', 'rejected', 'submitted', 'failed', 'duplicate',
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db(db_path=DB_PATH):
    conn = get_connection(db_path)
    try:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS messages (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id     TEXT UNIQUE NOT NULL,
                received_at           TEXT,
                sender_email          TEXT,
                sender_name           TEXT,
                subject               TEXT,
                has_excel_attachment  INTEGER DEFAULT 0,
                body_cache_path       TEXT,
                status                TEXT NOT NULL DEFAULT 'new',
                error_detail          TEXT,
                processed_at          TEXT,
                created_at            TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id             INTEGER REFERENCES messages(id),
                source                 TEXT NOT NULL,
                source_row_number      INTEGER,
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
        conn.commit()
        _migrate_leads_columns(conn)
    finally:
        conn.close()


def _migrate_leads_columns(conn):
    """
    CREATE TABLE IF NOT EXISTS only creates a table if it's missing entirely --
    it does nothing for a table that already exists with an older schema. This
    adds any LEAD_SCHEMA_FIELDS column that's missing from an existing
    database (e.g. `market`, added after real leads already existed in
    pipeline.db), so real data already in the database is never lost or
    reset just because the schema grew.
    """
    existing = {row[1] for row in conn.execute('PRAGMA table_info(leads)')}
    for field in LEAD_SCHEMA_FIELDS + ['assigned_tm_basis']:
        if field not in existing:
            conn.execute(f'ALTER TABLE leads ADD COLUMN {field} TEXT')
    conn.commit()


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def insert_message(source_message_id, received_at=None, sender_email=None,
                    sender_name=None, subject=None, has_excel_attachment=False,
                    body_cache_path=None, conn=None):
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
               (source_message_id, received_at, sender_email, sender_name, subject,
                has_excel_attachment, body_cache_path, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
            (source_message_id, received_at, sender_email, sender_name, subject,
             int(has_excel_attachment), body_cache_path, _now()),
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


def insert_lead(lead, source, message_id=None, source_row_number=None,
                 status='ingested', conn=None):
    """
    Insert a new lead. `lead` is a dict using the LEAD_SCHEMA_FIELDS keys
    (same shape excel_parser.parse_excel() already produces).
    Automatically computes dedup_key and flags a duplicate_of_lead_id if a
    match already exists — never blocks the insert.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        dedup_key = compute_dedup_key(lead)
        dup = find_duplicate_lead(dedup_key, conn=conn)
        duplicate_of_lead_id = dup['id'] if dup else None

        now = _now()
        field_values = [lead.get(f) for f in LEAD_SCHEMA_FIELDS]
        columns = (
            ['message_id', 'source', 'source_row_number', 'dedup_key', 'duplicate_of_lead_id']
            + LEAD_SCHEMA_FIELDS
            + ['status', 'created_at', 'updated_at']
        )
        values = (
            [message_id, source, source_row_number, dedup_key, duplicate_of_lead_id]
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


def approve_lead(lead_id, reviewed_by=None, review_notes=None, edited_fields=None, conn=None):
    """Mark a lead approved, optionally applying edits made during review."""
    fields = dict(edited_fields or {})
    fields.update({
        'status': 'approved',
        'reviewed_by': reviewed_by,
        'reviewed_at': _now(),
        'review_notes': review_notes,
    })
    update_lead_fields(lead_id, fields, conn=conn)


def reject_lead(lead_id, reviewed_by=None, review_notes=None, conn=None):
    update_lead_fields(
        lead_id,
        {'status': 'rejected', 'reviewed_by': reviewed_by, 'reviewed_at': _now(), 'review_notes': review_notes},
        conn=conn,
    )


def mark_lead_submitted(lead_id, oracle_project_name, conn=None):
    update_lead_fields(
        lead_id,
        {'status': 'submitted', 'submitted_at': _now(), 'oracle_project_name': oracle_project_name},
        conn=conn,
    )


def mark_lead_failed(lead_id, submit_error, submit_screenshot_path=None, conn=None):
    update_lead_fields(
        lead_id,
        {'status': 'failed', 'submit_error': submit_error, 'submit_screenshot_path': submit_screenshot_path},
        conn=conn,
    )


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
