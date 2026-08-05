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

## 3. Automated Scheduling Setup (Ingest & OSC Submit)

The application uses automated background tasks to ingest and submit leads on the following schedule:

| Task | Command | Schedule | Purpose |
| :--- | :--- | :--- | :--- |
| **Morning Ingest** | `python pipeline_ingest.py` | **8:00 AM Daily** | Fetches new lead emails, runs AI extraction, screens fraud risk, and stages leads in `pending_review`. |
| **Afternoon Ingest** | `python pipeline_ingest.py` | **4:00 PM Daily** | Fetches afternoon lead emails and stages them in `pending_review`. |
| **OSC Batch Submit** | `python pipeline_submit.py` | **5:00 PM Daily** | Processes all human-approved leads in SQLite and submits them to Oracle Sales Cloud via REST API. |

### Windows Server (Automated Setup)
Double-click or run the included batch script in PowerShell:
```powershell
.\setup_scheduler.bat
```
This automatically registers the three tasks in **Windows Task Scheduler**.

Scheduling is optional. The pipeline also runs entirely on demand: `py
pipeline_ingest.py` when you want to pull mail, and the dashboard's "Approve
All & Send to Queue" button launches `pipeline_submit.py` immediately.

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
