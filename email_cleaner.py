"""
Best-effort cleanup of a plain-text email body for DISPLAY only.

Strips the clutter that isn't the actual message -- external-sender security
banners, tracking/embedded-image URLs, legal disclaimers, and quoted reply
history -- so the dashboard's "view original email" shows what a person
would actually read (body paragraph + signature), not the full raw dump.

This never touches the underlying stored lead_details, which stays verbatim
on purpose (see lead_extractor.py's docstring) -- it's what a sales rep sees
and what gets submitted to Oracle, and shouldn't be lossy. This module only
changes what the dashboard *renders*.

Patterns below were derived from real cached email bodies in email_cache/
(a natural prose inquiry with a signature block and a company disclaimer,
and a website contact-form submission full of SafeLinks/SendGrid tracking
URLs and an image-checkbox reference) -- see the __main__ block for both.
"""

import re

_CAUTION_BANNER_RE = re.compile(
    r'^CAUTION:\s*This email originated from outside.*$\n?', re.IGNORECASE | re.MULTILINE
)

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


def clean_email_body(text):
    """Returns a cleaned copy of `text` for display. Safe to call on already
    short/clean text -- a no-op in that case."""
    if not text:
        return text

    for pattern in (_QUOTE_HEADER_RE, _ON_WROTE_RE, _ORIGINAL_MESSAGE_RE, _AUTOMATED_FOOTER_RE):
        m = pattern.search(text)
        if m:
            text = text[:m.start()]

    text = _CAUTION_BANNER_RE.sub('', text)
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

    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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
    print('=== Form-submission example, cleaned ===')
    print(clean_email_body(form_example))
