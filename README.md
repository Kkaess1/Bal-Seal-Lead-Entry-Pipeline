# Bal Seal lead-entry pipeline

Local Windows pipeline that reads the configured Outlook shared mailbox,
extracts and screens leads, presents them for review, and submits approved
records to Oracle Sales Cloud.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\playwright install chromium
Copy-Item .env.example .env
py db.py
```

Store production credentials in Windows Credential Manager:

```powershell
py secrets_store.py GEMINI_API_KEY <key>
py secrets_store.py ORACLE_USERNAME <username>
py secrets_store.py ORACLE_PASSWORD <password>
```

Set a persistent random `DASHBOARD_SECRET_KEY` in `.env`.

## Commands

```powershell
py pipeline_ingest.py
py pipeline_ingest.py --recent 10
py pipeline_submit.py
py -m dashboard.serve
py -m unittest discover -s tests -v
```

The Flask development server (`py dashboard/app.py`) is intended only for
local debugging. Use the Waitress command above for normal operation.

## Oracle submission

Approved leads submit through the Oracle Sales Cloud REST API by default:

```json
"submission_method": "api"
```

The API endpoint is derived from `oracle.base_url`:

```text
https://cagc.fa.us1.oraclecloud.com/crmRestApi/resources/11.13.18.05/leads
```

The Playwright browser submitter is still available as a fallback by setting:

```json
"submission_method": "playwright"
```

The API path stores Oracle's returned `LeadId`/`LeadNumber` in the local
database. Custom market labels are translated to Oracle's API lookup values in
`oracle_api.MARKET_TO_API_VALUE`.

## State and recovery

- Message and lead ingestion use stable source keys, so retries update the same
  work item instead of creating another lead.
- `new` and `error` messages are retried from the cached body on the next run.
- Approved leads are atomically claimed as `submitting`.
- A crash after clicking Oracle Save becomes `submission_unknown`. Reconcile
  that record in Oracle before restoring it; automatic retry could duplicate it.
- Claims left in `submitting` for more than 90 minutes are moved to
  `submission_unknown` at the next submission run.

The `/status` dashboard page shows queue counts and the most recent run state.
Logs are written under `logs/`.
