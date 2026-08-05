"""
Derives the company website for a lead, for Oracle's Website field.

Almost nobody types their URL into a web form -- but nearly everybody writes
from a company address, and for a business inquiry the email domain *is* the
company's website. df@dcfusionllc.com means dcfusionllc.com. Across the cached
Sales inbox, 53 of the 58 leads carrying a contact address had a corporate
domain resolving to that company's real site. No model call, no lookup, no
tokens -- it's a string split.

The remaining ~9% write from gmail, and they deliberately get nothing. Looking
up "SLB" from a personal address is exactly the case most likely to put the
wrong company's URL into a CRM field, where it reads as authoritative and
nobody questions it again. A blank is honest, and a rep can fill it in while
reviewing.

Nothing here talks to the network. A domain is reported as a website without
checking that it serves one; that's the same assumption the rep would make
reading the address, and a dead link is cheaper than a wrong company.
"""

import re

from fraud_screen import DISPOSABLE_EMAIL_DOMAINS, PERSONAL_EMAIL_DOMAINS

# Consumer mail providers beyond the ones fraud_screen scores on. Kept here
# rather than added to that module's set on purpose: fraud_screen's list drives
# a visible "personal email domain" flag on the lead, and widening it would
# change screening output as a side effect of a website change. This list only
# ever decides whether a domain is worth calling a company website.
_EXTRA_CONSUMER_DOMAINS = {
    'me.com', 'mac.com', 'googlemail.com', 'ymail.com', 'rocketmail.com',
    'comcast.net', 'sbcglobal.net', 'verizon.net', 'att.net', 'bellsouth.net',
    'cox.net', 'earthlink.net', 'charter.net', 'roadrunner.com', 'aim.com',
    'btinternet.com', 'sky.com', 'virginmedia.com', 'web.de', 't-online.de',
    'freenet.de', 'orange.fr', 'wanadoo.fr', 'free.fr', 'libero.it',
    'qq.com', '163.com', '126.com', 'sina.com', 'naver.com', 'daum.net',
    'rediffmail.com', 'zoho.com', 'fastmail.com', 'hushmail.com', 'gmx.de',
    'gmx.net', 'yandex.ru', 'mail.ru', 'inbox.lv', 'seznam.cz',
}

# Senders that carry leads without being the lead: our own domains, the two
# form vendors, and the CAD portal that relays part enquiries.
_NON_CUSTOMER_DOMAINS = {
    'balseal.com', 'bse-web.com', 'jotform.com', 'cadenas.de',
    'partcommunity.com', 'sendgrid.net', 'example.com',
}

_SCHEME_RE = re.compile(r'^[a-z][a-z0-9+.\-]*://', re.IGNORECASE)

# A hostname with at least one dot and an alphabetic TLD -- which also rejects
# bare IPs, so a lead that arrived from user@192.168.1.10 yields nothing.
_HOSTNAME_RE = re.compile(
    r'^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$', re.IGNORECASE
)

_STRIP_CHARS = ' \t<>"\'()[],;:.'


def is_company_domain(domain):
    """Whether a mail domain stands for a company rather than a person."""
    domain = (domain or '').strip().lower().strip('.')
    if not domain or not _HOSTNAME_RE.match(domain):
        return False
    return not (
        domain in PERSONAL_EMAIL_DOMAINS
        or domain in DISPOSABLE_EMAIL_DOMAINS
        or domain in _EXTRA_CONSUMER_DOMAINS
        or domain in _NON_CUSTOMER_DOMAINS
    )


def normalize_url(value):
    """
    Tidies a website someone actually gave us into a bare scheme+host URL, or
    returns None if it isn't one. Path and query are dropped: the CRM field
    wants the company's site, not the product page they were looking at.

    An explicit http:// is preserved -- some industrial suppliers still don't
    redirect -- but a scheme-less "balseal.com" becomes https://.
    """
    if not value:
        return None
    value = str(value).strip().strip(_STRIP_CHARS)
    if not value or '@' in value:  # an email address is not a website
        return None

    scheme = 'https'
    m = _SCHEME_RE.match(value)
    if m:
        scheme = m.group(0)[:-3].lower()
        if scheme not in ('http', 'https'):
            return None
        value = value[m.end():]

    host = value.split('/')[0].split('?')[0].split('#')[0]
    host = host.split('@')[-1].split(':')[0].strip().lower().strip('.')
    if not _HOSTNAME_RE.match(host):
        return None
    return f'{scheme}://{host}'


def website_from_email(email):
    """The company site implied by a work email address, or None for a
    personal/disposable address or one of our own relays."""
    domain = (email or '').strip().rsplit('@', 1)[-1].strip().lower().strip('.')
    if '@' not in (email or '') or not is_company_domain(domain):
        return None
    return f'https://{domain}'


def derive_website(lead):
    """
    Best website for a lead: whatever they told us, else what their email
    address implies. Returns None when neither yields anything usable, and
    never overwrites a stored value with a derived one -- see caller.
    """
    stated = normalize_url(lead.get('website'))
    if stated and is_company_domain(stated.split('://', 1)[-1]):
        return stated
    return website_from_email(lead.get('email'))


if __name__ == '__main__':
    samples = [
        {'email': 'df@dcfusionllc.com'},
        {'email': 'tdoskamp@gmail.com'},
        {'email': 'charles.hamel@cta-brp-udes.com'},
        {'email': 'someone@engineeringmagazine.co.uk'},
        {'email': 'buyer@gmail.com', 'website': 'www.acme-seals.com/products?ref=x'},
        {'email': 'rep@balseal.com'},
        {'website': 'http://legacy-supplier.com'},
    ]
    for sample in samples:
        print(f'{str(sample):<70} -> {derive_website(sample)}')
