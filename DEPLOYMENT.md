# Lead Input Pipeline — Deployment & Scheduling Guide

This document contains step-by-step instructions for IT and system administrators to host, configure, and schedule the **Bal Seal Engineering Lead Input Pipeline**.

---

## 1. System Requirements

- **Operating System:** Windows Server / Windows 10/11 or Linux VM.
- **Python:** 3.10 – 3.14
- **Database:** SQLite (built-in file database `pipeline.db`, zero setup required).
- **Network Access:**
  - Inbound: Port `5151` for internal team dashboard access.
  - Outbound: HTTPS (`https://cagc.fa.us1.oraclecloud.com`) to Oracle Sales Cloud REST API & Microsoft Graph API (if using Graph for mail ingestion).

---

## 2. Server Installation

1. **Clone Repository / Extract Project:**
   ```bash
   cd "C:\Project Docs\Lead Input pipeline"
   ```

2. **Create Virtual Environment & Install Dependencies:**
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

3. **Configure Environment Variables (`.env`):**
   Create a `.env` file in the root folder using `.env.example` as a template:
   ```env
   ORACLE_USERNAME=your_oracle_username
   ORACLE_PASSWORD=your_oracle_password
   DASHBOARD_SECRET_KEY=generate_random_secret_here
   ```

---

## 3. Automated Scheduling Setup

Reading the mailbox and sending leads to Oracle remain human-triggered. The
dashboard stays running, and a separate read-only reporting task refreshes
Oracle lead outcomes daily.

| Task | Command | Schedule | Purpose |
| :--- | :--- | :--- | :--- |
| **Oracle Outcome Sync** | `python oracle_sync.py` | **Daily (default 6:00 AM)** | Read-only refresh of lead qualification, conversion, opportunities, and estimated revenue. |
| **Review Dashboard** | `pythonw dashboard\serve.py` | **At logon, stays up** | Serves the review dashboard on http://127.0.0.1:5151. Windowless, restarts up to 3× on failure, a 10-minute heartbeat restarts it if it ever dies, and no execution time limit (the default would kill it after 3 days). |
| **Lead Retention Purge** | `python purge_old_leads.py --apply` | **Daily (default 7:00 AM)** | Permanently deletes finished leads past the retention policy — rejected after 30 days, everything else finished after a year — and their cached email. Leads awaiting review are never touched. Runs *after* the Oracle sync so a lead's final outcome is counted before it goes. |

The dashboard task runs as the signed-in user with an interactive logon type,
in the same session as Outlook — its reply/forward/delete actions and the
ingest runs it launches drive Outlook through COM and cannot work from a
`SYSTEM` service. It therefore stops when that user signs out and starts again
at the next logon.

Retired on 2026-08-07, and deleted by `setup_scheduler.bat` if an older run
registered them: `LeadPipeline_Ingest_AM` (8:45 AM), `LeadPipeline_Ingest_PM`
(4:00 PM) and `LeadPipeline_Submit_OSC` (5:00 PM).

### Windows Server (Automated Setup)
Double-click or run the included batch script in PowerShell:
```powershell
.\setup_scheduler.bat
.\setup_oracle_sync_task.ps1
.\setup_retention_task.ps1
```

**Retention deletes customer data permanently and writes no backup.** Check
what it would remove before registering the task:

```powershell
py purge_old_leads.py            # dry run, deletes nothing
```

README "Retention" documents the policy, and why the status page's totals,
conversion rates and funnel do not drop when leads are deleted.

**Ingestion runs on demand:** the **Click to ingest leads** button on Pending
Review, or `py pipeline_ingest.py`.

**Submission runs when a human acts:** **Approve All & Send to Queue** on the
Input Queue, **Send to Oracle now** for anything already approved (a lead whose
first attempt hit a network error is put back to `approved` with a retry time,
and this is what picks it up), or the retry action on an uncertain submission.

Because nothing is scheduled, the stale-claim recovery in both entry points
only runs when you start one. A message left in `processing` or a lead left in
`submitting` by an interrupted run stays that way until the next ingest or
submit you trigger.

Note that the stale-claim recovery in both entry points only runs when that
entry point runs. Without a schedule, a lead left in `submitting` or a message
left in `processing` by an interrupted run stays stuck until you next run the
pipeline by hand.

> Windows only. Ingestion drives the Outlook desktop app through COM
> (`pywin32`), which has no Linux equivalent — a Linux cron installer used to
> live here and was removed on 2026-07-31 because it could never work.

---

## 4. Running the Dashboard Web App

To run the web dashboard for internal team access:

### Production Execution (Waitress WSGI Server)
```powershell
.venv\Scripts\python.exe -m dashboard.serve
```
The dashboard binds to `http://0.0.0.0:5151` (or your server's IP / internal DNS host).

---

## 5. Summary of Key Pipeline Files

- `pipeline_ingest.py` — Email & attachment lead ingestion & AI extraction engine.
- `pipeline_submit.py` — Oracle Sales Cloud REST API submission & retry engine.
- `oracle_api.py` — Oracle REST API payload builder & pre-submission deduplication checker.
- `dashboard/app.py` — Lead review dashboard (Flask app).
- `outlook_ingest.py` — Sales inbox reader (Outlook COM); the only source of leads.
- `setup_scheduler.bat` — Windows Task Scheduler installer (optional).
