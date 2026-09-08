"""
Creating one lead by hand, from text a reviewer pasted or a screenshot they
attached.

The fourth way a lead can come into existence, alongside the three in
pipeline_ingest.py -- and the only one that does not start with an email in
the Sales inbox. It exists for the leads that arrive as something other than
mail: a phone call, a business card from a trade show, a colleague's forward,
a form a customer filled in while someone was watching over their shoulder.

Deliberately NOT part of pipeline_ingest.py. Everything in that module starts
from an Outlook message and drives COM to get it; this starts from a browser
POST and must work with no mailbox involved at all. What the two genuinely
share -- the field list, the rank/market rules, the territory map, the
website and market defaults -- is shared as functions, not by routing a
manual lead through a pipeline built for mail.

Two steps of the ingest pipeline are missing on purpose:

  * The fraud screen. Its whole job is deciding whether an unsolicited email
    is junk; a reviewer who has just typed a lead in has already answered
    that, and flagging their own entry as suspicious is noise. Nothing here
    can auto-reject, and nothing here skips human review -- the lead lands in
    Pending Review like every other and still has to be approved by hand.

  * location_enrichment / hq_lookup. Both read the open web and take the
    better part of a minute, which is too long to hold a browser POST open.
    The lead page already offers "Find the company's head office" as a button
    for exactly this case, so an unrouted manual lead has the same way out an
    unrouted ingested one does.
"""

import logging

import company_website
import db
import email_cleaner
import lead_extractor
import territory_map

logger = logging.getLogger(__name__)

# What db.insert_lead() stores in leads.source, and the key
# pipeline_submit._SOURCE_LABELS reads to name the Oracle project.
SOURCE = 'manual_entry'

# Screenshots are read once, in memory, and never written to disk. They are
# customers' own material like every attachment this pipeline handles, and
# storing them would mean a new folder of PII to serve back out of the
# dashboard's own origin -- see the SVG note in dashboard/app.py. What a
# reviewer needs to keep is the text, and that is what `transcript` is for.
ACCEPTED_IMAGE_TYPES = {
    'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp',
}

# How many screenshots one lead may carry. Defined by the extractor, which is
# what actually pays for them, and re-exported here because the upload form
# and the route that reads it talk to this module rather than to that one.
MAX_IMAGES = lead_extractor.MAX_IMAGES

# A size the browser can refuse before anything is uploaded. Bigger than any
# screenshot and smaller than the document someone means to send by email
# instead; the request body is capped again by MAX_CONTENT_LENGTH.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def build_lead_details(text, transcript):
    """
    What a rep will read in Oracle's Lead Details.

    Both halves, not one: the pasted text is what the reviewer wrote or
    forwarded, and the transcript is what the screenshot said. Someone who
    pastes "met at the show, quote wanted" and attaches a photo of the card
    means both, and dropping either loses the half the other does not carry.
    Neither is a model summary -- see lead_extractor.extract_lead_from_notes.
    """
    parts = [part.strip() for part in (text, transcript) if part and part.strip()]
    if len(parts) == 2 and parts[1] in parts[0]:
        # The reviewer pasted the same text they screenshotted. Printing it
        # twice reads as a bug, not as thoroughness.
        parts.pop()
    return email_cleaner.clean_email_body('\n\n'.join(parts)) or None


def create_lead_from_notes(text, images=None, reviewed_by=None):
    """
    Read the notes, write the lead, route it, and leave it in Pending Review.

    Returns (lead_id, extraction). Inserted straight into 'pending_review'
    rather than through 'ingested'/'extracted': those two exist so a lead
    whose ingest died mid-pipeline can still be found (see lead_stages.py),
    and there is no window to die in here -- the row is written once, after
    the model has already answered.

    An extraction failure is not an error the caller has to handle. The lead
    is still created, with whatever fields came back and the reason in its
    extraction notes, because a reviewer who has pasted a customer's details
    should never lose them to an API outage -- the review form is right there
    to fill in the rest by hand.
    """
    extraction = lead_extractor.extract_lead_from_notes(text, images=images)

    lead = dict(extraction.get('fields') or {})
    lead['lead_details'] = build_lead_details(text, extraction.get('transcript'))

    # Same two defaults ingest applies, and for the same reasons: Oracle wants
    # a Website and a Market, and both are cheap to derive locally.
    website = company_website.derive_website(lead)
    if website:
        lead['website'] = website
    if not (lead.get('market') or '').strip():
        lead['market'] = 'INDUSTRIAL'

    lead_id = db.insert_lead(lead, source=SOURCE, status='pending_review')

    tm, basis = territory_map.assign_tm(lead)
    db.set_territory_assignment(lead_id, tm, basis)

    # Written directly rather than through db.set_extraction_result(): that
    # helper also moves the lead to 'extracted', which is a mid-ingest status
    # meaning "screening has not run yet" (see lead_stages.py). Calling it
    # here quietly undid the insert above and left every hand-entered lead
    # looking like an ingest that had died halfway.
    fields = {
        'extraction_confidence': extraction.get('confidence', 0.0),
        'extraction_notes': extraction.get('notes') or 'Entered by hand from pasted notes.',
    }
    if reviewed_by:
        fields['reviewed_by'] = reviewed_by
    db.update_lead_fields(lead_id, fields)

    logger.info(
        'Lead %s created by hand (%s field(s) read, territory=%s)',
        lead_id, len(extraction.get('fields') or {}), tm or 'unassigned',
    )
    return lead_id, extraction
