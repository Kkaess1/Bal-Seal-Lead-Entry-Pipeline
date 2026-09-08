# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local Windows pipeline that reads a shared Outlook mailbox, extracts and screens
leads with an LLM, presents them for human review in a Flask dashboard, and submits
approved records to Oracle Sales Cloud. Single-user, single-machine, SQLite-backed.

`README.md` is unusually detailed and is the authority on *why* things are the way
they are — most non-obvious decisions are documented there with the date and the
incident that caused them. Read the relevant section before changing behaviour.

## Commands

```powershell
py -m unittest discover -s tests -v          # full suite (~500 tests, ~60s)
py -m unittest tests.test_lead_stages        # one module
py -m unittest tests.test_lead_stages.EveryStatusIsOnExactlyOnePageTests.test_an_interrupted_ingest_still_reaches_a_reviewer

py pipeline_ingest.py                        # read the Sales inbox now
py pipeline_ingest.py --recent 10            # bounded test run
py pipeline_submit.py                        # send approved leads to Oracle
py oracle_sync.py                            # refresh Oracle outcomes (read-only)
py purge_old_leads.py                        # dry run: what retention would delete
py purge_old_leads.py --apply                # actually delete it
py -m dashboard.serve                        # dashboard under Waitress (normal operation)
```

There is no pytest, no linter, and no build step. `py dashboard/app.py` starts the
Flask dev server and is for debugging only.

Setup, credential storage (`py secrets_store.py OPENAI_API_KEY <key>`) and scheduled
tasks are covered in README.md "Setup" and DEPLOYMENT.md.

## Editing the dashboard requires a restart

The dashboard runs under Waitress via the `LeadPipeline_Dashboard` scheduled task,
which starts at logon. **Waitress reloads neither Python modules nor Jinja templates**
(Flask leaves `TEMPLATES_AUTO_RELOAD` off outside debug), so a running dashboard
serves whatever the code looked like when the process started. A stale page is far
more often this than a code bug — it has cost real debugging time.

```powershell
Stop-ScheduledTask -TaskName LeadPipeline_Dashboard
Start-ScheduledTask -TaskName LeadPipeline_Dashboard
```

`.venv\Scripts\pythonw.exe` is a uv trampoline that execs the real interpreter, so
two PIDs per dashboard is normal and is not two servers.

## Architecture

Four command-line entry points and one web app, sharing `pipeline.db`. No ingest
and no upload runs on a timer — every one starts from a human clicking something.
Two things are scheduled and neither reads mail nor sends anything: the dashboard,
and the daily retention purge.

```
Outlook (COM) --> pipeline_ingest.py --> SQLite --> dashboard/app.py --> pipeline_submit.py --> Oracle REST
                                            ^                                                        |
                                            +--------------- oracle_sync.py (read-only) <------------+
```

- **`pipeline_ingest.py`** — mail in, leads staged. Calls `outlook_ingest` (COM),
  `lead_extractor` (LLM fields), `fraud_screen`, `territory_map`. Never touches Oracle.
- **`pipeline_submit.py`** — only ever acts on `status='approved'`, claiming each lead
  atomically so overlapping runs cannot double-send.
- **`oracle_sync.py`** — pulls lead/opportunity/order outcomes back. GET only; it
  never POSTs or PATCHes.
- **`dashboard/app.py`** — ~2000 lines, all routes. `db.py` (~2300 lines) is the only
  module that touches SQLite.
- **`retention.py` / `purge_old_leads.py`** — deletes finished leads once they are
  old enough (rejected after 30 days, everything else finished after a year) and
  the cached email with them. Never touches a lead still in Pending Review or the
  Input Queue, at any age. Dry run by default; `LeadPipeline_Retention` runs it
  daily with `--apply`.
- **`manual_lead.py`** — the one lead source that is not email: the Create lead
  button on Pending Review, reading pasted text or a screenshot. Shares the field
  list, rank/market rules and territory map with ingest, but deliberately skips the
  fraud screen and the head-office lookup. README "Entering a lead by hand" says why,
  and why that does not contradict "One way in".

### lead_stages.py is the single source of truth for status

Every status belongs to exactly one of three pages (Pending Review / Input Queue /
History), and `lead_stages.py` is the one place that says which. Pages, route guards
and templates read `PENDING` / `STAGED` / `SENDING` / `HISTORY` / `EDITABLE` /
`RETURNABLE` / `RECALLABLE` from it rather than hardcoding status strings.
`tests/test_lead_stages.py` renders all three pages with a lead in every status and
fails unless each appears on exactly one. Adding a status without updating this
module breaks that test by design.

The invariant it protects: **a lead always has a way onward and a way back.** Several
dead ends have been closed here — a `duplicate` with no route back, a lead stuck in
`submitting`, an `approved` lead missing a required field. If you add a status or a
transition, make sure a human can still reach the lead from a page.

### Purging a lead must not change a number on the status page

Every Oracle reporting table is keyed to `leads(id)` with a foreign key and
foreign keys are ON, so a purged lead takes its qualification, opportunities and
revenue with it. `db.purge_leads()` therefore rolls each lead's contribution into
`purged_lead_stats` **in the same transaction that deletes it**, and the status
page reads `db.lifetime_lead_counts()` / `lifetime_message_counts()` /
`lifetime_oracle_summary()` rather than the live counts.

Two things follow. If you add a figure to that page, it needs a lifetime source
or it will quietly shrink the first time the retention job runs. And if you
change what `oracle_dashboard_summary()` counts, change the rollup in
`db.purge_leads()` to match — a drift between them makes the funnel tell a
different story about old leads than about new ones.

`count_leads()` stays live-only: History paginates on it.
`purged_lead_stats` holds counts and money only — it survives a purge, so
anything personal in it would defeat the purge.

### Two gates before Oracle

`oracle_api.submission_blockers(lead)` is one definition checked in several places:
`approve()`, `/queue/approve_all`, `/queue/submit_now`, and the submitter itself as a
backstop. A lead that fails goes back to **Pending Review with the reason attached**,
never to History — nothing reached Oracle, so it is work to do, not an outcome.

### The dashboard has no authentication

`dashboard/serve.py` refuses to bind anything but loopback, and says why. Every POST
route is CSRF-checked against a session token. HOSTING_SECURITY.md is the plan for
putting it behind real auth; do not weaken the loopback guard without reading it.

## AI conventions

One pinned model in `ai_provider.DEFAULT_OPENAI_MODEL`, used by every caller
(`lead_extractor` on both its paths, `fraud_screen`, `ai_column_mapper`); none
passes an override. `call_llm_with_images()` is the same model with a screenshot
attached — the Responses API needs the role/content-parts form once an image is
there, which is why it is a separate function rather than an argument. The
dated snapshot is deliberate so extraction quality cannot shift without a commit.

**Two different cleaners, and the difference matters:**

- `email_cleaner.clean_email_body()` — for human display and for Oracle's Lead
  Details. Strips quoted history; signatures survive.
- `email_cleaner.compact_for_ai()` — for the model. **Keeps** quoted history and a
  form's contact rows, because a reply's company and email frequently exist only in
  the quoted original. Over 92 sample emails the display cleaner loses a contact
  address in 70 of them; compaction loses one.

Never swap one for the other to reduce duplication.

Prompt wording in `lead_extractor.py` that looks redundant is load-bearing — README
documents a live A/B with a control arm in which a shorter prompt was measurably
worse in three distinct ways. `tests/test_extraction_prompt.py` pins field names,
market options and JSON keys, but cannot catch that class of regression.

The fraud screen's LLM pass **never** sets `auto_reject`. Only the deterministic rule
layer can, and only for unambiguous junk. That split is the prompt-injection defence:
nothing an email can make the model say is able to skip human review.

## Test conventions

Any test touching the database must redirect `db.DB_PATH` to a temp file, or it
writes into the real `pipeline.db` — this has leaked test leads into production
before. The canonical form, used by 18 test modules:

```python
self.tempdir = tempfile.TemporaryDirectory()
self.addCleanup(self.tempdir.cleanup)
self.original_db_path = db.DB_PATH
db.DB_PATH = Path(self.tempdir.name) / 'test.db'
self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
db.init_db()
```

Rendering a dashboard page in a test usually needs
`patch('dashboard.app.db.get_lead_email_thread', return_value=[])`, because it reads
cached email bodies off disk.

Test names are full sentences describing the behaviour being protected
(`test_a_lead_a_run_is_sending_is_never_taken_back`), and docstrings explain the
incident that motivated the test. Match that style.

## PII and git

The repo is private and holds real customer data on disk. `.gitignore` covers
`pipeline.db`, `*.db-shm` / `*.db-wal` (a WAL holds the same PII as its database),
`email_cache/`, `email_attachments/`, `email_messages/`, `input/`, `logs/`, and
`reference/*` except its README.

Check what is actually staged before any `git add -A`. Scratch and one-off scripts
(`scratch_*.py`, `backfill_*.py`, `verify_*.py`) are left untracked on purpose;
several produce JSON containing customer names.

`purge_old_leads.py` deletes customer data permanently and writes no backup, which
is the point of it. It defaults to a dry run for the same reason the Oracle write
tooling does; only `--apply` deletes anything.

Oracle write tooling (`scratch_retire_leads.py`) is deliberately kept separate from
the query that chooses records (`scratch_owner_inventory.py`), defaults to a dry run,
and writes an undo file before applying. Keep that split — a script that both selects
and writes is one typo away from mass-updating a rep's entire book.

## Windows and Outlook constraints

Ingestion and the reply / forward / delete buttons drive the Outlook desktop app
through COM, which needs an interactive logged-in session with the Sales mailbox
open. That is why the dashboard runs as the signed-in user rather than as a service,
and why it cannot simply be moved to a server. Emails already handled are matched by
category **colour**, not name — the green tag in this mailbox is called `Completed`.
