# Bal Seal lead-entry pipeline

Local Windows pipeline that reads the configured Outlook shared mailbox,
extracts and screens leads, presents them for review, and submits approved
records to Oracle Sales Cloud.

## One way in

The Sales inbox (`outlook.mailbox_name` in `config.json`) is the only source
of leads. `outlook_ingest.py` is scoped to that mailbox specifically and
raises rather than falling back to the signed-in user's personal inbox.

There is no second ingestion path by design. A dormant Microsoft Graph backend
and a test-lead seeding script were removed on 2026-07-31 so that nothing can
introduce a lead that did not arrive as mail in that inbox. Spreadsheets are
still supported, but only as attachments on those emails.

Emails carrying a **green Outlook category** have already been entered and are
skipped. Green is matched by the category's colour, not its name — this
mailbox's tag is called `Completed`.

Writing that tag back is currently **off** while the system is being evaluated:

```json
"outlook": { "mark_reviewed": false }
```

With it off, nothing the pipeline does alters the real Sales mailbox. The
trade-off is that the green tag is also what stops an email being read again,
so an approved email keeps returning to the review queue on every ingest. The
submission-time duplicate guard is what stops those repeats reaching Oracle.
Set it to `true` to have the pipeline tag emails once every lead from them has
been approved or rejected.

Dashboard forwards use the shared mailbox identity configured by
`outlook.forward_from_address` (currently `sales@balseal.com`). The signed-in
Outlook user must have Exchange **Send As** or **Send on Behalf** permission
for that address; Outlook exposes a shared mailbox as a store rather than a
separate sending account.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
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

## AI model

One pinned model, in `ai_provider.DEFAULT_GEMINI_MODEL`:

```python
DEFAULT_GEMINI_MODEL = 'gemini-3.6-flash'
```

Used by all three AI callers — `lead_extractor` (field extraction),
`fraud_screen` (plausibility pass), `ai_column_mapper` (spreadsheet headers).
None passes a model override.

It was `gemini-flash-latest`, a floating alias Google repoints to each new Flash
release. Pinned on 2026-08-03 so extraction quality can't shift without a
commit: with a floating alias, a bad week of extraction is impossible to tell
apart from a prompt problem. The alias also drifts on price — Flash input went
from $0.30/M at 2.5 to $1.50/M at 3.6.

Flash rather than Pro because the inputs are short (median 96 tokens of body
across `email_cache/`) and the work is field extraction, not reasoning. Stable
rather than a `*-preview` build because previews get revised and withdrawn. At
roughly a cent per lead the tier is worth ~$30/year either way, so the choice is
made on accuracy and stability, not cost.

To evaluate a newer model, bump the constant and diff the extracted fields over
`email_cache/` before trusting it.

## Oracle submission

Approved leads submit through the Oracle Sales Cloud REST API. The endpoint is
derived from `oracle.base_url`:

```text
https://cagc.fa.us1.oraclecloud.com/crmRestApi/resources/11.13.18.05/leads
```

Oracle's returned `LeadId`/`LeadNumber` is stored on the local lead. Custom
market labels are translated to Oracle's API lookup values in
`oracle_api.MARKET_TO_API_VALUE`.

A Playwright browser-automation submitter used to exist as a fallback and was
removed on 2026-07-31 — it had not been the active path, and an unexercised
fallback tends not to work when it is finally needed.

### International leads

`country_codes.py` holds the full ISO 3166-1 table plus the aliases leads
actually arrive with (`UK`, `Deutschland`, `U.S.A.`, `South Korea`). A country
that resolves is sent as its alpha-2 code; one that does not is **omitted with a
warning** rather than sent verbatim, because Oracle's territory field will not
resolve an arbitrary string. The warning lands in the lead's `field_warnings`
and in the submit log.

`oracle.country_format` controls the wire format: `code` (default, sends `DE`)
or `name` (sends `Germany`, Oracle's dropdown label). Codes are what this tenant
has accepted for US/CA; if Oracle ever rejects a country, flip this to `name`
before changing any code.

A lead with no country at all still defaults to US, but now warns unless its
`state` is a recognizable US state. State goes to `PrimaryContactState` for US
and `PrimaryContactProvince` for everywhere else; the remaining address fields
are country-independent, since Oracle formats them per territory itself.

### Company website

Oracle has a Website field on the lead, and nearly every inquiry arrives from a
work address, so `company_website.py` derives one from the email domain:
`df@dcfusionllc.com` → `https://dcfusionllc.com`. It is a string split — no
model call, no lookup, no tokens. Across the cached inbox it fills 52 of the 58
leads that carry a contact address (90%).

The other 10% write from gmail/me.com and deliberately get **nothing**. Looking
up a company name from a personal address is the case most likely to put the
wrong company's URL into a CRM field, where it reads as authoritative and never
gets questioned again. A blank is honest, and a rep can fill it in.

Derivation runs at ingest (so the value is on the dashboard for review, not a
surprise at submission) and again as a backstop in `pipeline_submit.py` for
leads staged before this existed. A website the sender actually stated always
wins over the derived one.

On this tenant the Website box is the custom attribute `WebsiteBSE_c`, found by
probing the live instance on 2026-07-31 and set as `oracle.website_field`:

```json
"oracle": { "website_field": "WebsiteBSE_c" }
```

It is configured rather than hardcoded because which attribute that box maps to
is tenant configuration, and an attribute Oracle doesn't recognize comes back
`"not a valid field"` as a 400 that rejects the *whole* lead. Unset means
websites are still derived, stored and shown locally, just not sent — and each
submit run logs a line saying so.

`leads/describe` does not list this tenant's custom `*_c` fields, so the field's
writability could not be confirmed up front. Rather than gamble a lead on it,
`submit_lead` retries **once without the website** when — and only when — Oracle
returns a 400/422 naming that attribute. A 400 means Oracle created nothing, so
the retry cannot duplicate. The lead lands either way and carries a field
warning saying the website was dropped.

`oracle_field_probe.py` is the read-only tool that finds the name, kept for the
next field that needs the same treatment:

```text
py oracle_field_probe.py                      # scan recent leads
py oracle_field_probe.py --lead 300000123456  # a lead whose Website box is filled in
py oracle_field_probe.py --describe           # Oracle's metadata, incl. updatable/createable
```

## Duplicate protection

Before a lead is sent, it is checked against the local record of what has
already been submitted (`db.find_submitted_duplicate`, matching on email +
name + organization). A match means nothing is sent: the lead is marked
`duplicate`, linked to the original, and raised as a dashboard notification.

This local check is the one that holds. Oracle's own pre-submission dedup query
only returns leads visible to the API user, and a lead Oracle has not yet
assigned an owner to is invisible — including one this pipeline created
moments earlier, which is exactly the lead most likely to be duplicated.

## Customer attachments

Customers often attach a photo of a worn seal or a drawing. The dashboard
**documents** these and deliberately does not store or serve them:
`outlook_ingest.list_attachments` records `{name, size, kind}` per message and
`db.record_attachments` saves that JSON; the files are never written to disk.
A rep who needs the file opens the source email in Outlook, where it stays
behind the protections Outlook and the mail gateway already apply.

That is a security decision, not a shortcut. A lead attachment is an
unsolicited file from a stranger. Serving it from the dashboard would mean:

- **Stored XSS.** An `.svg` or `.html` rendered inline executes the sender's
  JavaScript in the dashboard's origin, where it can read the CSRF token and
  POST to `/leads/<id>/approve` — a crafted attachment approving its own lead
  into Oracle.
- **Losing Mark-of-the-Web.** Files written by this pipeline and re-served
  locally generally don't carry the zone marker that puts Office documents in
  Protected View, so the dashboard would weaken a protection the same file had
  in Outlook.
- **Becoming a malware delivery path**, with a trusted internal URL doing the
  convincing that a suspicious email attachment could not.

Filenames are still attacker-controlled text, so they are rendered as escaped
text with no link. No template uses `|safe`, which is what makes that safe.

Only `.xlsx` attachments are ever written to disk, by
`download_excel_attachments`, because trade-show lead sheets have to be parsed.
Signature logos and other embedded images are filtered out of the recorded list.

## Source email attachment

Every lead submitted through the API path also gets its source email uploaded
into the Oracle lead's Attachments section, named after the email's subject.

Ingestion exports each email to `email_messages/` as a `.msg` (via Outlook), so
what lands in Oracle is the real message — formatting, headers, thread history
and any files the sender attached — rather than just the extracted text. If the
export isn't available the pipeline falls back to the cached body `.txt`, and
failing that to a `.txt` rebuilt from the lead's own fields.

The upload targets the child-resource URL advertised in Oracle's own response
`links`, falling back to `leads/{LeadId}/child/AllLeadAttachments`. Oracle
validates `CategoryName` against a tenant lookup; the usual codes are tried in
turn, and once you know which one this tenant accepts, pin it:

```json
"attachment_category": "MISC"
```

A failed upload never fails the lead — the lead is already in Oracle by then.
It is recorded as a field warning on the lead instead.

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
