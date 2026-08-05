# Hosting the dashboard internally — hardening plan

Status: **draft for review. Nothing here is implemented yet.**

The dashboard was built to run on `127.0.0.1`, where being reachable was the
same thing as being authorized. Publishing it on an internal host breaks that
assumption, and several things that are currently fine become serious.

`DEPLOYMENT.md` already tells IT to bind `0.0.0.0:5151` (§4) and to put
`ORACLE_PASSWORD` in a plaintext `.env` (§2.3). Following that guide today
produces an unauthenticated web application that can write to production Oracle,
on a host holding CRM credentials in a readable file. That guide needs to change
alongside the code.

---

## Phase 0 — blocking. Do not publish without these.

### 0.1 Authentication in front of every route

There is no login, no auth decorator, and no IP restriction. `before_request`
([app.py:199](dashboard/app.py#L199)) validates CSRF tokens on POSTs, which
authenticates the *form*, never the *person*. `/leads/<id>/approve` is an
unauthenticated HTTP endpoint that writes to production Oracle.

Recommended: **IIS (or nginx) reverse proxy in front of Waitress, with
Integrated Windows Authentication.** In a Bal Seal environment this is usually
the least-effort route and yields real domain usernames for free.

Two details that decide whether this actually works:

- **Waitress must stay bound to `127.0.0.1`** so the proxy is the only possible
  client. `config.json` already defaults to this — keep it, and correct
  `DEPLOYMENT.md`.
- **The app must fail closed.** It reads the authenticated user from a header
  the proxy sets (`X-Remote-User` or `REMOTE_USER`) and rejects any request
  without one. If Waitress were reachable directly, a client could forge that
  header and impersonate anyone — which is why the previous point is not
  optional.

### 0.2 Authorization, not just authentication

"Any authenticated employee" is not the right audience for every customer
inquiry the company has received. Restrict to an AD group (sales + pipeline
admins) and check membership, not merely that a user is signed in.

### 0.3 TLS

Waitress serves plain HTTP. Session cookies and every customer's name, email,
phone, address and full message body currently cross the network in cleartext.
Terminate TLS at the proxy with an internal CA certificate, redirect HTTP, and
set the cookie flags the app does not set today:

```python
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)
```

### 0.4 Secrets out of `.env`

Move `ORACLE_USERNAME`, `ORACLE_PASSWORD` and `GEMINI_API_KEY` into Windows
Credential Manager via `secrets_store.py`, stored **under the identity that runs
the process** (Credential Manager is per-user). Then delete them from `.env`.

`DASHBOARD_SECRET_KEY` needs a code change first: [app.py:27](dashboard/app.py#L27)
reads it from the environment only and silently falls back to a random
per-process key, which would reset every session and CSRF token on restart.
Route it through `secrets_store` and refuse to start without it.

**Rotate the Oracle password as part of this.** It has been sitting in a
plaintext file on a workstation; moving it without rotating preserves the
exposure rather than ending it.

---

## Phase 1 — accountability

### 1.1 Record who actually did it

`reviewed_by` is hardcoded to the string `'dashboard'`
([app.py:265](dashboard/app.py#L265), [327](dashboard/app.py#L327),
[344](dashboard/app.py#L344)). The column, the form field and the plumbing all
exist — nothing supplies a value. Once 0.1 provides an authenticated username,
pass it through on approve, reject, save and approve-all. Without this you
cannot answer "who approved this lead", which matters for both accountability
and any review of who accessed customer data.

### 1.2 Keep the CRM credentials out of the web tier

`/queue/approve_all` spawns `pipeline_submit.py` as a child process
([app.py:273-274](dashboard/app.py#L273-L274)), so the web-facing identity needs
Oracle credentials today. Better: the button records intent only, and the
existing scheduled `pipeline_submit.py` task — running as a different account —
does the Oracle work. The exposed tier then never holds CRM credentials at all.

Cost: submissions happen on the schedule rather than instantly. If immediacy
matters, keep the button but understand that it is what forces credentials into
the web tier.

---

## Phase 2 — defence in depth

### 2.1 Security response headers

None are set today. Add via `after_request`:

| Header | Value | Why |
| :-- | :-- | :-- |
| `Content-Security-Policy` | `default-src 'self'; frame-ancestors 'none'` | Limits what injected markup could reach |
| `X-Content-Type-Options` | `nosniff` | Stops MIME-sniffing a response into script |
| `X-Frame-Options` | `DENY` | Clickjacking on approve/reject |
| `Referrer-Policy` | `same-origin` | Keeps lead IDs out of external referrers |

Note the templates use **no `|safe`**, so Jinja autoescaping is intact and
customer-controlled text renders inert. These headers are a second layer, not
the primary defence.

### 2.2 Least-privilege Oracle account

The pipeline needs to create leads and read for dedup. Confirm the account
cannot do more than that — a web-exposed application should not be one bug away
from broader CRM write access.

### 2.3 File system permissions

`pipeline.db`, `email_cache/`, `email_messages/` and `email_attachments/`
contain complete customer emails and contact data. ACL them to the service
identity and administrators only. They are currently protected by nothing more
than the machine being a workstation.

### 2.4 Retention

Nothing expires today. Decide how long cached email bodies, exported `.msg`
files and submitted leads should be kept, and enforce it. Every month of
retention is another month of PII to lose.

---

## Phase 3 — operations

- **Dependency patching.** Flask, Waitress, requests, openpyxl — a published app
  needs a patch cadence a localhost script did not.
- **Log review.** Submit and ingest logs land in `logs/`; nothing surfaces
  failures except the dashboard bell.
- **Backups.** `pipeline.db` is the only copy of review state and it is a single
  file. Back it up, and treat the backup as PII.

---

## Constraint worth designing around

Ingestion drives the **Outlook desktop app through COM**, which Microsoft does
not support in a service context — it wants a real interactive profile. That
argues for splitting the deployment:

- **Web tier** — the dashboard, behind IIS + auth + TLS, as a service account.
- **Ingest tier** — `pipeline_ingest.py` under Task Scheduler in the interactive
  session that owns the Outlook profile, as today.

Both share `pipeline.db`. SQLite handles that concurrency at this volume, and it
is already the shape the pipeline runs in.

---

## Suggested order

1. `DASHBOARD_SECRET_KEY` through `secrets_store`, secrets moved, Oracle password rotated (0.4)
2. Reverse proxy with Windows auth + TLS, Waitress stays on `127.0.0.1` (0.1, 0.3)
3. AD group check (0.2)
4. `reviewed_by` from the authenticated user (1.1)
5. Response headers (2.1)
6. File ACLs (2.3)
7. `DEPLOYMENT.md` rewritten to describe the hardened deployment rather than the current one

Items 1, 4, 5 and 7 are code and documentation changes in this repo. Items 2, 3
and 6 are infrastructure work that needs IT.
