"""
Best-effort cleanup of a plain-text email body.

Strips the clutter that isn't the actual message -- external-sender security
banners, tracking/embedded-image URLs, legal disclaimers, and quoted reply
history -- so what a rep reads (on the dashboard, and in the lead Description
that reaches Oracle) is what a person would actually read: the body paragraph
and signature, not the full raw dump.

Website form submissions get a second, sharper pass: they arrive as a
tab-separated label/value table whose contact half -- name, company, email,
phone, address -- is *already* parsed into its own Oracle fields, so
repeating it in the Description just buries the engineering question the rep
opened the lead to read. extract_form_submission() keeps only the answers
that aren't stored anywhere else. See the block comment above it.

Patterns below were derived from real cached email bodies in email_cache/
(a natural prose inquiry with a signature block and a company disclaimer,
and website form submissions from both vendors, full of SafeLinks/SendGrid
tracking URLs and image-checkbox references) -- see the __main__ block.
"""

import re

_CAUTION_BANNER_RE = re.compile(
    r'^CAUTION:\s*This email originated from outside.*$\n?', re.IGNORECASE | re.MULTILINE
)

# Proofpoint's external-sender banner, and the hidden preheader above it.
#
# Proofpoint injects its warning as a marked div pair at the very top of the
# HTML body:  <div id="...BannerStart"> ... <div id="...BannerEnd">.  Whatever
# sits above that is the sender's *preheader* -- the display:none block bulk
# mailers put first so the mail client has preview text for the message list.
# Outlook renders neither: not the markers, not the banner chrome, and not the
# hidden preheader.  Flattening the HTML to text exposed all three, and because
# a preheader is normally the opening of the message re-typed as one run-on
# line, the result read as the email arriving twice -- a truncated copy, then
# the real one.  That is the "double message" this strips.
#
# Verified against every cached body carrying the marker (37 of 254): the text
# above BannerStart was always either empty, zero-width-joiner padding, or a
# whitespace-normalised truncation of the body below it.  Never unique content.
#
# Anchored at \A: everything from the start of the body through the end of the
# banner goes, because all of it sits above the message a human was shown.
_PREHEADER_AND_BANNER_RE = re.compile(
    r'\A.*?'
    r'[A-Za-z0-9]{4,}BannerStart\b'
    r'.*?'
    r'[A-Za-z0-9]{4,}BannerEnd\b[ \t]*\n?',
    re.DOTALL,
)

# The From/Date/Subject block _cache_body() writes at the top of
# email_cache/*.txt. get_lead_email_thread() cleans those files whole, so the
# header has to be lifted off before the \A-anchored cut above runs -- it is
# above the banner, and would otherwise be swallowed with the preheader.
_CACHE_HEADER_RE = re.compile(
    r'\AFrom:[^\n]*\n(?:[^\n]*\n){0,4}?={10,}[ \t]*\n+'
)

# Any further banner pair -- one per quoted message down a reply chain. The
# rule above is lazy and only reaches the first, and only the first has a
# preheader above it worth cutting.
_BANNER_BLOCK_RE = re.compile(
    r'[A-Za-z0-9]{4,}BannerStart\b.*?[A-Za-z0-9]{4,}BannerEnd\b[ \t]*\n?', re.DOTALL
)

# A half-pair: the message was forwarded/truncated between the two markers, so
# there is no block to match. Drop the marker's line rather than leave a line
# of gibberish in the middle of the text.
_BANNER_MARKER_RE = re.compile(
    r'^.*[A-Za-z0-9]{4,}Banner(?:Start|End)\b.*$\n?', re.MULTILINE
)

# The banner's own wording, for gateways that tag the text without markers.
_EXTERNAL_SENDER_RE = re.compile(
    r'^[ \t]*(?:This Message Is From an External Sender'
    r'|This message came from outside your organization)[ \t.]*$\n?',
    re.IGNORECASE | re.MULTILINE,
)


def strip_security_banners(text):
    """
    Remove mail-gateway external-sender banners, and the hidden preheader a
    Proofpoint banner separates from the real body. See the patterns above.
    """
    if not text:
        return text
    header = ''
    m = _CACHE_HEADER_RE.match(text)
    if m:
        header, text = m.group(0), text[m.end():]
    text = _PREHEADER_AND_BANNER_RE.sub('', text)
    text = _BANNER_BLOCK_RE.sub('', text)
    text = _BANNER_MARKER_RE.sub('', text)
    text = _CAUTION_BANNER_RE.sub('', text)
    text = _EXTERNAL_SENDER_RE.sub('', text)
    return header + text

# Quoted reply history -- only the text above the first of these is what this
# sender actually wrote; everything below is the previous message(s) in the
# thread, already visible to them and not new information for a lead review.
# The 1-4 intervening lines (Sent/Date/To/Cc/Bcc, in any combination/order) are
# deliberately loose -- a real message with a Cc line between To: and Subject:
# slipped through an earlier, stricter version of this pattern that required
# From/Sent/To/Subject to be strictly adjacent.
_QUOTE_HEADER_RE = re.compile(
    r'\n\s*From:\s*.+\n(?:\s*(?:Sent|Date|To|Cc|Bcc):\s*.+\n){1,4}\s*Subject:\s*.+', re.IGNORECASE
)
_ON_WROTE_RE = re.compile(r'\n\s*On .{0,120}wrote:\s*\n', re.IGNORECASE)
_ORIGINAL_MESSAGE_RE = re.compile(r'\n-{3,}\s*Original Message\s*-{3,}', re.IGNORECASE)

# Automated-system footers (CAD/PLM download portals, transactional-mail
# senders, etc.) -- everything from here on is vendor boilerplate (phishing
# warnings, "how to reply" instructions, the vendor's own company/registry
# info), not part of the actual request. Same truncate-from-here approach as
# the quoted-reply patterns above.
_AUTOMATED_FOOTER_RE = re.compile(
    r'\n\s*(?:Information for [^:\n]{0,60}:\s*)?'
    r'(?:This (?:e-?mail|message) (?:was|is) sent automatically'
    r'|This is an automatically generated \S+)',
    re.IGNORECASE
)

# A line that's only underscores/dashes/equals -- a visual divider Outlook or
# a mail client inserted (before a signature, before quoted history, etc.),
# never actual content, wherever it appears.
_SEPARATOR_LINE_RE = re.compile(r'^[\s_\-=]{5,}$')

# mailto: links duplicate an email address already visible as plain text
# right next to them -- pure noise.
_MAILTO_RE = re.compile(r'\s*<mailto:[^>]+>')

# Any bracketed URL long enough to be a tracking/redirect/embedded-image link
# rather than something meant to be read (a short, real link would rarely be
# this long). 40 chars is a deliberately loose threshold, not exact.
_LONG_BRACKETED_URL_RE = re.compile(r'<https?://[^>]{40,}>')

# A line that, once trimmed, is *only* a URL -- almost always an embedded
# image or open-tracking pixel Outlook rendered as a bare link.
_STANDALONE_URL_RE = re.compile(r'^\s*<?https?://\S+>?\s*$')

# Paragraphs matching these are dropped only if also long -- a short,
# genuine sentence that happens to say "please keep this confidential"
# shouldn't be swept up with corporate legal boilerplate.
_DISCLAIMER_KEYWORDS = (
    'confidential', 'privileged', 'intended recipient', 'proprietary and confidential',
    'legal notice', 'please consider the environment',
)
_DISCLAIMER_MIN_LEN = 120


# ---------------------------------------------------------------------------
# Website form submissions
#
# The two form vendors that feed the Sales inbox (Jotform, and Gravity Forms
# via leads@bse-web.com) both render as a tab-separated label/value table. The
# contact half of that table -- name, company, email, phone, address -- is
# already parsed into its own Oracle fields, so repeating it in the lead
# Description is noise a rep has to read past to reach the actual question.
# What's below keeps only the answers that aren't captured elsewhere: the
# product/solution selections and the free-text application description.
#
# Layout differs between the two vendors (Jotform puts label and value on one
# line; Gravity puts the label on its own line and the value, tab-indented, on
# the next -- and a long free-text answer then runs on for many untabbed
# lines), so the parser below keys off tab structure rather than line shape.
# ---------------------------------------------------------------------------

# Labels whose answers Oracle already stores as structured fields, plus the
# form's own bookkeeping rows. Matched against a normalized label (lowercased,
# trailing '*'/':'/'.' and a leading "your " stripped).
_FORM_CONTACT_LABELS = {
    'name', 'first name', 'last name', 'full name', 'contact name',
    'company', 'company name', 'organization', 'organisation', 'employer',
    'email', 'email address', 'e-mail', 'e-mail address',
    'phone', 'phone number', 'telephone', 'mobile', 'mobile number', 'cell',
    'address', 'street', 'street address', 'mailing address', 'city', 'state',
    'state/province', 'state/area', 'province', 'zip', 'zip code',
    'postal code', 'country', 'title', 'job title', 'consent', 'map it',
    # Image column in the CAD portal's part table -- the cell holds a logo,
    # never text worth reading.
    'logo',
}

# Qualified variants of the fields above ("international phone (if
# applicable)", "shipping address", "work email"). Only endings specific
# enough that no product question ends the same way -- notably NOT "name",
# which would swallow "part name" / "project name".
_CONTACT_LABEL_SUFFIXES = (
    'phone', 'phone number', 'email', 'email address', 'e-mail',
    'address', 'fax', 'fax number', 'mobile', 'mobile number',
)
_CONTACT_LABEL_SUFFIX_MAX_LEN = 40

# Boilerplate that rides along inside answer cells -- the consent checkbox
# caption and the vendor's "manage your submission" footer.
_FORM_NOISE_RE = re.compile(
    r'^(?:i agree to the privacy policy\.?'
    r'|map it'
    r'|browse\.\.\.'          # caption of the file-upload button; the
                              # attached filename on the next line is real
    r'|you can edit this submission.*'
    r'|.*view all your submissions.*)$',
    re.IGNORECASE
)

# A short line ending in a colon and followed by nothing is a section heading
# whose rows were all contact fields (the CAD portal's "User information:"),
# left dangling once those were dropped. Only ever trimmed from the *end* of
# an answer -- mid-answer, a line like "Please include the following:" is
# introducing content that's still there.
_DANGLING_HEADING_MAX_LEN = 40

# Verbose prompts these forms use as field labels. Left of each pair is a
# substring match on the label; right is what a rep should see instead --
# None meaning "this is the free-text body, print the answer with no label at
# all". An unmatched label is kept verbatim if it's short enough to read as a
# field name, and dropped (answer printed bare) past _FORM_LABEL_MAX_LEN.
_FORM_LABEL_REWRITES = (
    ('application or engineering challenge', None),
    ('product type', 'Product type'),
    ('choose your solution', 'Solution'),
    ('kind of service', 'Service'),
)
_FORM_LABEL_MAX_LEN = 60

_FORM_LABEL_TRIM_RE = re.compile(r'^(?:your\s+)?(.*?)[\s*:.]*$', re.IGNORECASE | re.DOTALL)
_FORM_LABEL_PAREN_RE = re.compile(r'\s*\([^)]*\)\s*$')


def _normalize_form_label(label):
    label = _FORM_LABEL_PAREN_RE.sub('', label.strip())
    m = _FORM_LABEL_TRIM_RE.match(label)
    return (m.group(1) if m else label).strip().lower()


def _is_contact_label(label):
    """True when this field's answer is already stored as its own Oracle
    field (or is form bookkeeping), so repeating it in the Description is
    just noise."""
    norm = _normalize_form_label(label)
    if norm in _FORM_CONTACT_LABELS or norm.split('/')[0].strip() in _FORM_CONTACT_LABELS:
        return True
    return (
        len(norm) <= _CONTACT_LABEL_SUFFIX_MAX_LEN
        and norm.endswith(_CONTACT_LABEL_SUFFIXES)
    )


def _pair_is_awaiting_answer(pairs):
    """True when the last pair is a label whose answer has not arrived yet."""
    return bool(pairs) and not any(line.strip() for line in pairs[-1][1])


def _uses_indented_answers(text):
    """
    True for the layout in which every answer sits in a second table cell,
    leaving the first one empty:

        Name*<TAB>
        <TAB>Tom Kivisto<TAB>

    That empty leading cell is what tells an answer from a label, and it is
    the only thing the two layouts differ by, so it is decided once for the
    whole body rather than line by line -- a body that has it anywhere is
    parsed the old way throughout.
    """
    for raw_line in text.split('\n'):
        cells = [cell.strip() for cell in raw_line.rstrip('\r').split('\t')]
        if len(cells) > 1 and not cells[0] and any(cells[1:]):
            return True
    return False


def _parse_form_pairs(text):
    """Splits a tab-delimited form-submission body into [label, value_lines]
    pairs. Untabbed lines continue the answer above them, which is what keeps
    a multi-paragraph free-text answer intact.

    Two layouts have to survive this, and they differ only by a leading empty
    table cell. Gravity Forms used to wrap every answer in one, so an answer
    was recognisable by its empty first column:

        Name*<TAB>
        <TAB>Tom Kivisto<TAB>

    Around 2026-08-27 the same form started emitting answers flush left:

        Name*<TAB>
        test<TAB>

    which made every line look like a label in the first column. They all
    then collapsed into a single pair, _is_contact_label() matched nothing,
    and extract_form_submission() rejected the email as "not a form" -- so
    the entire raw table landed in the lead Description as one run-on
    paragraph (leads 492 and 500-503). Hence _pair_is_awaiting_answer():
    both layouts alternate label/answer regardless, so a lone first-column
    cell sitting under a label that has no answer yet *is* that answer.
    """
    indented = _uses_indented_answers(text)
    pairs = []
    for raw_line in text.split('\n'):
        # Only \r is stripped, never trailing tabs -- a label alone on its
        # line still ends in the tab that marks it as a table cell, and
        # rstrip()ing that away makes it indistinguishable from body text.
        line = raw_line.rstrip('\r')
        if '\t' not in line:
            if pairs:
                pairs[-1][1].append(line)
            continue
        cells = [cell.strip() for cell in line.split('\t')]
        filled = [cell for cell in cells if cell]
        if len(filled) >= 2:
            pairs.append([filled[0], [' '.join(filled[1:])]])
        elif len(filled) == 1:
            if cells[0] and (indented
                             or not _pair_is_awaiting_answer(pairs)
                             or _is_contact_label(filled[0])):
                # Label alone on its line; the answer follows below it.
                #
                # In the flush-left layout the two kinds of line are
                # identical in shape, so pairing goes by alternation -- which
                # a stray caption line ("Map It" under an address) would
                # otherwise shift by one for the whole rest of the table.
                # _is_contact_label() re-anchors it: a known field name is a
                # label, never somebody's answer to the prompt above it.
                pairs.append([filled[0], []])
            elif pairs:
                # An indented cell, or -- in the flush-left layout -- a cell
                # sitting directly under a label that is still missing its
                # answer. Either way, the answer to the label above.
                pairs[-1][1].append(filled[0])
        elif pairs:
            pairs[-1][1].append('')
    return pairs


def _merge_unanswered_pairs(pairs):
    """A "label" with no answer under it is usually not a label at all -- it's
    the last line of a long free-text answer that happened to end in a tab (a
    signature's street address, say). Fold those back into the answer above.
    A genuinely blank contact field is just dropped."""
    merged = []
    for label, value_lines in pairs:
        if any(line.strip() for line in value_lines):
            merged.append([label, value_lines])
        elif _is_contact_label(label):
            continue
        elif merged:
            merged[-1][1].append(label)
        else:
            merged.append([label, value_lines])
    return merged


def _is_url_only(label):
    """The header banner of these emails is a bare logo link, which parses as
    a label whose "answer" is the form's own title -- both already known from
    the email subject."""
    return bool(_STANDALONE_URL_RE.match(label.strip()))


def _rewrite_form_label(label):
    """Returns the label to print, or None to print the answer unlabeled."""
    lowered = label.lower()
    for needle, replacement in _FORM_LABEL_REWRITES:
        if needle in lowered:
            return replacement
    if len(label) > _FORM_LABEL_MAX_LEN:
        return None
    return label.rstrip(' *:.')


def extract_form_submission(text):
    """
    Reduces a website form-submission email to the answers a rep actually
    needs -- dropping the contact fields Oracle already stores separately and
    the form's own footer.

    Returns None when `text` doesn't look like one of these submissions, so
    callers can fall back to normal body cleanup. Detection deliberately
    requires several recognized contact labels: a prose email that happens to
    contain tabs must not get run through this.
    """
    if not text or '\t' not in text:
        return None

    # Tracking and mailto URLs come off before the table is parsed, not after.
    # They ride along inside cells ("Map It <SafeLinks redirect>", the consent
    # checkbox's tick.png), and a label carrying one no longer matches
    # _is_contact_label(), so the row survives into the Description as a
    # fragment of form chrome. clean_email_body() strips these anyway; doing it
    # first is what lets the label be recognised.
    text = _LONG_BRACKETED_URL_RE.sub('', _MAILTO_RE.sub('', text))

    pairs = _merge_unanswered_pairs(_parse_form_pairs(text))
    contact_rows = sum(1 for label, _ in pairs if _is_contact_label(label))
    if len(pairs) < 3 or contact_rows < 2:
        return None

    blocks = []
    for label, value_lines in pairs:
        if _is_contact_label(label) or _is_url_only(label):
            continue
        kept = [ln for ln in value_lines if not _FORM_NOISE_RE.match(ln.strip())]
        while kept and (
            not kept[-1].strip()
            or (kept[-1].strip().endswith(':')
                and len(kept[-1].strip()) <= _DANGLING_HEADING_MAX_LEN)
        ):
            kept.pop()
        value = '\n'.join(kept).strip()
        if not value:
            continue
        display_label = _rewrite_form_label(label)
        blocks.append(f'{display_label}: {value}' if display_label else value)

    return '\n\n'.join(blocks) or None


MAX_EMAIL_PARSE_LENGTH = 3000
TRUNCATION_NOTICE = (
    "\n\n[Note: This email exceeds 3,000 characters. Only the first 3,000 characters "
    "were parsed and displayed. View the original email in Outlook to read the full message.]"
)


def truncate_body_if_needed(text, max_chars=MAX_EMAIL_PARSE_LENGTH, notice=TRUNCATION_NOTICE):
    """
    If an email body or text exceeds max_chars (default 3000), truncate to max_chars
    and append a notice indicating more content exists in the original email.
    """
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    if notice in text:
        return text
    return f"{text[:max_chars].rstrip()}{notice}"


def clean_email_body(text):
    """Returns a cleaned copy of `text` for display. Safe to call on already
    short/clean text -- a no-op in that case."""
    if not text:
        return text

    # Banners come off first. The hidden preheader a Proofpoint banner sits
    # below is a run-on copy of the message's opening, and leaving it in place
    # would let it reach the form parser and the quoted-history cut as though
    # it were real body text.
    text = strip_security_banners(text)

    # Form submissions get reduced to their answers first; the cleanup below
    # then strips whatever tracking URLs and mailto links survive inside them.
    form_text = extract_form_submission(text)
    if form_text:
        text = form_text

    for pattern in (_AUTOMATED_FOOTER_RE,):
        m = pattern.search(text)
        if m:
            text = text[:m.start()]

    text = _MAILTO_RE.sub('', text)
    text = _LONG_BRACKETED_URL_RE.sub('', text)

    paragraphs = re.split(r'\n\s*\n', text)
    kept = []
    for p in paragraphs:
        lowered = p.lower()
        if len(p) > _DISCLAIMER_MIN_LEN and any(kw in lowered for kw in _DISCLAIMER_KEYWORDS):
            continue
        kept.append(p)
    text = '\n\n'.join(kept)

    lines = [
        ln for ln in text.split('\n')
        if not _STANDALONE_URL_RE.match(ln.strip()) and not _SEPARATOR_LINE_RE.match(ln)
    ]
    # Collapse runs of tabs/spaces (common in form-submission-style emails)
    # to single spaces, but preserve intentional line breaks.
    lines = [re.sub(r'[ \t]{2,}', ' ', ln).rstrip() for ln in lines]
    text = '\n'.join(lines)

    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return truncate_body_if_needed(text)


def extract_quoted_history(text, max_chars=40000):
    """
    The earlier exchange quoted inside a reply -- everything clean_email_body()
    cuts off, which is exactly what a reviewer means by "the rest of the
    thread".

    This is usually the only copy there is. The messages a "Re:" answers were
    often exchanged from a rep's own mailbox, so they are nowhere in the Sales
    mailbox and Outlook's conversation for that email contains one item --
    verified across all 79 folders of the Sales mailbox on 2026-08-10. The
    quoted block travels inside the reply regardless.

    Returns '' when the email quotes nothing (a first-contact inquiry).
    Tracking URLs and banners are stripped, but the exchange itself is left
    alone: it is being shown to a person to read, not summarised.
    """
    if not text:
        return ''

    # Same three markers clean_email_body() truncates at, earliest wins. The
    # automated-footer pattern is deliberately not among them -- vendor
    # boilerplate is not thread history.
    start = None
    for pattern in (_QUOTE_HEADER_RE, _ON_WROTE_RE, _ORIGINAL_MESSAGE_RE):
        match = pattern.search(text)
        if match and (start is None or match.start() < start):
            start = match.start()
    if start is None:
        return ''

    quoted = text[start:]
    # Each quoted message down the chain carries its own gateway banner.
    quoted = strip_security_banners(quoted)
    quoted = _MAILTO_RE.sub('', quoted)
    quoted = _LONG_BRACKETED_URL_RE.sub('', quoted)
    lines = [
        ln for ln in quoted.split('\n')
        if not _STANDALONE_URL_RE.match(ln.strip())
    ]
    lines = [re.sub(r'[ \t]{2,}', ' ', ln).rstrip() for ln in lines]
    quoted = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()

    if len(quoted) > max_chars:
        quoted = quoted[:max_chars].rstrip() + '\n\n[... rest of the thread not shown ...]'
    return quoted


def compact_for_ai(text, max_chars=MAX_EMAIL_PARSE_LENGTH):
    """
    Strip an email down to what is worth paying a model to read.

    Deliberately NOT clean_email_body(). That one is built for a human
    reader and throws away two things the extractor needs:

      * quoted reply history -- a reply's contact details frequently exist
        *only* in the quoted original ("RE: HOT RFQ" yielded its company and
        email that way, and nowhere else);
      * the contact half of a form submission, which is redundant on screen
        because it is already parsed into fields, but is exactly what the
        extractor is being asked to find.

    Measured over 92 cached emails, what is removed here -- tracking and
    redirect URLs, mailto duplicates, external-sender banners, legal
    disclaimers, divider lines and repeated whitespace -- is the large
    majority of the raw body, because these arrive as HTML flattened to text.

    Over-long bodies keep their head and tail rather than just the head: the
    new message sits at the top of a reply chain and the signature block with
    the phone number sits at the bottom, so a plain truncation loses precisely
    the contact details this exists to preserve.
    """
    if not text:
        return text

    # The duplicated preheader used to reach the extractor as though the sender
    # had written their opening twice -- wasted tokens, and a second, truncated
    # copy of the request for the model to reconcile against the real one.
    text = strip_security_banners(text)
    text = _MAILTO_RE.sub('', text)
    text = _LONG_BRACKETED_URL_RE.sub('', text)

    paragraphs = re.split(r'\n\s*\n', text)
    kept = []
    for p in paragraphs:
        lowered = p.lower()
        if len(p) > _DISCLAIMER_MIN_LEN and any(kw in lowered for kw in _DISCLAIMER_KEYWORDS):
            continue
        kept.append(p)
    text = '\n\n'.join(kept)

    lines = [
        ln for ln in text.split('\n')
        if not _STANDALONE_URL_RE.match(ln.strip()) and not _SEPARATOR_LINE_RE.match(ln)
    ]
    lines = [re.sub(r'[ \t]{2,}', ' ', ln).rstrip() for ln in lines]
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()

    if max_chars and len(text) > max_chars:
        head = int(max_chars * 0.7)
        tail = max_chars - head
        text = (
            f'{text[:head]}\n\n[... {len(text) - max_chars:,} characters of quoted '
            f'thread omitted ...]\n\n{text[-tail:]}'
        )
    return text


def condense_paragraphs(text):
    """
    Collapses line-wrapping WITHIN each paragraph into single spaces, but
    preserves paragraph breaks (blank lines) between them -- so a signature
    block stays visually separated from the message body instead of running
    together into one wall of text. Meant to be rendered with CSS
    `white-space: pre-line`, which preserves these newlines while still
    collapsing any incidental repeated spaces.
    """
    if not text:
        return text
    paragraphs = re.split(r'\n\s*\n', text)
    flattened = [' '.join(p.split()) for p in paragraphs if p.strip()]
    return '\n\n'.join(flattened)


if __name__ == '__main__':
    prose_example = (
        'CAUTION: This email originated from outside of the organization. Do not click '
        'links or open attachments unless you recognize the sender and know the content is safe.\n\n\n'
        'Hi,\n\nI am reaching out looking to get some more information on canted coil springs. '
        "I'm looking to use the springs to eat up tolerance in a sliding mechanism over the life "
        'of the product. The spring does not need to seal at all, it just needs to ensure there is '
        'zero free play and limited friction over 100k+ cycles for a plastic on plastic interface.\n'
        '_____________________________\n\n\n\nAndy Boyer\n\n'
        'Staff Mechanical Design Engineer, Product Development\n'
        'aboyer@rivian.com <mailto:aboyer@rivian.com> \n'
        '14600 Myford (Irvine, California)\n\n'
        ' <https://drive.google.com/uc?export=view&id=1qOAxhwNXJO8gccK8nx_xwpQJqnya1iy6> \n\n'
        'CONFIDENTIALITY NOTE: This electronic message (including any attachments) may contain '
        'information that is privileged, confidential, and proprietary. If you are not the intended '
        'recipient, you are hereby notified that any disclosure, copying, distribution, or use of the '
        'information contained herein (including any reliance thereon) is strictly prohibited. If you '
        'received this electronic message in error, please immediately reply to the sender that you '
        'have received this communication and destroy the material in its entirety, whether in '
        'electronic or hard copy format. Although Rivian has taken reasonable precautions to ensure no '
        'viruses are present in this email, Rivian accepts no responsibility for any loss or damage '
        'arising from the use of this email or attachments.\n'
    )
    print('=== Prose example, cleaned ===')
    print(clean_email_body(prose_example))
    print()

    form_example = (
        'CAUTION: This email originated from outside of the organization. Do not click links or '
        'open attachments unless you recognize the sender and know the content is safe.\n\n\n'
        'To get started, select one of the product types below. \t\n \t Bal Seal® \t\n'
        'Please tell us what kind of service your Bal Seal® will perform. \t\n \t Rotary \t\n'
        'Tell us about your application or engineering challenge, and we’ll get to work on it. '
        'Use the button to select and attach any relevant drawings or documents. \t\n'
        ' \t I am building a machine where I would need to pressurize through my rotary shaft with air.\n\n'
        'Shaft Ø 40mm (1.573")\nSpeed 5000 rpm\n\n'
        'Sealing pressure 80-160psi (nitrogen)\nLikely will run less, but if we could rate it up to '
        '200 psi that would be great.\n\n'
        'I could get the seals on a different part of the shaft that is 20mm if that helps. \t\n'
        'Name* \t\n \t Tom Kivisto \t\nCompany \t\n \t Forward Technology \t\nEmail \t\n'
        ' \t tkivisto@forwardtech.com <mailto:tkivisto@forwardtech.com>  \t\n'
        'Address \t\n \t MN\nUnited States\n'
        'Map It <https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Furldefense.com'
        '%2Fv3%2F__https%3A%2F%2Fu5937789.ct.sendgrid.net%2Fls%2Fclick%3Fupn%3Du001.somethingverylong>  \t\n'
        'Consent \t\n \t  <https://www.balseal.com/wp-content/plugins/gravityforms/images/tick.png> '
        'I agree to the privacy policy. \t\n'
        ' <https://u5937789.ct.sendgrid.net/wf/open?upn=u001.somethingverylongtrackingpixel>'
    )
    print('=== Form-submission example (label above value), cleaned ===')
    print(clean_email_body(form_example))
    print()

    # Same idea, other vendor: label and value share one line.
    jotform_example = (
        'CAUTION: This email originated from outside of the organization. Do not click links or '
        'open attachments unless you recognize the sender and know the content is safe.\n\n\n'
        ' <https://cdn.jotfor.ms/assets/img/logo2021/jotform-logo.png> \t\n\t\n\t\n'
        'Design Request Form\n\n\t\n\t\n'
        '\tPlease choose your solution\t Bal Contact® electrical contact\t \n'
        "Tell us about your application or engineering challenge, and we'll get to work on it "
        'right away. The more we know, the faster we can help.\t We would like to understand if '
        'it is possible to create a good radiofrequency contact in the region 1-4 GHz between to '
        'waveguides (WR284) upon compression. Do you have experience with RF conductivity? \t\n'
        'Your name\t Daniele Filippetto\t \n'
        'Company name\t DC Fusion\t \n'
        'Your email address\t df@dcfusionllc.com\t \n'
        'Your phone number\t (510) 219-1011\t \n'
        'Address\t 660 4th St., Unit #283, San Francisco, California, 94107, United States\t \n\t\n'
        '\tYou can edit this submission <https://nam12.safelinks.protection.outlook.com/?url='
        'https%3A%2F%2Fwww.jotform.com%2Fedit%2Fsomethingverylong> and view all your submissions '
        '<https://nam12.safelinks.protection.outlook.com/?url=somethingelseverylong> easily.\t \t\n'
    )
    print('=== Form-submission example (label beside value), cleaned ===')
    print(clean_email_body(jotform_example))
