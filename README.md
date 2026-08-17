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

**The green tag is the only thing that stops an email being ingested.** Every
other new message in the Sales inbox is pulled in and staged in Pending
Review — including a reply to a thread that was handled weeks ago, a reply to
a reply, an internal question, or something that is plainly not a lead.
Whether any of those needs answering or forwarding is a decision for a person,
and an email that never reaches the queue never gets that decision.

Green is matched by the category's colour, not its name — this mailbox's tag
is called `Completed`.

Two consequences worth knowing:

- **Every message keeps its own lead**, so a thread with three new replies puts
  three entries in Pending Review, each showing the whole exchange. Until
  2026-08-07 an ingest run collapsed a thread to its newest message and deleted
  the older leads; that is gone.
- Nothing is auto-rejected out of sight. A lead the fraud screen flags is
  staged as `pending_review` with `risk=reject`, visible and decidable, not
  hidden.

Duplicate protection still applies where it belongs — at submission, so the
same lead is not sent to Oracle twice — not at ingestion, where it would cost
you the decision.

The pipeline applies that tag as soon as it has ingested an email:

```json
"outlook": { "mark_ingested": true }
```

Tagging happens only after the message is recorded as processed, so an email
whose extraction failed stays untagged and is picked up again. A tag that
cannot be written is logged and ignored rather than failing the run — the cost
of a miss is one re-read, which message and lead idempotency already absorb.

Ingesting a message also stores the rest of its Outlook conversation as
read-only context, so a reply shows the exchange it belongs to. Without it a
`RE: ...` lands in the queue alone — everything it answers was tagged green
when it was first handled, so the pipeline skipped it and it is nowhere in the
database, leaving no way to judge whether the reply needs forwarding. Those
messages live in their own `thread_messages` table and are never a source of
leads, never retried and never submitted.

**Green now means "the pipeline has read this", not "this lead is in Oracle."**
That is a real change in what the tag tells the sales team, and worth knowing
before they read a green `Completed` as an entered lead. Set `mark_ingested`
to `false` to go back to tagging only after review.

`outlook.mark_reviewed` (currently `false`) is the older switch that tags an
email once every lead from it has been approved or rejected. With ingest
tagging on it is mostly redundant, since the email is already green.

## Nothing runs on a timer

Emails are read when you ask for them, with the **Click to ingest leads**
button on Pending Review. There is no scheduled ingestion: the 8:45 AM and
4:00 PM tasks were removed on 2026-08-07, and `setup_scheduler.bat` deletes
them if an older run registered them. Leads reach Oracle the same way — by a
person approving them (see below).

The button launches `pipeline_ingest.py` detached and answers immediately
rather than holding the browser open for a run that takes minutes; a second
click within two minutes is turned away instead of starting an overlapping
run. `py pipeline_ingest.py` from the project folder does the same thing.

One consequence worth knowing: **recovery only happens on a run you start.**
A message left in `new`/`error` by an interrupted run, and a lead left in
`submitting`, are retried at the start of the next ingest or submit run. With
nothing on a timer, "next run" means the next time you click.

The only scheduled task left is the dashboard itself — see "Always-on
dashboard" above.

## Nothing uploads on a timer

A lead reaches Oracle Sales Cloud only because a person approved it. There is
no scheduled submission and no automatic upload at any earlier stage: an
ingested lead sits in Pending Review, an approved one is sent, and nothing
else moves it. The 5:00 PM `LeadPipeline_Submit_OSC` task was removed on
2026-08-07.

Every path that reaches Oracle starts from a human action in the dashboard:

- **Approve All & Send to Queue** — approves the staged batch and starts the run.
- **Send to Oracle now** — starts a run for leads already approved. Needed
  because a lead whose first attempt hit a network error is put back to
  `approved` with a retry time; with no scheduled run, this is what picks it up
  rather than it waiting for the next batch approval.
- **Retry** on an uncertain submission — authorizes another attempt and starts
  the run itself.

`pipeline_submit.py` only ever acts on leads with `status='approved'`, claiming
each atomically, so overlapping runs cannot double-send. Running it by hand
(`py pipeline_submit.py`) submits approved leads and nothing else.

Dashboard forwards use the shared mailbox identity configured by
`outlook.forward_from_address` (currently `sales@balseal.com`). The signed-in
Outlook user must have Exchange **Send As** or **Send on Behalf** permission
for that address; Outlook exposes a shared mailbox as a store rather than a
separate sending account.

**Reply** answers the customer from the same shared identity. Outlook builds
the reply, so the Re: subject and the quoted original are intact and the
customer's mail client threads the answer under their own inquiry. Replying is
part of working a lead rather than disposing of one, so the lead **stays in the
review queue** — unlike forwarding and deleting, which both end it. Reply-all
is a checkbox, off by default, because inquiries routinely carry CCs.

The dialog names **who else is on the email** (To and CC, minus the Sales
mailbox itself) so that checkbox is an informed choice rather than a guess.
Ingestion records this; for messages ingested before it did, the dialog reads
it from Outlook the first time it is opened and stores the answer. "Not checked
yet" is deliberately never shown as "nobody else is here" — if Outlook cannot
be reached, the dialog says so.

Sent replies are recorded against the lead and shown wherever it appears.
Outbound mail goes to Sent Items and never comes back through ingestion, so
without that record the next reviewer has no way to see the customer has
already been written to.

A lead awaiting review also has a **Delete email** button, next to the forward
button on the pending card and on the lead's own page. It moves the original
email to the Sales mailbox's **Deleted Items** — Outlook's ordinary delete, so
it can be dragged back — and rejects every lead pulled from that email, since
none of them can be checked against a message that has left the inbox. The
confirm dialog says so up front when an email produced more than one lead.

```json
"outlook": { "allow_email_delete": true }
```

Set that to `false` to hide the button and refuse deletion outright — worth
doing on any run that should not touch the real mailbox. An unreadable or
invalid `config.json` is treated as `false` rather than defaulting to allowed.

## Reference tab

The dashboard's **Reference** tab is a quick-lookup page of whatever a reviewer
needs to check a lead against — territory maps, price lists, product sheets.

It reads as a file list: filenames in rows, and clicking one opens its preview
below (images inline, PDFs in a viewer, `.txt`/`.md`/`.csv` as text). Only one
row is open at a time, and a PDF is fetched when its row is opened rather than
on page load. Subfolders become sections.

Add documents from the page itself: paste a screenshot with Ctrl+V, drag files
onto the add box, or use the file chooser. Copying files into `reference/`
works identically. **Delete** sits inside the opened preview, so a document
cannot be removed without being looked at first.

Uploads only accept what the page can display, land in `reference/` and nowhere
else (the filename cannot choose a directory), and never overwrite — a second
`map.png` becomes `map (2).png`. `.svg` and `.html` are refused outright, being
scriptable documents served from the dashboard's own origin — export those as
PNG or PDF. The folder's own `README.md` is neither listed nor deletable.

The folder's contents are gitignored: internal sales material does not belong
in the repo (see `reference/README.md`).

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
py db.py
```

Store production credentials in Windows Credential Manager:

```powershell
py secrets_store.py OPENAI_API_KEY <key>
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

## Always-on dashboard

The dashboard runs itself: `LeadPipeline_Dashboard` is a scheduled task that
starts it **at logon** and restarts it up to three times if it dies. It runs
`pythonw.exe`, so there is no console window, and it keeps a rolling log at
`logs/dashboard.log` — under `pythonw` there is no console to print to, so a
crash would otherwise leave no trace. `setup_scheduler.bat` registers it.

It runs **as the signed-in user, in that user's session**, and that is not an
implementation detail: the reply, forward and delete buttons drive Outlook
through COM, which needs the interactive session that has the Sales mailbox
open. A Windows service under `SYSTEM` would stay up while signed off but
those buttons would fail — "always running" here means "always running while
you are logged in".

Starting a second dashboard is refused. Windows lets a process bind a port
that is already in use, so both servers look healthy while requests go to
whichever bound last — three dashboards were once running at once, serving a
mix of old and new code, which made a "restart" appear to change nothing.
`dashboard/serve.py` now checks the port before binding and exits with a log
line instead. **If a page looks stale, check for more than one dashboard
process** before assuming a code problem:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Select-Object ProcessId, CommandLine
```

## Territory / lead owner

`territory_map.assign_tm()` sets Oracle's **Owner** from the lead's geography,
following `reference/TM_Territories.pdf` ("Sales - Sales Territory", updated
09/11/2025) — US states, the California and Pennsylvania ZIP splits, Nevada's
Clark County split, Canadian provinces, and the international country list.

Until 2026-08-10 the function returned **Keaton Kaess for every lead**: an
override added while the pipeline was being evaluated, sitting above all of
that as an early `return`, which made the whole territory table dead code.

The sheet is a scan with no text layer, so it cannot be parsed. It is
transcribed in `territory_map.py`, and transcribed a second time,
independently, in `tests/test_territory_assignment.py`. **When the sheet is
reissued, update both** — a change in one place fails the tests instead of
quietly routing leads to the wrong rep.

A lead whose territory cannot be determined gets **no owner**, along with a
`basis` saying why ("No state on file", "State ... and ZIP ... disagree").
`oracle_api` omits `OwnerPartyName` in that case, so Oracle applies its own
default rather than the lead going to a wrong rep. Of the 27 open leads at the
time of the change, 18 had no location on file at all — the extractor only
fills an address when the email contains one.

### Finding a location the email never gave

Three fallbacks run in order, cheapest first, and each one only fills fields
that are still blank — anything the customer stated, or a reviewer typed,
always wins:

1. **The signature** (`location_enrichment.extract_stated_location`) — a
   recognizable US or Canadian address line, matched by pattern, no network.
2. **The company site** (`location_enrichment.research_company_location`) —
   fetches the site derived from the email domain and up to two of its own
   contact/about pages, reading `PostalAddress` JSON-LD or an address line.
3. **The head office** (`hq_lookup.py`) — a model with a single tool, an HTTP
   GET of a public page, browsing the way a person would: the company's own
   site first, then any other public page about it. This is what answers
   "Eaton, no address anywhere in the email". It matters that it can leave the
   company's site — plenty of large manufacturers, eaton.com among them, never
   answer an unattended HTTP client at all, so a lookup confined to their own
   domain would find nothing for exactly the companies most worth routing.

Step 3 costs an AI call plus several page fetches, so it runs only when the
first two have come up empty and the lead still routes nowhere. Two rules keep
an invented address out of Oracle: the answer must **cite a page the tool
actually fetched**, and it must **resolve to a territory** — otherwise the lead
keeps its blanks and reaches the reviewer as "Unassigned", which is the right
outcome for a company nobody could place. Whatever is written carries its
source URL in the assignment basis, so the reviewer can open the page it came
from.

Every fetch goes through the same guard as step 2: public hosts only (a URL
the model chose cannot reach into the local network), HTML only, capped bytes,
capped redirects, and a shared time budget.

The dashboard offers the same lookup as a **Find HQ** button beside any
"Unassigned" territory, for leads already sitting in the queue and for a
reviewer who has just corrected the company's website. Configure it under
`hq_lookup` in `config.json`:

```json
"hq_lookup": { "enabled": true, "max_fetches": 6, "budget_seconds": 60,
               "min_confidence": 0.6 }
```

Set `enabled` to `false` to switch the step off everywhere, including the
button.

For a multinational, the lookup reports the head office **in the contact's own
country** where there is one — a US engineer writing from `@eaton.com` belongs
to the US organisation, not to the group's registered address abroad.

## AI cost

One model call per email, and what it is sent is compacted first
(`email_cleaner.compact_for_ai`). Measured over the 92 cached bodies in
`email_cache/`:

```
raw body (what was sent before)   566,267 chars   ~141,600 tokens
compact_for_ai                    149,919 chars    ~37,500 tokens   -74%
```

The cost is not driven by the number of small replies — a two-line "thanks"
is ~100 tokens either way. It is driven by a handful of enormous emails:
**the three largest accounted for over half the total**, one of them 159,000
characters that compacted to 6,800. These are HTML newsletters and reply
chains flattened to text, thick with SafeLinks tracking URLs.

Compaction is deliberately not `clean_email_body()`, which is built for a
human reader and drops two things the extractor needs: quoted reply history
and a form submission's contact rows. Over the same 92 emails the display
cleaner loses a contact address in 70 of them; compaction loses one. A reply's
company and email frequently exist *only* in the quoted original — `RE: HOT
RFQ` yielded `Motion` and `Terri.Taggart@motion.com` that way and nowhere else.

Bodies over 20,000 characters keep their head and tail, not just the head: the
new message is at the top of a reply chain and the signature block with the
phone number is at the bottom.

The second AI pass (fraud plausibility) normally costs nothing extra — the
extractor returns it and `fraud_screen` reuses it. A rule-based auto-reject
skips it entirely.

### The prompt, not the email, is the bill

Once bodies are compacted the fixed extraction prompt dominates: **1,116 tokens
paid on every email against a median body of 175**. It was 1,420 before being
trimmed on 2026-08-07 — ~300 tokens off every email, about 17% of total input,
with no second model and no accuracy cost.

Trimming further is easy and was measurably worse. A shorter draft was compared
against the old prompt on 14 real cached emails, run through the live model,
with a **control arm** (old prompt against itself) to separate a real change
from the model disagreeing with itself:

| | emails differing | field diffs |
| :--- | :--- | :--- |
| control (old vs old) | 4/14 | 5 |
| trimmed (old vs new) | 5/14 | 8 |

Getting there took three fixes the first draft got wrong, each caught only by
running it: dropping "mobile/cell" from `phone_mobile` moved a number between
the two phone fields; joining the rank signals with AND instead of "most or
all" flipped real leads Hot→Warm; and demoting "First decide" to "Also decide"
made the model extract before judging, turning an internal email into a lead
with fifteen fields. A fourth showed up as *instability* rather than a wrong
answer — a three-part Spanish address split differently on every run and one
run dropped a part, fixed by saying "exactly as written; never drop part of an
address".

The lesson for anyone editing `lead_extractor.py`: prompt wording that looks
redundant is often load-bearing, and a shorter prompt raises variance on
ambiguous inputs. `tests/test_extraction_prompt.py` pins every field name,
market option, constrained value, JSON key and the injection guard, but it
cannot catch this class of change — only a live A/B with a control can.

## AI model

One pinned model, in `ai_provider.DEFAULT_OPENAI_MODEL`:

```python
DEFAULT_OPENAI_MODEL = 'gpt-5-mini-2025-08-07'
```

Used by all three AI callers — `lead_extractor` (field extraction),
`fraud_screen` (plausibility pass), `ai_column_mapper` (spreadsheet headers).
None passes a model override.

The dated snapshot is intentional: extraction quality cannot shift without a
commit. GPT-5 mini fits short, precise, high-volume extraction work and is
available through the Responses API. `OPENAI_API_KEY` is the preferred secret
name; `CHATGPT_API` remains accepted for compatibility with the original local
configuration.

To evaluate a newer model, bump the constant and diff the extracted fields over
`email_cache/` before trusting it.

## Oracle submission

Approved leads submit through the Oracle Sales Cloud REST API. The endpoint is
derived from `oracle.base_url`:

```text
https://cagc.fa.us1.oraclecloud.com/crmRestApi/resources/11.13.18.05/leads
```

Oracle's `Description` / Lead Details field accepts at most 2,000 characters.
For a longer inquiry, `description_compactor.py` asks OpenAI to remove greetings,
signatures, repeated contact data, boilerplate, and redundancy while retaining
the request, quantities, part numbers, specifications, delivery details, and
deadlines. A deterministic bounded fallback is used if OpenAI is unavailable,
and the complete source email remains attached to the Oracle lead.

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

## The lead lifecycle

A lead is always in exactly one of three places, and there is always a way
onward and a way back:

```
   Pending Review  --approve-->  Input Queue  --sent to Oracle-->  History
        ^                              |                              |
        |                              |  a missing field is caught    |
        |<-----------------------------+  before Oracle is contacted   |
        |                                                              |
        +<------------- "Return to Review" ----------------------------+
```

`lead_stages.py` is the one place that says which status belongs to which
page. Every page and every guard reads it, and `tests/test_lead_stages.py`
renders all three pages with one lead in each of the eleven statuses and fails
unless each appears on exactly one of them.

That test exists because two statuses used to appear on none. A lead spends a
few seconds in `ingested` and then `extracted` between being written down and
being fraud-screened; an ingest run that died in that window left the lead in
the database, with its email, visible nowhere and recoverable by nothing. Those
two now list under Pending Review, which is what such a lead still needs, and
the ingest run's closing write is conditional (`db.finish_ingest`) so a lead
approved while screening was still running does not get quietly pulled back.

Two other dead ends closed with it: a lead the dedup guard marked `duplicate`
had no way back at all (a wrong match — two genuine inquiries months apart —
was unreachable forever), and a lead left `submitting` by a submit run that was
killed stayed claimed until some later run happened to sweep it up, which for a
pipeline with nothing on a timer could be never. `duplicate` now returns to
review like `rejected` and `failed`; a stuck `submitting` lead shows how long
it has been going and offers "Release & check Oracle", which moves it to
`submission_unknown` — the honest status, since the dead run may have reached
Oracle first.

`approved` and `submitting` are the Input Queue's, not History's. They were
listed on both, so a lead in flight appeared twice and "where is it?" had two
answers.

**A lead that could not be sent leaves History.** When a submission is blocked
by a missing field, nothing reached Oracle, so the lead goes back to Pending
Review with the reason attached — it is not a historical outcome, it is work to
do. Look for it under Pending Review, on the notification bell, and on its own
card, which keeps saying what to fix until it is fixed.

## State and recovery

- Message and lead ingestion use stable source keys, so retries update the same
  work item instead of creating another lead.
- `new` and `error` messages are retried from the cached body on the next run.
- Approved leads are atomically claimed as `submitting`.
- A crash after clicking Oracle Save becomes `submission_unknown`. Reconcile
  that record in Oracle before restoring it; automatic retry could duplicate it.
- Claims left in `submitting` for more than 90 minutes are moved to
  `submission_unknown` at the next submission run — and, since a next run may
  never come, the Input Queue offers the same release by hand after 15 minutes.

The `/status` dashboard page shows intake/review counts plus the Oracle sales
funnel: qualified leads, converted leads, opportunity stage/status, estimated
revenue grouped by currency, won-opportunity value, and linked sales-order net
value when Oracle Sales/CPQ exposes it. Search by person,
company, email, Oracle lead number, or opportunity number, then open a lead to
see its lifetime. The outcome table is limited to leads this pipeline actually
created in Oracle (pre-existing Oracle dedup matches are excluded) and can be
filtered to All submitted, Qualified, or Converted.

Oracle outcome data is refreshed by the read-only `oracle_sync.py` command. It
uses the Oracle lead ID already saved during submission and calls the lead,
`LeadOpportunity`, and (when available) `salesOrderLines` REST resources; it
never POSTs or PATCHes Oracle data.

```powershell
py oracle_sync.py                         # refresh all submitted leads now
py oracle_sync.py --lead-id 123           # refresh one local lead
.\setup_oracle_sync_task.ps1              # install daily 6:00 AM refresh
.\setup_oracle_sync_task.ps1 -At '04:30'  # choose a different local time
```

The opportunity amount in this dashboard is Oracle's estimated sales revenue.
A linked sales-order net amount is a stronger payoff proxy, but neither is
invoiced or recognized revenue. Exact accounting revenue requires the ERP
transaction that your Oracle deployment treats as its financial source of
truth. Logs are written under `logs/`.
