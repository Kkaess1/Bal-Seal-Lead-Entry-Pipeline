"""
SQLite data layer for the automated lead pipeline.
Plain sqlite3, no ORM — mirrors the flat function-based style of excel_parser.py etc.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lead_stages

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
    # The show a lead was met at, as its lead list names it. Fills the last
    # part of the Oracle project name ("... - Tradeshow - Space Tech Expo");
    # see pipeline_submit._project_label(). Set by spreadsheet_import.py and
    # editable on the review form like any other field.
    'trade_show',
    # Oracle's Lead Source, when one was chosen for this lead (the upload
    # form's "where are these leads from", or the review form). Empty means
    # decided at submission -- see oracle_api.lead_source_for().
    'lead_source',
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
                recipients            TEXT,
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
                oracle_payload         TEXT,
                field_warnings         TEXT,
                alert_dismissed_at     TEXT,
                force_submit           INTEGER NOT NULL DEFAULT 0,

                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_dedup_key ON leads(dedup_key);

            -- Replies the operator sent back to a lead from the dashboard.
            -- Outbound mail leaves the Sales inbox for Sent Items, so ingestion
            -- never sees it and it cannot appear in get_lead_email_thread().
            -- Without this row, the next person to open the lead has no way of
            -- knowing the customer has already been written to.
            CREATE TABLE IF NOT EXISTS lead_replies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id     INTEGER NOT NULL REFERENCES leads(id),
                message_id  INTEGER REFERENCES messages(id),
                kind        TEXT NOT NULL DEFAULT 'reply',
                recipients  TEXT,
                reply_all   INTEGER NOT NULL DEFAULT 0,
                body        TEXT NOT NULL,
                sent_by     TEXT,
                sent_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lead_replies_lead ON lead_replies(lead_id);

            CREATE TABLE IF NOT EXISTS outlook_actions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id         INTEGER NOT NULL REFERENCES leads(id),
                message_id      INTEGER REFERENCES messages(id),
                kind            TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'started',
                details         TEXT,
                error           TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outlook_actions_status
                ON outlook_actions(status, updated_at);
            -- Other messages in an ingested email's Outlook conversation,
            -- kept purely so a reviewer can read the exchange a reply belongs
            -- to. Deliberately NOT in `messages`: nothing here is a source of
            -- leads, gets claimed, retried or submitted -- it is context, and
            -- a separate table is what guarantees it stays that way.
            CREATE TABLE IF NOT EXISTS thread_messages (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT UNIQUE NOT NULL,
                conversation_id   TEXT,
                received_at       TEXT,
                sender_email      TEXT,
                sender_name       TEXT,
                subject           TEXT,
                body_text         TEXT,
                created_at        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_thread_messages_conversation
                ON thread_messages(conversation_id);

            CREATE TABLE IF NOT EXISTS run_state (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT NOT NULL
            );

            -- Read-only snapshots from Oracle Sales.  These are deliberately
            -- separate from the operational lead row: a reporting outage must
            -- never change whether a lead is approved or submitted.
            CREATE TABLE IF NOT EXISTS oracle_lead_snapshots (
                lead_id                       INTEGER PRIMARY KEY REFERENCES leads(id),
                oracle_lead_id                TEXT NOT NULL,
                oracle_lead_number            TEXT,
                oracle_status                 TEXT,
                qualified_at                  TEXT,
                converted_at                  TEXT,
                qualification_score           REAL,
                eligible_for_conversion       INTEGER,
                deal_amount                   REAL,
                deal_currency                 TEXT,
                oracle_owner                  TEXT,
                oracle_last_updated_at         TEXT,
                last_synced_at                 TEXT NOT NULL,
                sync_error                     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_status
                ON oracle_lead_snapshots(oracle_status);

            CREATE TABLE IF NOT EXISTS oracle_opportunities (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id              INTEGER NOT NULL REFERENCES leads(id),
                oracle_opportunity_id TEXT,
                opportunity_number   TEXT NOT NULL,
                name                 TEXT,
                account_name         TEXT,
                status               TEXT,
                sales_stage          TEXT,
                close_date           TEXT,
                win_probability      REAL,
                revenue_amount       REAL,
                expected_amount      REAL,
                currency_code        TEXT,
                last_synced_at       TEXT NOT NULL,
                UNIQUE(lead_id, opportunity_number)
            );
            CREATE INDEX IF NOT EXISTS idx_oracle_opportunities_lead
                ON oracle_opportunities(lead_id);

            CREATE TABLE IF NOT EXISTS oracle_order_lines (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id               INTEGER NOT NULL REFERENCES leads(id),
                oracle_opportunity_id TEXT NOT NULL,
                order_line_id         TEXT NOT NULL,
                order_number          TEXT,
                order_line_number     TEXT,
                product_name          TEXT,
                line_status           TEXT,
                win_loss_status       TEXT,
                order_date            TEXT,
                net_amount            REAL,
                contract_value        REAL,
                currency_code         TEXT,
                last_synced_at        TEXT NOT NULL,
                UNIQUE(lead_id, order_line_id)
            );
            CREATE INDEX IF NOT EXISTS idx_oracle_order_lines_lead
                ON oracle_order_lines(lead_id);

            CREATE TABLE IF NOT EXISTS oracle_lifecycle_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id     INTEGER NOT NULL REFERENCES leads(id),
                event_type  TEXT NOT NULL,
                event_at    TEXT NOT NULL,
                title       TEXT NOT NULL,
                detail      TEXT,
                fingerprint TEXT NOT NULL,
                UNIQUE(lead_id, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_oracle_events_lead
                ON oracle_lifecycle_events(lead_id, event_at);

            -- What retention.py deleted, kept as counts so the numbers on the
            -- status page do not fall as old leads are purged.
            --
            -- Every Oracle reporting table above is keyed to leads(id) with a
            -- foreign key, and foreign keys are ON, so a purged lead takes its
            -- snapshot, opportunities and order lines with it. Without this
            -- table "All leads", the conversion rates and the sales funnel
            -- would all quietly shrink each time the retention job ran, and a
            -- rate computed over a shrinking denominator is worse than no rate
            -- at all -- it looks like a real number.
            --
            -- Deliberately counts and money only. No names, no addresses, no
            -- email: this table has to survive a purge, so anything personal
            -- in it would defeat the purge.
            CREATE TABLE IF NOT EXISTS purged_lead_stats (
                metric     TEXT NOT NULL,
                bucket     TEXT NOT NULL,
                lead_count INTEGER NOT NULL DEFAULT 0,
                amount     REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (metric, bucket)
            );
        """)
        conn.execute('PRAGMA journal_mode = WAL')
        conn.commit()
        _migrate_leads_columns(conn)
        _migrate_messages_columns(conn)
        _migrate_lead_replies_columns(conn)
        _migrate_oracle_reporting_columns(conn)
    finally:
        conn.close()


def _migrate_leads_columns(conn):
    existing = {row[1] for row in conn.execute('PRAGMA table_info(leads)')}
    text_fields = LEAD_SCHEMA_FIELDS + [
        'assigned_tm_basis', 'parent_lead_id', 'ingest_key',
        'submitting_started_at', 'next_attempt_at', 'oracle_record_id',
        'oracle_payload', 'field_warnings', 'alert_dismissed_at',
    ]
    for field in text_fields:
        if field not in existing:
            conn.execute(f'ALTER TABLE leads ADD COLUMN {field} TEXT')
    if 'submission_attempt_count' not in existing:
        conn.execute(
            'ALTER TABLE leads ADD COLUMN submission_attempt_count INTEGER NOT NULL DEFAULT 0'
        )
    if 'force_submit' not in existing:
        conn.execute(
            'ALTER TABLE leads ADD COLUMN force_submit INTEGER NOT NULL DEFAULT 0'
        )
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_ingest_key '
        'ON leads(ingest_key) WHERE ingest_key IS NOT NULL'
    )
    conn.commit()


def _migrate_lead_replies_columns(conn):
    existing = {row[1] for row in conn.execute('PRAGMA table_info(lead_replies)')}
    if not existing:
        return   # table not created yet; init_db's schema already has the column
    if 'kind' not in existing:
        # Forwards used to leave a trace only in a lead's review_notes prose.
        # Recording them here alongside replies is what lets the address
        # autocomplete offer the people you actually forward to.
        conn.execute("ALTER TABLE lead_replies ADD COLUMN kind TEXT NOT NULL DEFAULT 'reply'")
    conn.commit()


def _migrate_oracle_reporting_columns(conn):
    existing = {
        row[1] for row in conn.execute('PRAGMA table_info(oracle_lead_snapshots)')
    }
    if existing and 'converted_at' not in existing:
        conn.execute('ALTER TABLE oracle_lead_snapshots ADD COLUMN converted_at TEXT')
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
    if 'recipients' not in existing:
        # JSON {"to": [...], "cc": [...]} of who the email was addressed to, so
        # the dashboard can show who else is on it before a reply goes out.
        # NULL means "never looked", which is distinct from a message that
        # genuinely has no other recipients -- see get_message_recipients().
        conn.execute('ALTER TABLE messages ADD COLUMN recipients TEXT')
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
                    conversation_id=None, ingest_batch_id=None, recipients=None,
                    conn=None):
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
                recipients, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
            (source_message_id, conversation_id, ingest_batch_id,
             received_at, sender_email, sender_name, subject,
             int(has_excel_attachment), body_cache_path, message_file_path,
             json.dumps(recipients) if recipients is not None else None, _now()),
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


def record_thread_message(source_message_id, conversation_id=None, received_at=None,
                          sender_email=None, sender_name=None, subject=None,
                          body_text=None, conn=None):
    """
    Store one conversation sibling for display. Ignores a message already held
    as a real ingested message -- that one is the authoritative row, and the
    thread view merges the two sources.
    """
    if not source_message_id:
        return None
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        if conn.execute(
            'SELECT 1 FROM messages WHERE source_message_id = ?', (source_message_id,)
        ).fetchone():
            return None
        cur = conn.execute(
            """INSERT OR IGNORE INTO thread_messages
               (source_message_id, conversation_id, received_at, sender_email,
                sender_name, subject, body_text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_message_id, conversation_id, received_at, sender_email,
             sender_name, subject, body_text, _now()),
        )
        if owns_conn:
            conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        if owns_conn:
            conn.close()


def list_thread_messages(conversation_id, conn=None):
    """Conversation siblings held as context, oldest first."""
    if not conversation_id:
        return []
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM thread_messages WHERE conversation_id = ? '
            'ORDER BY received_at ASC, id ASC',
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []   # database predating this table; see list_lead_replies()
    finally:
        if owns_conn:
            conn.close()


def parse_message_recipients(raw):
    """
    A messages.recipients value as {'to': [...], 'cc': [...]}.

    Returns None -- distinct from empty lists -- when the column is NULL,
    meaning nobody has ever looked. "We never checked" and "there is genuinely
    nobody else on this email" are different answers, and the reply dialog says
    something different for each.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {
        'to': [r for r in parsed.get('to') or [] if isinstance(r, dict)],
        'cc': [r for r in parsed.get('cc') or [] if isinstance(r, dict)],
    }


def get_message_recipients(message_id, conn=None):
    """Stored To/CC for a message, or None when it was never recorded."""
    if not message_id:
        return None
    message = get_message(message_id, conn=conn) or {}
    return parse_message_recipients(message.get('recipients'))


def set_message_recipients(message_id, recipients, conn=None):
    """Record who a message was addressed to. Separate from insert_message()
    because messages ingested before this was captured are backfilled later,
    on demand -- see outlook_ingest.message_recipients()."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            'UPDATE messages SET recipients = ? WHERE id = ?',
            (json.dumps(recipients or {'to': [], 'cc': []}), message_id),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


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


def existing_ingest_keys(ingest_keys, conn=None):
    """
    The subset of `ingest_keys` that already belong to a lead.

    insert_lead() quietly returns the existing row for a key it has seen, which
    is right for idempotency but leaves a caller unable to say "12 of these
    were already imported". Asked up front instead, in one query per chunk.
    """
    keys = [k for k in dict.fromkeys(ingest_keys) if k]
    if not keys:
        return set()
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        found = set()
        # Under SQLite's default bound-parameter limit.
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            placeholders = ', '.join('?' for _ in chunk)
            found.update(
                row['ingest_key'] for row in conn.execute(
                    f'SELECT ingest_key FROM leads WHERE ingest_key IN ({placeholders})',
                    chunk,
                )
            )
        return found
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


# How list_leads() may order its rows. Both end on l.id DESC so that leads
# sharing a timestamp -- one ingest run, one submit run -- still come back in a
# fixed order rather than whatever the query planner happens to produce.
LEAD_ORDERS = {
    # Outlook inbox order, for leads still being worked on.
    'received': 'COALESCE(m.received_at, l.created_at) DESC, l.id DESC',
    # The order leads finished in, for History. submitted_at is the moment the
    # lead reached Oracle and reviewed_at the moment a reviewer decided it, so
    # between them they cover every History status. updated_at is deliberately
    # only a fallback: dismissing an alert or an oracle_sync write touches it,
    # which would shuffle a finished lead back to the top months later.
    'finished': ('COALESCE(l.submitted_at, l.reviewed_at, l.updated_at, '
                 'l.created_at) DESC, l.id DESC'),
}


def list_leads(status=None, conn=None, limit=None, offset=0, order='received', query=None):
    """
    List leads, optionally filtered and paginated. `order` is a key of
    LEAD_ORDERS; the default puts the most recently received email first, the
    way the Outlook inbox is ordered.

    History passes order='finished' instead. Its leads are done, and the
    question there is "what went out last?", not "what arrived last?" -- a
    batch approved today is full of emails from a spread of dates, so inbox
    order interleaved today's submissions with last month's and read as no
    order at all.

    'received' orders on the lead's own message received_at, not created_at.
    created_at is when the pipeline ingested the lead, and one ingest run sweeps up
    several days of mail in a few seconds -- so ordering by it grouped the
    whole batch under one near-identical timestamp and left the emails inside
    it in whatever order they happened to be processed, which is not the order
    the operator saw them in Outlook.

    Leads with no message of their own (a spreadsheet row, say) fall back to
    created_at, which is the closest thing they have to an arrival time.

    Sorting the ISO-8601 strings directly is safe here and avoids a per-row
    parse: Outlook timestamps are all stored normalised to +00:00 and
    created_at is written as UTC too, so lexical order is chronological order.
    Anything paginated has to be ordered in SQL -- sorting in Python after
    LIMIT would only reorder within the current page.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        params = []
        # l.* rather than *: the join would otherwise fold the message's own
        # id/subject/received_at columns into the lead dict and shadow them.
        sql = ('SELECT l.* FROM leads l '
               'LEFT JOIN messages m ON m.id = l.message_id')
        conditions = []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            placeholders = ', '.join('?' for _ in statuses)
            conditions.append(f'l.status IN ({placeholders})')
            params.extend(statuses)
        if query and query.strip():
            term = f'%{query.strip()}%'
            conditions.append(
                "(l.first_name LIKE ? OR l.last_name LIKE ? OR "
                "(l.first_name || ' ' || l.last_name) LIKE ? OR "
                "l.organization LIKE ? OR l.email LIKE ?)"
            )
            params.extend([term] * 5)
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        try:
            sql += f' ORDER BY {LEAD_ORDERS[order]}'
        except KeyError:
            raise ValueError(
                f'unknown lead order {order!r}; expected one of '
                f"{', '.join(sorted(LEAD_ORDERS))}"
            ) from None
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            params.extend((max(1, int(limit)), max(0, int(offset))))
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def count_leads(status=None, query=None, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        params = []
        conditions = []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            placeholders = ', '.join('?' for _ in statuses)
            conditions.append(f'status IN ({placeholders})')
            params.extend(statuses)
        if query and query.strip():
            term = f'%{query.strip()}%'
            conditions.append(
                "(first_name LIKE ? OR last_name LIKE ? OR "
                "(first_name || ' ' || last_name) LIKE ? OR "
                "organization LIKE ? OR email LIKE ?)"
            )
            params.extend([term] * 5)
        sql = 'SELECT COUNT(*) FROM leads'
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        return conn.execute(sql, params).fetchone()[0]
    finally:
        if owns_conn:
            conn.close()


def list_oracle_sync_candidates(conn=None):
    """Submitted local leads with a usable Oracle identifier, oldest first."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE status = 'submitted'
                 AND oracle_record_id IS NOT NULL
                 AND oracle_record_id != ''
               ORDER BY submitted_at, id"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def save_oracle_snapshot(lead_id, snapshot, opportunities, order_lines=None, conn=None):
    """Atomically replace one lead's current Oracle reporting snapshot.

    A compact change log is retained so daily syncs build a useful lifecycle
    instead of overwriting every previous state transition.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        synced_at = snapshot.get('last_synced_at') or _now()
        conn.execute(
            """INSERT INTO oracle_lead_snapshots (
                   lead_id, oracle_lead_id, oracle_lead_number, oracle_status,
                   qualified_at, converted_at, qualification_score, eligible_for_conversion,
                   deal_amount, deal_currency, oracle_owner,
                   oracle_last_updated_at, last_synced_at, sync_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
               ON CONFLICT(lead_id) DO UPDATE SET
                   oracle_lead_id=excluded.oracle_lead_id,
                   oracle_lead_number=excluded.oracle_lead_number,
                   oracle_status=excluded.oracle_status,
                   qualified_at=excluded.qualified_at,
                   converted_at=excluded.converted_at,
                   qualification_score=excluded.qualification_score,
                   eligible_for_conversion=excluded.eligible_for_conversion,
                   deal_amount=excluded.deal_amount,
                   deal_currency=excluded.deal_currency,
                   oracle_owner=excluded.oracle_owner,
                   oracle_last_updated_at=excluded.oracle_last_updated_at,
                   last_synced_at=excluded.last_synced_at,
                   sync_error=NULL""",
            (
                lead_id, str(snapshot.get('oracle_lead_id') or ''),
                snapshot.get('oracle_lead_number'), snapshot.get('oracle_status'),
                snapshot.get('qualified_at'), snapshot.get('converted_at'),
                snapshot.get('qualification_score'),
                1 if snapshot.get('eligible_for_conversion') is True else
                0 if snapshot.get('eligible_for_conversion') is False else None,
                snapshot.get('deal_amount'), snapshot.get('deal_currency'),
                snapshot.get('oracle_owner'), snapshot.get('oracle_last_updated_at'),
                synced_at,
            ),
        )
        seen = []
        for opportunity in opportunities:
            number = str(opportunity.get('opportunity_number') or '').strip()
            if not number:
                continue
            seen.append(number)
            conn.execute(
                """INSERT INTO oracle_opportunities (
                       lead_id, oracle_opportunity_id, opportunity_number, name,
                       account_name, status, sales_stage, close_date,
                       win_probability, revenue_amount, expected_amount,
                       currency_code, last_synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lead_id, opportunity_number) DO UPDATE SET
                       oracle_opportunity_id=excluded.oracle_opportunity_id,
                       name=excluded.name, account_name=excluded.account_name,
                       status=excluded.status, sales_stage=excluded.sales_stage,
                       close_date=excluded.close_date,
                       win_probability=excluded.win_probability,
                       revenue_amount=excluded.revenue_amount,
                       expected_amount=excluded.expected_amount,
                       currency_code=excluded.currency_code,
                       last_synced_at=excluded.last_synced_at""",
                (
                    lead_id, str(opportunity.get('oracle_opportunity_id') or '') or None,
                    number, opportunity.get('name'), opportunity.get('account_name'),
                    opportunity.get('status'), opportunity.get('sales_stage'),
                    opportunity.get('close_date'), opportunity.get('win_probability'),
                    opportunity.get('revenue_amount'), opportunity.get('expected_amount'),
                    opportunity.get('currency_code'), synced_at,
                ),
            )
        if seen:
            placeholders = ','.join('?' for _ in seen)
            conn.execute(
                f'DELETE FROM oracle_opportunities WHERE lead_id = ? '
                f'AND opportunity_number NOT IN ({placeholders})',
                (lead_id, *seen),
            )
        else:
            conn.execute('DELETE FROM oracle_opportunities WHERE lead_id = ?', (lead_id,))

        seen_order_lines = []
        for order_line in (order_lines or []):
            line_id = str(order_line.get('order_line_id') or '').strip()
            opportunity_id = str(order_line.get('oracle_opportunity_id') or '').strip()
            if not line_id or not opportunity_id:
                continue
            seen_order_lines.append(line_id)
            conn.execute(
                """INSERT INTO oracle_order_lines (
                       lead_id, oracle_opportunity_id, order_line_id, order_number,
                       order_line_number, product_name, line_status, win_loss_status,
                       order_date, net_amount, contract_value, currency_code,
                       last_synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lead_id, order_line_id) DO UPDATE SET
                       oracle_opportunity_id=excluded.oracle_opportunity_id,
                       order_number=excluded.order_number,
                       order_line_number=excluded.order_line_number,
                       product_name=excluded.product_name,
                       line_status=excluded.line_status,
                       win_loss_status=excluded.win_loss_status,
                       order_date=excluded.order_date,
                       net_amount=excluded.net_amount,
                       contract_value=excluded.contract_value,
                       currency_code=excluded.currency_code,
                       last_synced_at=excluded.last_synced_at""",
                (
                    lead_id, opportunity_id, line_id, order_line.get('order_number'),
                    order_line.get('order_line_number'), order_line.get('product_name'),
                    order_line.get('line_status'), order_line.get('win_loss_status'),
                    order_line.get('order_date'), order_line.get('net_amount'),
                    order_line.get('contract_value'), order_line.get('currency_code'),
                    synced_at,
                ),
            )
        if order_lines is not None and seen_order_lines:
            placeholders = ','.join('?' for _ in seen_order_lines)
            conn.execute(
                f'DELETE FROM oracle_order_lines WHERE lead_id = ? '
                f'AND order_line_id NOT IN ({placeholders})',
                (lead_id, *seen_order_lines),
            )
        elif order_lines is not None:
            conn.execute('DELETE FROM oracle_order_lines WHERE lead_id = ?', (lead_id,))

        _record_oracle_events(conn, lead_id, snapshot, opportunities, order_lines or ())
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def _record_oracle_events(conn, lead_id, snapshot, opportunities, order_lines=()):
    events = []
    status = snapshot.get('oracle_status')
    if status:
        events.append(('lead_status', snapshot.get('oracle_last_updated_at'),
                       f'Oracle lead status: {status}', status))
    if snapshot.get('qualified_at'):
        score = snapshot.get('qualification_score')
        detail = f'Qualification score: {score:g}' if isinstance(score, (int, float)) else None
        events.append(('qualified', snapshot['qualified_at'], 'Lead qualified', detail))
    if snapshot.get('converted_at'):
        events.append(('converted', snapshot['converted_at'],
                       'Lead converted to opportunity', None))
    for opportunity in opportunities:
        number = str(opportunity.get('opportunity_number') or '')
        name = opportunity.get('name') or number
        detail = ' | '.join(str(value) for value in (
            opportunity.get('status'), opportunity.get('sales_stage')) if value)
        events.append(('opportunity', snapshot.get('last_synced_at'),
                       f'Opportunity: {name}', detail or None))
    for order_line in order_lines:
        order_number = order_line.get('order_number') or 'un-numbered order'
        amount = order_line.get('net_amount')
        currency = order_line.get('currency_code') or ''
        detail = f'{currency} {amount:,.2f}' if isinstance(amount, (int, float)) else None
        events.append(('order', order_line.get('order_date') or snapshot.get('last_synced_at'),
                       f'Order linked: {order_number}', detail))
    for event_type, event_at, title, detail in events:
        event_at = event_at or snapshot.get('last_synced_at') or _now()
        fingerprint = hashlib.sha256(
            f'{event_type}|{title}|{detail or ""}'.encode('utf-8')
        ).hexdigest()
        conn.execute(
            """INSERT OR IGNORE INTO oracle_lifecycle_events
                   (lead_id, event_type, event_at, title, detail, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (lead_id, event_type, event_at, title, detail, fingerprint),
        )


def mark_oracle_sync_error(lead_id, oracle_lead_id, error, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            """INSERT INTO oracle_lead_snapshots
                   (lead_id, oracle_lead_id, last_synced_at, sync_error)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(lead_id) DO UPDATE SET
                   last_synced_at=excluded.last_synced_at,
                   sync_error=excluded.sync_error""",
            (lead_id, str(oracle_lead_id), _now(), str(error)[:2000]),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def oracle_dashboard_summary(conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM oracle_lead_snapshots
                    WHERE oracle_status IS NOT NULL OR oracle_lead_number IS NOT NULL) synced,
                 (SELECT COUNT(*) FROM oracle_lead_snapshots
                    WHERE (qualified_at IS NOT NULL
                       OR UPPER(COALESCE(oracle_status,'')) = 'QUALIFIED')) qualified,
                 (SELECT COUNT(*) FROM oracle_lead_snapshots s
                    WHERE s.converted_at IS NOT NULL OR EXISTS (
                        SELECT 1 FROM oracle_opportunities o WHERE o.lead_id = s.lead_id
                    )) converted,
                 (SELECT COUNT(DISTINCT lead_id) FROM oracle_opportunities
                    WHERE UPPER(COALESCE(status,'')) IN ('WON','CLOSED_WON','CLOSED WON')) won,
                 (SELECT COUNT(*) FROM oracle_lead_snapshots WHERE sync_error IS NOT NULL) errors"""
        ).fetchone()
        currencies = conn.execute(
            """SELECT COALESCE(currency_code, 'Unknown') currency,
                      SUM(COALESCE(revenue_amount, 0)) pipeline_value,
                      SUM(CASE WHEN UPPER(COALESCE(status,'')) IN
                           ('WON','CLOSED_WON','CLOSED WON')
                          THEN COALESCE(revenue_amount,0) ELSE 0 END) won_value
               FROM oracle_opportunities GROUP BY COALESCE(currency_code, 'Unknown')
               ORDER BY currency"""
        ).fetchall()
        order_currencies = conn.execute(
            """SELECT COALESCE(currency_code, 'Unknown') currency,
                      SUM(COALESCE(net_amount, 0)) order_value
               FROM oracle_order_lines
               WHERE order_number IS NOT NULL AND order_number != ''
               GROUP BY COALESCE(currency_code, 'Unknown') ORDER BY currency"""
        ).fetchall()
        return {
            **dict(row), 'currencies': [dict(r) for r in currencies],
            'order_currencies': [dict(r) for r in order_currencies],
        }
    finally:
        if owns_conn:
            conn.close()


def search_lead_outcomes(query='', outcome='all', limit=100, conn=None):
    """Search only leads this pipeline actually created in Oracle.

    Oracle pre-submission dedup matches are intentionally excluded: they are
    associated with a local review, but the Oracle lead predated this pipeline.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        params = []
        conditions = [
            "l.status = 'submitted'",
            "l.oracle_record_id IS NOT NULL",
            "l.oracle_record_id != ''",
            "COALESCE(l.field_warnings, '') NOT LIKE '%already exists in Oracle%'",
        ]
        if outcome == 'qualified':
            conditions.append(
                "(s.qualified_at IS NOT NULL OR "
                "UPPER(COALESCE(s.oracle_status,'')) = 'QUALIFIED')"
            )
        elif outcome == 'converted':
            conditions.append(
                "(s.converted_at IS NOT NULL OR EXISTS ("
                "SELECT 1 FROM oracle_opportunities converted_opportunity "
                "WHERE converted_opportunity.lead_id = l.id))"
            )
        if query.strip():
            term = f'%{query.strip()}%'
            conditions.append("""(l.first_name LIKE ? OR l.last_name LIKE ?
                         OR l.organization LIKE ? OR l.email LIKE ?
                         OR l.oracle_record_id LIKE ? OR s.oracle_lead_number LIKE ?
                         OR EXISTS (SELECT 1 FROM oracle_opportunities ox
                             WHERE ox.lead_id = l.id AND
                               (ox.opportunity_number LIKE ? OR ox.name LIKE ?)))""")
            params = [term] * 8
        where = 'WHERE ' + ' AND '.join(conditions)
        rows = conn.execute(
            f"""SELECT l.id, l.first_name, l.last_name, l.organization, l.email,
                       l.status local_status, l.created_at, l.submitted_at,
                       l.oracle_record_id, s.oracle_lead_number, s.oracle_status,
                       s.qualified_at, s.qualification_score, s.last_synced_at,
                       s.sync_error, COUNT(o.id) opportunity_count,
                       GROUP_CONCAT(DISTINCT o.currency_code) currencies,
                       SUM(COALESCE(o.revenue_amount, 0)) revenue_amount,
                       (SELECT GROUP_CONCAT(DISTINCT ol.currency_code)
                          FROM oracle_order_lines ol WHERE ol.lead_id = l.id
                            AND ol.order_number IS NOT NULL) order_currencies,
                       (SELECT SUM(COALESCE(ol.net_amount, 0))
                          FROM oracle_order_lines ol WHERE ol.lead_id = l.id
                            AND ol.order_number IS NOT NULL) order_value,
                       MAX(CASE WHEN UPPER(COALESCE(o.status,'')) IN
                           ('WON','CLOSED_WON','CLOSED WON') THEN 1 ELSE 0 END) has_won
                FROM leads l
                LEFT JOIN oracle_lead_snapshots s ON s.lead_id = l.id
                LEFT JOIN oracle_opportunities o ON o.lead_id = l.id
                {where}
                GROUP BY l.id
                ORDER BY l.created_at DESC LIMIT ?""",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def get_lead_lifecycle(lead_id, conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        lead = get_lead(lead_id, conn=conn)
        if not lead:
            return None
        snapshot = conn.execute(
            'SELECT * FROM oracle_lead_snapshots WHERE lead_id = ?', (lead_id,)
        ).fetchone()
        opportunities = conn.execute(
            'SELECT * FROM oracle_opportunities WHERE lead_id = ? ORDER BY close_date, id',
            (lead_id,),
        ).fetchall()
        events = conn.execute(
            'SELECT * FROM oracle_lifecycle_events WHERE lead_id = ? ORDER BY event_at, id',
            (lead_id,),
        ).fetchall()
        order_lines = conn.execute(
            'SELECT * FROM oracle_order_lines WHERE lead_id = ? ORDER BY order_date, id',
            (lead_id,),
        ).fetchall()
        return {
            'lead': lead,
            'snapshot': dict(snapshot) if snapshot else None,
            'opportunities': [dict(row) for row in opportunities],
            'order_lines': [dict(row) for row in order_lines],
            'events': [dict(row) for row in events],
        }
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


# Removed on 2026-08-07: consolidate_ingest_batch_threads(), which kept only
# the newest message's lead when several messages of one Outlook thread arrived
# in the same ingest run and deleted the rest. Every non-green email now keeps
# its own lead, because deciding whether a reply needs answering is the
# operator's call and a deleted lead never reaches them to make it. Earlier
# messages of a thread are also stored as read-only context -- see
# record_thread_message() -- so a reply is still read against its exchange.


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
        if not current or current['status'] not in lead_stages.EDITABLE:
            return False
        update_lead_fields(lead_id, fields, conn=conn)
        if owns_conn:
            conn.commit()
        return True
    finally:
        if owns_conn:
            conn.close()


def finalize_approved_batch(reviewed_by=None, lead_ids=None, conn=None):
    """Transition staged 'review_approved' leads to final 'approved' status ready for Oracle submission.

    Clears submit_error on the way through: re-approving a lead that
    return_lead_for_correction() sent back is the operator saying they have corrected
    it, so the old reason should not follow it forward. If it blocks again the
    next attempt writes a fresh one.

    `lead_ids` restricts the batch to leads the caller has just checked. The
    dashboard passes it so that a lead staged in another tab between its
    blocker check and this update cannot ride out unchecked. Omitting it
    promotes everything staged, which is what a caller with no list means.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        now = _now()
        sql = ("""UPDATE leads SET status = 'approved', updated_at = ?,
                                   submit_error = NULL
                  WHERE status = 'review_approved'""")
        params = [now]
        if lead_ids is not None:
            lead_ids = list(lead_ids)
            if not lead_ids:
                return 0
            sql += f" AND id IN ({', '.join('?' for _ in lead_ids)})"
            params.extend(lead_ids)
        cur = conn.execute(sql, params)
        count = cur.rowcount
        if owns_conn:
            conn.commit()
        return count
    finally:
        if owns_conn:
            conn.close()


def finish_ingest(lead_id, conn=None):
    """Move a lead from mid-ingest to Pending Review, and only from there.

    Guarded on the old status because those leads are now visible while the
    ingest run is still working on them (lead_stages.INGESTING is listed on
    Pending Review, so an interrupted run cannot hide a lead any more). A
    reviewer who approves one during the seconds fraud screening takes would
    otherwise have that approval overwritten by this write landing after it.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        placeholders = ', '.join('?' for _ in lead_stages.INGESTING)
        cur = conn.execute(
            f"""UPDATE leads SET status = 'pending_review', updated_at = ?
                WHERE id = ? AND status IN ({placeholders})""",
            [_now(), lead_id, *lead_stages.INGESTING],
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount > 0
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
    """
    Restore a rejected (or failed) lead back to 'pending_review' status.

    The review stamp goes with it. A lead sitting in the pending queue has not
    been reviewed, and leaving the old one behind is not merely untidy: the
    review card prefills its notes box from review_notes, so a lead rejected
    with 'Original email deleted from the Sales inbox' and then restored would
    carry that sentence into its next approval as if a reviewer had written it.

    submit_error is deliberately kept: on a restored 'failed' lead it is why
    the last attempt failed, which is the reason to look at it, and a
    successful submission clears it (see mark_lead_submitted).
    """
    update_lead_fields(
        lead_id,
        {
            'status': 'pending_review',
            'reviewed_by': None, 'reviewed_at': None, 'review_notes': None,
        },
        conn=conn,
    )


def recall_approved_lead(lead_id, conn=None):
    """
    Pull a lead back out of the send queue, from 'approved' and only from there.

    An approved lead that cannot actually go to Oracle -- no Territory Manager,
    say -- used to be a dead end. It is not on Pending Review, so it has no
    review card; 'approved' is not in lead_stages.EDITABLE, so the lead page
    shows no form; and the queue's only escape hatch (check_oracle) requires
    'submitting'. The lead sat under "Approved for Oracle" with the field that
    would fix it uneditable, and nothing on any page could move it.

    Guarded on the status, and returning whether it moved, because a submit run
    claims each lead by flipping it to 'submitting' before it calls Oracle
    (see claim_lead_for_submission). If a run claims this lead between the page
    being drawn and the button being clicked, the UPDATE matches nothing and
    the caller reports that instead of yanking a lead out from under a POST
    that may already be in flight.

    submit_error is kept -- it is why the lead is being pulled back -- and the
    review stamp is cleared for the reason restore_lead_to_review() gives.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            """UPDATE leads
               SET status = 'pending_review', updated_at = ?,
                   reviewed_by = NULL, reviewed_at = NULL, review_notes = NULL,
                   submitting_started_at = NULL, next_attempt_at = NULL
               WHERE id = ? AND status IN ('approved', 'failed')""",
            (_now(), lead_id),
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount > 0
    finally:
        if owns_conn:
            conn.close()


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
        # The third arm catches a lead return_lead_for_correction() sent back: it is
        # sitting in Pending Review rather than in a failure status, but the
        # attempt still failed while the operator was not watching, so the bell
        # is how they find out without re-reading the whole queue.
        rows = conn.execute(
            f"""SELECT * FROM leads
                WHERE alert_dismissed_at IS NULL
                  AND (status IN ({placeholders})
                       OR (field_warnings IS NOT NULL AND field_warnings != '')
                       OR (status = 'pending_review'
                           AND submit_error IS NOT NULL AND submit_error != ''))
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
        # Must match list_submission_alerts() arm for arm. If it does not, an
        # alert it raises but this cannot reach is undismissable, and the bell
        # keeps a count the operator has no way to clear.
        cur = conn.execute(
            f"""UPDATE leads SET alert_dismissed_at = ?, updated_at = ?
                WHERE alert_dismissed_at IS NULL
                  AND (status IN ({placeholders})
                       OR (field_warnings IS NOT NULL AND field_warnings != '')
                       OR (status = 'pending_review'
                           AND submit_error IS NOT NULL AND submit_error != ''))""",
            (now, now, *ALERT_STATUSES),
        )
        if owns_conn:
            conn.commit()
        return cur.rowcount
    finally:
        if owns_conn:
            conn.close()


def mark_lead_submitted(lead_id, oracle_project_name, oracle_record_id=None,
                        field_warnings=None, oracle_payload=None, conn=None):
    fields = {
        'status': 'submitted', 'submitted_at': _now(),
        'oracle_project_name': oracle_project_name,
        'submitting_started_at': None, 'next_attempt_at': None,
        'submit_error': None, 'force_submit': 0,
    }
    if oracle_record_id:
        fields['oracle_record_id'] = str(oracle_record_id)
    if oracle_payload:
        fields['oracle_payload'] = (
            json.dumps(oracle_payload, sort_keys=True)
            if not isinstance(oracle_payload, str) else oracle_payload
        )
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


def mark_lead_for_force_submit(lead_id, conn=None):
    """
    Mark a lead to force submission to Oracle, bypassing duplicate checks.
    Puts it back into 'approved' status and sets force_submit=1.
    """
    update_lead_fields(
        lead_id,
        {
            'status': 'approved',
            'force_submit': 1,
            'submitting_started_at': None,
            'next_attempt_at': None,
            'submit_error': None,
            'alert_dismissed_at': _now(),
        },
        conn=conn,
    )


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


def return_lead_for_correction(lead_id, submit_error, conn=None):
    """
    Send a lead that never reached Oracle back to Pending Review, with the
    reason attached.

    For failures caught *before* Oracle is contacted -- a missing country, an
    unassigned territory manager. Nothing exists on Oracle's side to reconcile,
    and the fix is a field the operator can edit, so parking the lead in
    'failed' only moved it to History where nobody was looking for it and the
    work had to be undone by hand ("Return to Review") before it could be
    corrected.

    'pending_review', matching restore_lead_to_review() and the manual "Return
    to Review" button: a lead that could not be submitted has to be looked at
    again, and Pending Review is where an unresolved lead belongs. Approving it
    from there is one button, whereas a staged lead can only go out with the
    whole batch.

    The review stamp is cleared for the reason restore_lead_to_review() gives:
    the review card prefills its notes box from review_notes, so a stale note
    would ride along into the next approval as if a reviewer had just written
    it. submit_error is deliberately kept -- it is why the lead is back.

    Never used for an outcome Oracle may have acted on -- see
    mark_submission_unknown(), where a record might exist and a human has to
    look before anything is retried.
    """
    update_lead_fields(
        lead_id,
        {
            'status': 'pending_review',
            'submit_error': submit_error,
            'reviewed_by': None, 'reviewed_at': None, 'review_notes': None,
            'submitting_started_at': None,
            'next_attempt_at': None,
            # A fresh failure is a fresh event: re-raise the bell even if an
            # earlier alert on this lead was dismissed.
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

        import email_cleaner

        entries = []
        for r in rows:
            body = ""
            display_cache_path = r['translated_body_cache_path'] or r['body_cache_path']
            if display_cache_path and Path(display_cache_path).exists():
                try:
                    raw_body = Path(display_cache_path).read_text(encoding='utf-8', errors='replace')
                    body = email_cleaner.clean_email_body(raw_body)
                except Exception:
                    body = ""
            entries.append({
                'id': r['id'],
                'sender_email': r['sender_email'] or '',
                'sender_name': r['sender_name'],
                'subject': r['translated_subject'] or r['subject'],
                'translated_from': r['original_language'] if r['translated_body_cache_path'] else None,
                'received_at': r['received_at'],
                'body_text': body,
                'is_context': False,
            })

        # Merge in the rest of the Outlook conversation. Those messages were
        # handled before this reply arrived, so they were skipped as green and
        # never became messages of their own -- without them a "RE: ..." lead
        # shows up with nothing to read it against.
        if source_message and source_message.get('conversation_id'):
            # Sender + timestamp, not id: a message reached through Outlook's
            # conversation reports a different EntryID than the one it was
            # ingested under, so context captured for one lead can otherwise
            # duplicate a real message on another lead in the same thread.
            already = {
                ((e['received_at'] or ''), (e['sender_email'] or '').lower())
                for e in entries
            }
            for r in list_thread_messages(source_message['conversation_id'], conn=conn):
                if ((r['received_at'] or ''), (r['sender_email'] or '').lower()) in already:
                    continue
                entries.append({
                    'id': f"context-{r['id']}",
                    'sender_email': r['sender_email'] or '',
                    'sender_name': r['sender_name'],
                    'subject': r['subject'],
                    'translated_from': None,
                    'received_at': r['received_at'],
                    # Cleaned like an ingested body, not stored raw: an item
                    # pulled from Outlook's conversation carries the whole
                    # chain quoted underneath it, so leaving it alone printed
                    # every earlier message again inside each later one -- the
                    # thread read as the same emails pasted over and over.
                    # Each card now holds only what that sender newly wrote.
                    'body_text': email_cleaner.clean_email_body(r['body_text'] or ''),
                    'is_context': True,
                })

        entries.sort(key=lambda e: (e['received_at'] or '', str(e['id'])))

        thread = []
        for idx, entry in enumerate(entries):
            sender = entry['sender_email']
            if entry['sender_name']:
                sender = f"{entry['sender_name']} <{sender}>"

            is_internal = bool(
                entry['sender_email'] and 'balseal.com' in entry['sender_email'].lower()
            )
            recipient = "sales@balseal.com"
            if is_internal and lead.get('email'):
                recipient = lead['email']

            thread.append({
                'id': entry['id'],
                'is_original': (idx == 0),
                'index': idx + 1,
                # Position in the conversation, so the display can say "2 of 4"
                # rather than leaving the reader to count cards.
                'position': idx + 1,
                'total': len(entries),
                'sender': sender,
                'sender_name': entry['sender_name'] or '',
                'sender_email': entry['sender_email'],
                # Which side wrote it -- the one distinction worth a colour.
                'is_internal': is_internal,
                'recipient': recipient,
                'subject': entry['subject'],
                'translated_from': entry['translated_from'],
                'received_at': entry['received_at'],
                'body_text': entry['body_text'],
                'is_context': entry['is_context'],
            })
        return thread
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# lead_replies (outbound mail sent from the dashboard)
# ---------------------------------------------------------------------------


def begin_outlook_action(lead_id, kind, message_id=None, details=None, conn=None):
    """Persist intent before Outlook is allowed to perform a side effect."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        now = _now()
        cur = conn.execute(
            """INSERT INTO outlook_actions
               (lead_id, message_id, kind, status, details, created_at, updated_at)
               VALUES (?, ?, ?, 'started', ?, ?, ?)""",
            (lead_id, message_id, kind,
             json.dumps(details or {}, ensure_ascii=False), now, now),
        )
        if owns_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def finish_outlook_action(action_id, status, error=None, conn=None):
    """Record the authoritative or uncertain outcome of an Outlook action."""
    if status not in ('sent', 'deleted', 'failed', 'unknown'):
        raise ValueError(f'Unsupported Outlook action status: {status}')
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            'UPDATE outlook_actions SET status = ?, error = ?, updated_at = ? WHERE id = ?',
            (status, error, _now(), action_id),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def list_unresolved_outlook_actions(conn=None):
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        return [dict(row) for row in conn.execute(
            """SELECT * FROM outlook_actions
               WHERE status IN ('started', 'unknown')
               ORDER BY created_at DESC"""
        ).fetchall()]
    finally:
        if owns_conn:
            conn.close()

def record_lead_reply(lead_id, body, recipients=None, cc_recipients=None,
                      message_id=None, reply_all=False, sent_by=None,
                      kind='reply', conn=None):
    """
    Record that a reply was sent to the customer behind this lead.

    Called only after Outlook reports the send succeeded, so a row here means
    the mail actually went out -- never that it was attempted.

    Stored as JSON {"to": [...], "cc": [...]}: who was addressed and who was
    only copied are different facts, and the log is the audit trail of who was
    written to. Rows written before copies existed hold a bare list, which
    list_lead_replies() reads as a To line.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO lead_replies
               (lead_id, message_id, kind, recipients, reply_all, body, sent_by, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, message_id, kind,
             json.dumps({'to': list(recipients or []), 'cc': list(cc_recipients or [])}),
             1 if reply_all else 0, body, sent_by, _now()),
        )
        if owns_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def list_known_addresses(limit=400, conn=None):
    """
    Addresses the dashboard has seen, for autocompleting a forward or reply.

    Ordered by how useful a suggestion is likely to be, not alphabetically:
    somewhere you have already sent something comes first (that is the short
    list of colleagues a lead gets handed to), then people who wrote in, then
    everyone else who was on an email.

    Returns [{'address', 'name'}]. Never raises on a database that
    predates any of these tables -- an autocomplete list is not worth failing
    a page render for.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    ranked = {}

    def add(address, name, why, rank):
        address = (address or '').strip()
        if not address or '@' not in address:
            return
        key = address.lower()
        current = ranked.get(key)
        if current is None or rank < current['rank']:
            ranked[key] = {'address': address, 'name': (name or '').strip(),
                           'why': why, 'rank': rank}
        elif not current['name'] and name:
            current['name'] = name.strip()

    try:
        try:
            rows = conn.execute(
                'SELECT kind, recipients FROM lead_replies ORDER BY sent_at DESC'
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            parsed = row['recipients']
            try:
                parsed = json.loads(parsed or '{}')
            except (TypeError, ValueError):
                continue
            addresses = (parsed.get('to') or []) + (parsed.get('cc') or []) \
                if isinstance(parsed, dict) else parsed
            why = {'forward': 'you have forwarded to this address',
                   'reply': 'you have replied to this address'}.get(
                       row['kind'], 'you have written to this address')
            for address in addresses or []:
                add(address, '', why, 0)

        for row in conn.execute(
            'SELECT sender_email, sender_name FROM messages '
            'WHERE sender_email IS NOT NULL ORDER BY received_at DESC'
        ).fetchall():
            add(row['sender_email'], row['sender_name'], 'wrote to the Sales inbox', 1)

        try:
            for row in conn.execute(
                'SELECT sender_email, sender_name FROM thread_messages '
                'WHERE sender_email IS NOT NULL ORDER BY received_at DESC'
            ).fetchall():
                add(row['sender_email'], row['sender_name'], 'wrote in a thread', 1)
        except sqlite3.OperationalError:
            pass

        for row in conn.execute(
            "SELECT recipients FROM messages WHERE recipients IS NOT NULL"
        ).fetchall():
            for entry in (parse_message_recipients(row['recipients']) or {}).get('to', []) + \
                    (parse_message_recipients(row['recipients']) or {}).get('cc', []):
                add(entry.get('address'), entry.get('name'), 'was on an email', 2)
    finally:
        if owns_conn:
            conn.close()

    # Territory contacts often receive forwards but never write to the Sales
    # inbox, so Outlook has given us only their address, not a display name.
    # The maintained territory tables already contain their authoritative
    # names. Match conventional company usernames (wkahout -> Wade Kahout)
    # so searching by the person's name works for those saved addresses too.
    try:
        import territory_map
        staff_names = {
            name for mapping in (
                territory_map.US_STATE_TO_TM, territory_map.COUNTRY_TO_TM,
                territory_map.CANADA_PROVINCE_TO_TM, territory_map.US_STATE_TO_CRR,
                territory_map.CANADA_PROVINCE_TO_CRR,
            )
            for name in mapping.values() if name
        }
        staff_names.update(
            value for key, value in vars(territory_map).items()
            if key.startswith('_CRR_') and isinstance(value, str) and value
        )

        # Bal Seal addresses follow first initial + surname. Populate every
        # named territory contact even before somebody has forwarded to them,
        # so typing "Ethan John" can offer ejohn@balseal.com immediately.
        # All words after the given name are treated as the surname, preserving
        # names such as Sandra van der Maat -> svandermaat@balseal.com.
        for name in staff_names:
            parts = [''.join(ch for ch in part.lower() if ch.isalnum())
                     for part in name.split()]
            parts = [part for part in parts if part]
            if len(parts) < 2:
                continue
            inferred_address = parts[0][0] + ''.join(parts[1:]) + '@balseal.com'
            add(inferred_address, name, 'Bal Seal territory contact', 2)

        for entry in ranked.values():
            if entry['name']:
                continue
            local = ''.join(ch for ch in entry['address'].split('@', 1)[0].lower()
                            if ch.isalnum())
            for name in staff_names:
                parts = [''.join(ch for ch in part.lower() if ch.isalnum())
                         for part in name.split()]
                parts = [part for part in parts if part]
                if not parts:
                    continue
                candidates = {
                    ''.join(parts), '.'.join(parts),
                    parts[0][0] + ''.join(parts[1:]) if len(parts) > 1 else parts[0],
                }
                if local in candidates:
                    entry['name'] = name
                    break
    except (ImportError, AttributeError):
        pass

    ordered = sorted(ranked.values(), key=lambda e: (e['rank'], e['address'].lower()))
    # Ranking/source explanations are implementation details. The dropdown is
    # intentionally just a compact contact list: name and email, nothing else.
    return [
        {'address': entry['address'], 'name': entry['name']}
        for entry in ordered[:limit]
    ]


def list_lead_replies(lead_id, conn=None):
    """Replies sent for a lead, oldest first. `recipients` and `cc_recipients`
    come back as lists; a malformed value degrades to [] rather than breaking
    the page that renders it, and a legacy bare list reads as a To line."""
    if not lead_id:
        return []
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM lead_replies WHERE lead_id = ? ORDER BY sent_at ASC, id ASC',
            (lead_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # A database created before this table existed and not re-initialised
        # yet. Every entry point calls init_db() (which creates it) on startup,
        # so this only covers a direct read against an older file -- and "no
        # replies recorded" is the truthful answer there anyway.
        return []
    finally:
        if owns_conn:
            conn.close()

    replies = []
    for row in rows:
        reply = dict(row)
        try:
            parsed = json.loads(reply.get('recipients') or '[]')
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, dict):
            to_list = parsed.get('to') or []
            cc_list = parsed.get('cc') or []
        else:
            # Written before copies existed: everyone recorded was addressed.
            to_list, cc_list = (parsed if isinstance(parsed, list) else []), []
        reply['recipients'] = [str(a) for a in to_list]
        reply['cc_recipients'] = [str(a) for a in cc_list]
        replies.append(reply)
    return replies


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


def acquire_run_lease(key, stale_minutes=90, conn=None):
    """Atomically acquire a named background-job lease; return its token."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT value, updated_at FROM run_state WHERE key = ?', (key,)
        ).fetchone()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        if row:
            try:
                state = json.loads(row['value'] or '{}')
                updated = datetime.fromisoformat(row['updated_at'])
            except (TypeError, ValueError, json.JSONDecodeError):
                state, updated = {}, datetime.min.replace(tzinfo=timezone.utc)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if state.get('status') in ('starting', 'running') and updated >= cutoff:
                # Background jobs record their PID as soon as Popen succeeds.
                # A forced Task Scheduler shutdown cannot run the job's
                # finally/except cleanup, so the durable lease can otherwise
                # claim "running" for 90 minutes after its process is gone.
                # Recover that orphan immediately while retaining the age
                # cutoff as a fallback for older leases that have no PID.
                pid = state.get('pid')
                if not pid or _process_is_running(pid):
                    conn.rollback()
                    return None

        token = secrets.token_urlsafe(24)
        now = _now()
        value = json.dumps({'token': token, 'status': 'starting', 'started_at': now})
        conn.execute(
            """INSERT INTO run_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, now),
        )
        conn.commit()
        return token
    finally:
        if owns_conn:
            conn.close()


def _process_is_running(pid):
    """Return whether *pid* still names a process, without signalling it."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if os.name == 'nt':
        # Unlike POSIX, Windows Python may pass signal 0 to TerminateProcess;
        # os.kill(pid, 0) is therefore not a safe liveness probe here.
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Access denied proves that a process owns the PID even though this
        # account cannot open it. Invalid/not-found PIDs return other errors.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False
    return True


def update_run_lease(key, token, status, detail=None, pid=None, conn=None):
    """Update a lease only when its unguessable token still owns the job."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute('SELECT value FROM run_state WHERE key = ?', (key,)).fetchone()
        try:
            state = json.loads(row['value']) if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            state = {}
        if not token or not secrets.compare_digest(str(state.get('token', '')), token):
            return False
        state['status'] = status
        state['detail'] = detail
        if pid is not None:
            state['pid'] = int(pid)
        if status in ('succeeded', 'failed'):
            state['finished_at'] = _now()
            state.pop('pid', None)
        now = _now()
        conn.execute(
            'UPDATE run_state SET value = ?, updated_at = ? WHERE key = ?',
            (json.dumps(state), now, key),
        )
        if owns_conn:
            conn.commit()
        return True
    finally:
        if owns_conn:
            conn.close()


def get_run_lease(key, conn=None):
    raw = get_run_state(key, conn=conn)
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def run_lease_process_is_running(key, conn=None):
    """Whether a named job lease points at a worker process that still exists."""
    state = get_run_lease(key, conn=conn) or {}
    return (
        state.get('status') in ('starting', 'running')
        and bool(state.get('pid'))
        and _process_is_running(state['pid'])
    )


def reset_orphaned_run_lease(key, detail='Reset by operator', conn=None):
    """Mark an active lease failed, but never unlock a live worker."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT value FROM run_state WHERE key = ?', (key,)
        ).fetchone()
        try:
            state = json.loads(row['value']) if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            state = {}
        if state.get('status') not in ('starting', 'running'):
            conn.rollback()
            return False
        if state.get('pid') and _process_is_running(state['pid']):
            conn.rollback()
            return False
        state.update(status='failed', detail=detail, finished_at=_now())
        state.pop('pid', None)
        conn.execute(
            'UPDATE run_state SET value = ?, updated_at = ? WHERE key = ?',
            (json.dumps(state), _now(), key),
        )
        conn.commit()
        return True
    finally:
        if owns_conn:
            conn.close()



# --------------------------------------------------------------------------
# Retention: purging finished leads, and keeping their numbers
# --------------------------------------------------------------------------
# retention.py owns the policy (how old is too old). Everything below is the
# SQL half of it, here because db.py is the only module that touches SQLite.
#
# The ordering key is the same one History sorts on -- see LEAD_ORDERS
# ['finished']. A lead is aged from when it finished, not from when its email
# arrived: a lead reviewed last week is a week old even if the email it came
# from is a year old, and purging on email date would delete leads the
# operator had only just dealt with.
FINISHED_AT_SQL = 'COALESCE(submitted_at, reviewed_at, updated_at, created_at)'

# Metric names in purged_lead_stats. Each is a family of buckets:
#   PURGED_LEAD_STATUS      bucket = lead status        -> lead_count
#   PURGED_MESSAGE_STATUS   bucket = message status     -> lead_count
#   PURGED_ORACLE           bucket = summary key        -> lead_count
#   PURGED_OPPORTUNITY      bucket = currency code      -> lead_count + amount
#   PURGED_WON              bucket = currency code      -> amount
#   PURGED_ORDER            bucket = currency code      -> amount
PURGED_LEAD_STATUS = 'lead_status'
PURGED_MESSAGE_STATUS = 'message_status'
PURGED_ORACLE = 'oracle'
PURGED_OPPORTUNITY = 'opportunity'
PURGED_WON = 'opportunity_won'
PURGED_ORDER = 'order_line'

# The five counts oracle_dashboard_summary() reports. Rolled up with exactly
# the same definitions -- if these two drift apart, the funnel starts telling
# a different story about purged leads than about live ones.
_ORACLE_ROLLUP_SQL = """
    SELECT
      (SELECT COUNT(*) FROM oracle_lead_snapshots
         WHERE lead_id IN ({ids})
           AND (oracle_status IS NOT NULL OR oracle_lead_number IS NOT NULL)) synced,
      (SELECT COUNT(*) FROM oracle_lead_snapshots
         WHERE lead_id IN ({ids})
           AND (qualified_at IS NOT NULL
                OR UPPER(COALESCE(oracle_status,'')) = 'QUALIFIED')) qualified,
      (SELECT COUNT(*) FROM oracle_lead_snapshots s
         WHERE s.lead_id IN ({ids})
           AND (s.converted_at IS NOT NULL OR EXISTS (
                SELECT 1 FROM oracle_opportunities o WHERE o.lead_id = s.lead_id
           ))) converted,
      (SELECT COUNT(DISTINCT lead_id) FROM oracle_opportunities
         WHERE lead_id IN ({ids})
           AND UPPER(COALESCE(status,'')) IN ('WON','CLOSED_WON','CLOSED WON')) won,
      (SELECT COUNT(*) FROM oracle_lead_snapshots
         WHERE lead_id IN ({ids}) AND sync_error IS NOT NULL) errors
"""


def leads_due_for_purge(rejected_before, finished_before, conn=None):
    """
    Finished leads old enough to delete, oldest first.

    Two rules, and a lead matching either is due:
      - a 'rejected' lead that finished before `rejected_before`
      - any finished lead that finished before `finished_before`

    Only lead_stages.HISTORY statuses are considered. A lead still in Pending
    Review or the Input Queue is unfinished work no matter how old it is, and
    deleting it would take away the "way onward and a way back" that
    lead_stages.py exists to guarantee. An ancient lead sitting in the queue is
    a prompt to deal with it, not something to quietly bin.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        placeholders = ', '.join('?' for _ in lead_stages.HISTORY)
        rows = conn.execute(
            f"""SELECT id, status, message_id,
                       {FINISHED_AT_SQL} AS finished_at,
                       CASE WHEN status = 'rejected' AND {FINISHED_AT_SQL} < ?
                            THEN 'rejected' ELSE 'aged' END AS rule
                FROM leads
                WHERE status IN ({placeholders})
                  AND ((status = 'rejected' AND {FINISHED_AT_SQL} < ?)
                       OR {FINISHED_AT_SQL} < ?)
                ORDER BY finished_at, id""",
            (rejected_before, *lead_stages.HISTORY, rejected_before, finished_before),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def purge_lead_file_paths(lead_ids, conn=None):
    """
    Every file on disk that belongs only to these leads, so the caller can
    delete it once the rows are gone.

    A message's cached body is only listed when no surviving lead still points
    at that message: one trade-show spreadsheet email becomes many leads, and
    deleting the cached body while a sibling lead is still in review would
    empty that lead's email thread on the dashboard.
    """
    if not lead_ids:
        return []
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        ids = ', '.join('?' for _ in lead_ids)
        paths = [
            row[0] for row in conn.execute(
                f"""SELECT submit_screenshot_path FROM leads
                    WHERE id IN ({ids}) AND submit_screenshot_path IS NOT NULL""",
                list(lead_ids),
            )
        ]
        for row in conn.execute(
            f"""SELECT body_cache_path, translated_body_cache_path,
                       message_file_path, attachments
                FROM messages
                WHERE id IN (SELECT message_id FROM leads
                             WHERE id IN ({ids}) AND message_id IS NOT NULL)
                  AND NOT EXISTS (SELECT 1 FROM leads survivor
                                  WHERE survivor.message_id = messages.id
                                    AND survivor.id NOT IN ({ids}))""",
            list(lead_ids) * 2,
        ):
            paths.extend(row[0:3])
            # Attachments are stored as JSON metadata; each entry may name a
            # saved copy of the customer's file. Tolerates a legacy or
            # malformed value rather than aborting a purge over it.
            try:
                for attachment in json.loads(row['attachments'] or '[]'):
                    if isinstance(attachment, dict):
                        paths.append(attachment.get('saved_path')
                                     or attachment.get('path'))
            except (TypeError, ValueError):
                pass
        return [p for p in paths if p]
    finally:
        if owns_conn:
            conn.close()


def purge_leads(lead_ids, conn=None):
    """
    Delete these leads and everything hanging off them, after adding their
    numbers to purged_lead_stats.

    The rollup happens first and in the same transaction as the delete. If the
    two were separate, a crash between them would either lose the numbers
    forever (delete first) or double-count them on the retry (roll up first,
    separately) -- and there is no way to detect either afterwards, because the
    rows the count was derived from are exactly what is gone.

    Returns a summary dict of what was removed.
    """
    if not lead_ids:
        return {'leads': 0, 'messages': 0, 'by_status': {}}
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        lead_ids = [int(i) for i in lead_ids]
        ids = ', '.join('?' for _ in lead_ids)

        by_status = {
            row['status']: row['count'] for row in conn.execute(
                f"""SELECT status, COUNT(*) AS count FROM leads
                    WHERE id IN ({ids}) GROUP BY status""", lead_ids)
        }
        oracle = dict(conn.execute(
            _ORACLE_ROLLUP_SQL.format(ids=ids), lead_ids * 5).fetchone())
        opportunities = conn.execute(
            f"""SELECT COALESCE(currency_code, 'Unknown') currency,
                       COUNT(*) opportunity_count,
                       SUM(COALESCE(revenue_amount, 0)) pipeline_value,
                       SUM(CASE WHEN UPPER(COALESCE(status,'')) IN
                            ('WON','CLOSED_WON','CLOSED WON')
                           THEN COALESCE(revenue_amount,0) ELSE 0 END) won_value
                FROM oracle_opportunities WHERE lead_id IN ({ids})
                GROUP BY COALESCE(currency_code, 'Unknown')""", lead_ids).fetchall()
        order_lines = conn.execute(
            f"""SELECT COALESCE(currency_code, 'Unknown') currency,
                       SUM(COALESCE(net_amount, 0)) order_value
                FROM oracle_order_lines
                WHERE lead_id IN ({ids})
                  AND order_number IS NOT NULL AND order_number != ''
                GROUP BY COALESCE(currency_code, 'Unknown')""", lead_ids).fetchall()
        # Messages that will be orphaned by this delete -- counted here, while
        # the leads that keep them alive still exist.
        orphan_messages = [
            row['id'] for row in conn.execute(
                f"""SELECT id FROM messages
                    WHERE id IN (SELECT message_id FROM leads
                                 WHERE id IN ({ids}) AND message_id IS NOT NULL)
                      AND NOT EXISTS (SELECT 1 FROM leads survivor
                                      WHERE survivor.message_id = messages.id
                                        AND survivor.id NOT IN ({ids}))""",
                lead_ids * 2)
        ]
        message_statuses = {}
        if orphan_messages:
            message_ids = ', '.join('?' for _ in orphan_messages)
            message_statuses = {
                row['status']: row['count'] for row in conn.execute(
                    f"""SELECT status, COUNT(*) AS count FROM messages
                        WHERE id IN ({message_ids}) GROUP BY status""",
                    orphan_messages)
            }

        _add_purged_stats(conn, PURGED_LEAD_STATUS,
                          {k: {'lead_count': v} for k, v in by_status.items()})
        _add_purged_stats(conn, PURGED_MESSAGE_STATUS,
                          {k: {'lead_count': v} for k, v in message_statuses.items()})
        _add_purged_stats(conn, PURGED_ORACLE,
                          {k: {'lead_count': v} for k, v in oracle.items() if v})
        _add_purged_stats(conn, PURGED_OPPORTUNITY, {
            row['currency']: {'lead_count': row['opportunity_count'],
                              'amount': row['pipeline_value'] or 0}
            for row in opportunities})
        _add_purged_stats(conn, PURGED_WON, {
            row['currency']: {'amount': row['won_value'] or 0}
            for row in opportunities})
        _add_purged_stats(conn, PURGED_ORDER, {
            row['currency']: {'amount': row['order_value'] or 0}
            for row in order_lines})

        # Children first: foreign keys are ON, so the lead delete is refused
        # while any of these still point at it.
        for table in ('oracle_lifecycle_events', 'oracle_order_lines',
                      'oracle_opportunities', 'oracle_lead_snapshots',
                      'lead_replies', 'outlook_actions'):
            conn.execute(f'DELETE FROM {table} WHERE lead_id IN ({ids})', lead_ids)
        # A surviving lead may be marked as a duplicate OF one being purged.
        # Clearing the pointer keeps that lead, and its "duplicate of" note is
        # already in field_warnings -- the alternative is either a foreign key
        # error or purging a lead that is not due.
        conn.execute(
            f'UPDATE leads SET duplicate_of_lead_id = NULL '
            f'WHERE duplicate_of_lead_id IN ({ids}) AND id NOT IN ({ids})',
            lead_ids * 2)
        conn.execute(f'DELETE FROM leads WHERE id IN ({ids})', lead_ids)

        if orphan_messages:
            message_ids = ', '.join('?' for _ in orphan_messages)
            conn.execute(
                f'DELETE FROM messages WHERE id IN ({message_ids})', orphan_messages)
        # Conversation context nothing references any more. thread_messages is
        # keyed by conversation, not by lead, so it can only be cleaned up once
        # the last message of its conversation has gone.
        conn.execute(
            """DELETE FROM thread_messages
               WHERE conversation_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM messages
                                 WHERE messages.conversation_id
                                       = thread_messages.conversation_id)""")

        if owns_conn:
            conn.commit()
        return {
            'leads': sum(by_status.values()),
            'messages': len(orphan_messages),
            'by_status': by_status,
        }
    finally:
        if owns_conn:
            conn.close()


def _add_purged_stats(conn, metric, buckets):
    """Add to a metric's running totals. Never overwrites: every purge run
    adds to what earlier runs already counted."""
    for bucket, values in buckets.items():
        conn.execute(
            """INSERT INTO purged_lead_stats (metric, bucket, lead_count, amount)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(metric, bucket) DO UPDATE SET
                   lead_count = lead_count + excluded.lead_count,
                   amount = amount + excluded.amount""",
            (metric, str(bucket), int(values.get('lead_count', 0) or 0),
             float(values.get('amount', 0) or 0)),
        )


def purged_stats(conn=None):
    """Everything purged so far, as {metric: {bucket: {count, amount}}}."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        stats = {}
        for row in conn.execute('SELECT * FROM purged_lead_stats'):
            stats.setdefault(row['metric'], {})[row['bucket']] = {
                'count': row['lead_count'], 'amount': row['amount'],
            }
        return stats
    finally:
        if owns_conn:
            conn.close()


def lifetime_lead_counts(conn=None):
    """
    Lead counts per status over the life of the pipeline: the leads still in
    the database plus the ones retention has since deleted.

    count_leads() stays live-only on purpose -- History paginates on it, and a
    page count that included deleted leads would offer pages with nothing on
    them. This is for the status page, where the question is "how many leads
    has this pipeline handled", and the honest answer does not drop by 200 the
    morning the retention job first runs.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        counts = {
            row['status']: row['count'] for row in conn.execute(
                'SELECT status, COUNT(*) AS count FROM leads GROUP BY status')
        }
        for bucket, values in purged_stats(conn=conn).get(
                PURGED_LEAD_STATUS, {}).items():
            counts[bucket] = counts.get(bucket, 0) + values['count']
        return counts
    finally:
        if owns_conn:
            conn.close()


def lifetime_message_counts(conn=None):
    """Message counts per status, including messages deleted with their leads."""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        counts = dict(count_messages_by_status(conn=conn))
        for bucket, values in purged_stats(conn=conn).get(
                PURGED_MESSAGE_STATUS, {}).items():
            counts[bucket] = counts.get(bucket, 0) + values['count']
        return counts
    finally:
        if owns_conn:
            conn.close()


def lifetime_oracle_summary(conn=None):
    """
    oracle_dashboard_summary() plus the Oracle outcomes of purged leads.

    Same shape as the live summary, so the funnel and the rate cards read from
    one thing either way. A purged lead's Oracle record still exists in Oracle
    -- deleting our copy of it is a local retention decision, and it should not
    look as though the lead was never qualified.
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        summary = oracle_dashboard_summary(conn=conn)
        purged = purged_stats(conn=conn)
        for key, values in purged.get(PURGED_ORACLE, {}).items():
            if key in summary:
                summary[key] = summary[key] + values['count']

        won_by_currency = purged.get(PURGED_WON, {})
        summary['currencies'] = _merge_currency_rows(
            summary['currencies'], 'currency',
            {currency: {
                'pipeline_value': values['amount'],
                'won_value': won_by_currency.get(currency, {}).get('amount', 0),
             } for currency, values in purged.get(PURGED_OPPORTUNITY, {}).items()},
        )
        summary['order_currencies'] = _merge_currency_rows(
            summary['order_currencies'], 'currency',
            {currency: {'order_value': values['amount']}
             for currency, values in purged.get(PURGED_ORDER, {}).items()},
        )
        return summary
    finally:
        if owns_conn:
            conn.close()


def _merge_currency_rows(live_rows, key, purged_by_currency):
    """Add purged money into the live per-currency rows, keeping them sorted.

    A currency can appear in either side alone: the last EUR lead may have been
    purged months ago, or the first one may have arrived this morning."""
    merged = {row[key]: dict(row) for row in live_rows}
    for currency, amounts in purged_by_currency.items():
        row = merged.setdefault(currency, {key: currency})
        for field, amount in amounts.items():
            row[field] = (row.get(field) or 0) + (amount or 0)
    return [merged[currency] for currency in sorted(merged)]


if __name__ == '__main__':
    init_db()
    print(f'Initialized database at {DB_PATH}')
