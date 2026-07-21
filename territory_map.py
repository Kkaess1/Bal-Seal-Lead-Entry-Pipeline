"""
Sales territory -> Territory Manager (TM) lookup.

Used to set Oracle's "Owner" field so a lead routes to the correct rep by
company geography instead of defaulting to whoever runs the pipeline.

Source: Bal Seal's internal "Sales - Sales Territory" page (inside.balseal.com/Sales/
SitePages/Sales Territory.aspx), last updated 09/11/2025, supplied by Keaton as a PDF
on 2026-07-20. See territory_reference_raw.txt for a plain-text dump of the same page.
This superseded an earlier copy/paste of the same table that lost its row/column
structure -- notably, that earlier attempt had Israel and Austria under Serdar Eren;
the real table puts both under Davide Amato. Trust this file, not memory of the paste.
"""

# --- International: country name (lowercased) -> TM ---
# A value of None means the source list explicitly marked that country "NA"
# (no TM assigned) -- still a real answer, not a gap, so it's kept distinct from
# a country that's simply missing from this dict entirely.
COUNTRY_TO_TM = {
    # EMEA - Davide Amato (Southern Europe)
    'italy': 'Davide Amato', 'portugal': 'Davide Amato', 'spain': 'Davide Amato',
    'israel': 'Davide Amato', 'slovenia': 'Davide Amato', 'croatia': 'Davide Amato',
    'bosnia': 'Davide Amato', 'montenegro': 'Davide Amato', 'albania': 'Davide Amato',
    'greece': 'Davide Amato', 'cyprus': 'Davide Amato', 'austria': 'Davide Amato',
    'morocco': 'Davide Amato', 'algeria': 'Davide Amato', 'tunisia': 'Davide Amato',
    'sardinia': 'Davide Amato', 'sardegna': 'Davide Amato',

    # EMEA - Marcus James (UK, Ireland, Scandinavia)
    'england': 'Marcus James', 'united kingdom': 'Marcus James', 'uk': 'Marcus James',
    'great britain': 'Marcus James', 'scotland': 'Marcus James', 'wales': 'Marcus James',
    'northern ireland': 'Marcus James', 'ireland': 'Marcus James', 'denmark': 'Marcus James',
    'finland': 'Marcus James', 'norway': 'Marcus James', 'sweden': 'Marcus James',

    # EMEA - Ralph Delcourt (France, Benelux, Eastern Europe)
    'france': 'Ralph Delcourt', 'belgium': 'Ralph Delcourt', 'netherlands': 'Ralph Delcourt',
    'luxembourg': 'Ralph Delcourt', 'czech republic': 'Ralph Delcourt', 'czechia': 'Ralph Delcourt',
    'hungary': 'Ralph Delcourt', 'poland': 'Ralph Delcourt', 'romania': 'Ralph Delcourt',
    'slovakia': 'Ralph Delcourt', 'moldova': 'Ralph Delcourt', 'bulgaria': 'Ralph Delcourt',

    # EMEA - Serdar Eren (Germany, Switzerland, Turkiye + Middle East + Africa)
    'germany': 'Serdar Eren', 'switzerland': 'Serdar Eren', 'turkiye': 'Serdar Eren',
    'turkey': 'Serdar Eren',
    'libya': 'Serdar Eren', 'western sahara': 'Serdar Eren', 'mauritania': 'Serdar Eren',
    'mali': 'Serdar Eren', 'senegal': 'Serdar Eren', 'gambia': 'Serdar Eren',
    'guinea bissau': 'Serdar Eren', 'sierra leone': 'Serdar Eren', 'liberia': 'Serdar Eren',
    "cote d'ivoire": 'Serdar Eren', 'ivory coast': 'Serdar Eren', 'burkina faso': 'Serdar Eren',
    'ghana': 'Serdar Eren', 'togo': 'Serdar Eren', 'benin': 'Serdar Eren', 'niger': 'Serdar Eren',
    'nigeria': 'Serdar Eren', 'cameroon': 'Serdar Eren', 'chad': 'Serdar Eren', 'egypt': 'Serdar Eren',
    'sudan': 'Serdar Eren', 'eritrea': 'Serdar Eren', 'ethiopia': 'Serdar Eren',
    'south sudan': 'Serdar Eren', 'central african republic': 'Serdar Eren',
    'democratic republic of the congo': 'Serdar Eren', 'congo': 'Serdar Eren',
    'uganda': 'Serdar Eren', 'kenya': 'Serdar Eren', 'somalia': 'Serdar Eren',
    'tanzania': 'Serdar Eren', 'angola': 'Serdar Eren', 'zambia': 'Serdar Eren',
    'namibia': 'Serdar Eren', 'botswana': 'Serdar Eren', 'zimbabwe': 'Serdar Eren',
    'mozambique': 'Serdar Eren', 'south africa': 'Serdar Eren', 'madagascar': 'Serdar Eren',
    'saudi arabia': 'Serdar Eren', 'yemen': 'Serdar Eren', 'oman': 'Serdar Eren',
    'qatar': 'Serdar Eren',

    # Explicitly "NA" (no TM) in the source list -- flag for manual review, don't guess
    'venezuela': None, 'iran': None, 'iraq': None, 'syria': None, 'afghanistan': None,

    # Asia - Alan Lam
    'korea': 'Alan Lam', 'south korea': 'Alan Lam',

    # Asia - Nozomi Kato
    'japan': 'Nozomi Kato',

    # Asia - Sam Tam (China, Taiwan, India, Southeast Asia, Oceania)
    'china': 'Sam Tam', 'hong kong': 'Sam Tam', 'macau': 'Sam Tam', 'taiwan': 'Sam Tam',
    'india': 'Sam Tam', 'australia': 'Sam Tam', 'new zealand': 'Sam Tam',
    'bangladesh': 'Sam Tam', 'cambodia': 'Sam Tam', 'indonesia': 'Sam Tam', 'laos': 'Sam Tam',
    'malaysia': 'Sam Tam', 'mongolia': 'Sam Tam', 'myanmar': 'Sam Tam', 'nepal': 'Sam Tam',
    'pakistan': 'Sam Tam', 'philippines': 'Sam Tam', 'singapore': 'Sam Tam',
    'sri lanka': 'Sam Tam', 'thailand': 'Sam Tam', 'vietnam': 'Sam Tam',

    # Americas (non-US/Canada) - Lisa Wolf
    'mexico': 'Lisa Wolf', 'guatemala': 'Lisa Wolf', 'nicaragua': 'Lisa Wolf',
    'costa rica': 'Lisa Wolf', 'cuba': 'Lisa Wolf', 'dominican republic': 'Lisa Wolf',
    'puerto rico': 'Lisa Wolf', 'colombia': 'Lisa Wolf', 'ecuador': 'Lisa Wolf',
    'peru': 'Lisa Wolf', 'guyana': 'Lisa Wolf', 'suriname': 'Lisa Wolf',
    'french guiana': 'Lisa Wolf', 'brazil': 'Lisa Wolf', 'bolivia': 'Lisa Wolf',
    'chile': 'Lisa Wolf', 'paraguay': 'Lisa Wolf', 'argentina': 'Lisa Wolf',
    'uruguay': 'Lisa Wolf',
}

# --- Domestic: US states ---
# California, Pennsylvania, and Nevada are each split within the source table itself
# (by ZIP or county) rather than being one state -> TM entry -- handled as special
# cases in assign_tm() below, not in this dict.
US_STATE_TO_TM = {
    # Daniel Smoliga (NORTHEAST)
    'connecticut': 'Daniel Smoliga', 'delaware': 'Daniel Smoliga',
    'massachusetts': 'Daniel Smoliga', 'maine': 'Daniel Smoliga',
    'new hampshire': 'Daniel Smoliga', 'new jersey': 'Daniel Smoliga',
    'new york': 'Daniel Smoliga', 'rhode island': 'Daniel Smoliga',
    'vermont': 'Daniel Smoliga',

    # Brian Carter (WEST)
    'alaska': 'Brian Carter', 'colorado': 'Brian Carter', 'hawaii': 'Brian Carter',
    'idaho': 'Brian Carter', 'montana': 'Brian Carter', 'oregon': 'Brian Carter',
    'utah': 'Brian Carter', 'washington': 'Brian Carter', 'wyoming': 'Brian Carter',

    # Jeff Frederick (SOUTHERN CALIFORNIA)
    'arizona': 'Jeff Frederick', 'new mexico': 'Jeff Frederick',

    # Mark Copeland (SOUTH)
    'alabama': 'Mark Copeland', 'arkansas': 'Mark Copeland', 'florida': 'Mark Copeland',
    'georgia': 'Mark Copeland', 'louisiana': 'Mark Copeland', 'maryland': 'Mark Copeland',
    'mississippi': 'Mark Copeland', 'north carolina': 'Mark Copeland',
    'oklahoma': 'Mark Copeland', 'south carolina': 'Mark Copeland',
    'tennessee': 'Mark Copeland', 'texas': 'Mark Copeland', 'virginia': 'Mark Copeland',

    # Wade Kahout (MIDWEST)
    'iowa': 'Wade Kahout', 'minnesota': 'Wade Kahout', 'north dakota': 'Wade Kahout',
    'nebraska': 'Wade Kahout', 'south dakota': 'Wade Kahout', 'wisconsin': 'Wade Kahout',

    # Bill Rost (MIDEAST)
    'illinois': 'Bill Rost', 'indiana': 'Bill Rost', 'kansas': 'Bill Rost',
    'kentucky': 'Bill Rost', 'michigan': 'Bill Rost', 'missouri': 'Bill Rost',
    'ohio': 'Bill Rost', 'west virginia': 'Bill Rost',
}

# --- Domestic: Canada provinces/territories ---
# New Brunswick and Prince Edward Island aren't in the source table at all -- left
# out rather than guessed. Keyed by both postal abbreviation and full name.
CANADA_PROVINCE_TO_TM = {
    # Daniel Smoliga (NORTHEAST)
    'nl': 'Daniel Smoliga', 'newfoundland and labrador': 'Daniel Smoliga',
    'ns': 'Daniel Smoliga', 'nova scotia': 'Daniel Smoliga',
    'qc': 'Daniel Smoliga', 'quebec': 'Daniel Smoliga',

    # Brian Carter (WEST)
    'ab': 'Brian Carter', 'alberta': 'Brian Carter',
    'bc': 'Brian Carter', 'british columbia': 'Brian Carter',
    'nwt': 'Brian Carter', 'nt': 'Brian Carter', 'northwest territories': 'Brian Carter',
    'sk': 'Brian Carter', 'saskatchewan': 'Brian Carter',
    'yt': 'Brian Carter', 'yukon': 'Brian Carter',

    # Wade Kahout (MIDWEST)
    'mb': 'Wade Kahout', 'manitoba': 'Wade Kahout',
    'nu': 'Wade Kahout', 'nunavut': 'Wade Kahout',

    # Bill Rost (MIDEAST)
    'on': 'Bill Rost', 'ontario': 'Bill Rost',
}

US_STATE_ABBR = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California',
    'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware', 'FL': 'Florida', 'GA': 'Georgia',
    'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa',
    'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire',
    'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York', 'NC': 'North Carolina',
    'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania',
    'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee',
    'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}

# ZIP-code / county splits called out explicitly in the source table.
_CA_SOUTHERN_MAX_ZIP = 93599   # <= this -> Jeff Frederick (Southern CA), else -> Brian Carter (West)
_PA_ROST_MAX_ZIP = 16999       # <= this -> Bill Rost, else -> Daniel Smoliga

# Las Vegas-metro ZIP3 prefixes -- a heuristic for Clark County, NV, not an
# authoritative county lookup (the source table only names the county, not ZIPs).
# Always surfaced in `basis` so a human can double-check edge cases; Nevada leads
# without a ZIP fall back to manual review rather than guessing blind.
_CLARK_COUNTY_NV_ZIP3 = {'890', '891'}

_US_COUNTRY_NAMES = {'usa', 'us', 'united states', 'u.s.a.', 'united states of america'}
_CANADA_COUNTRY_NAMES = {'canada'}


def _zip5(raw_zip):
    digits = ''.join(ch for ch in (raw_zip or '') if ch.isdigit())
    return digits[:5] if len(digits) >= 5 else None


def _normalize_us_state(state):
    s = state.strip()
    if len(s) == 2 and s.upper() in US_STATE_ABBR:
        return US_STATE_ABBR[s.upper()].lower()
    return s.lower()


def assign_tm(lead):
    """
    Return (tm_name, basis) for a lead based on its company address.

    tm_name is None when there's no confident match -- callers should leave
    Oracle's Owner field at its default and flag the lead for manual assignment
    rather than guessing. `basis` always explains why, for the dashboard.
    """
    country = (lead.get('country') or '').strip().lower()
    state_raw = (lead.get('state') or '').strip()
    zip5 = _zip5(lead.get('zip'))

    if not country or country in _US_COUNTRY_NAMES:
        if not state_raw:
            return None, 'No state on file -- cannot determine US territory'
        state = _normalize_us_state(state_raw)

        if state == 'california':
            if not zip5:
                return None, 'California -- need a ZIP code to determine North/South split'
            if int(zip5) <= _CA_SOUTHERN_MAX_ZIP:
                return 'Jeff Frederick', f'California, ZIP {zip5} (<= {_CA_SOUTHERN_MAX_ZIP})'
            return 'Brian Carter', f'California, ZIP {zip5} (> {_CA_SOUTHERN_MAX_ZIP})'

        if state == 'pennsylvania':
            if not zip5:
                return None, 'Pennsylvania -- need a ZIP code to determine territory split'
            if int(zip5) <= _PA_ROST_MAX_ZIP:
                return 'Bill Rost', f'Pennsylvania, ZIP {zip5} (<= {_PA_ROST_MAX_ZIP})'
            return 'Daniel Smoliga', f'Pennsylvania, ZIP {zip5} (> {_PA_ROST_MAX_ZIP})'

        if state == 'nevada':
            if not zip5:
                return None, 'Nevada -- need a ZIP code to determine the Clark County split'
            if zip5[:3] in _CLARK_COUNTY_NV_ZIP3:
                return 'Jeff Frederick', f'Nevada, ZIP {zip5} -- inferred Clark County (ZIP-prefix heuristic, verify)'
            return 'Brian Carter', f'Nevada, ZIP {zip5} -- inferred not Clark County (ZIP-prefix heuristic, verify)'

        if state in US_STATE_TO_TM:
            return US_STATE_TO_TM[state], f'US state: {state_raw}'
        return None, f'US state "{state_raw}" not found in territory list'

    if country in _CANADA_COUNTRY_NAMES:
        if not state_raw:
            return None, 'No province on file -- cannot determine Canada territory'
        province = state_raw.strip().lower()
        if province in CANADA_PROVINCE_TO_TM:
            return CANADA_PROVINCE_TO_TM[province], f'Canada province: {state_raw}'
        return None, f'Canada province "{state_raw}" not found in territory list'

    if country in COUNTRY_TO_TM:
        tm = COUNTRY_TO_TM[country]
        if tm:
            return tm, f'country: {country}'
        return None, f'"{country}" is explicitly unassigned (NA) in the source territory list'

    return None, f'country "{country}" not found in territory list'


if __name__ == '__main__':
    samples = [
        {'country': 'USA', 'state': 'TX'},
        {'country': 'USA', 'state': 'OH'},
        {'country': 'USA', 'state': 'CA', 'zip': '90210'},   # LA -> Southern CA
        {'country': 'USA', 'state': 'CA', 'zip': '94105'},   # SF -> West
        {'country': 'USA', 'state': 'PA', 'zip': '19102'},   # Philadelphia -> Bill Rost
        {'country': 'USA', 'state': 'PA', 'zip': '15222'},   # Pittsburgh -> Daniel Smoliga
        {'country': 'USA', 'state': 'NV', 'zip': '89101'},   # Las Vegas -> Clark County
        {'country': 'USA', 'state': 'NV', 'zip': '89501'},   # Reno -> not Clark County
        {'country': 'USA', 'state': 'NV'},                    # no zip -> manual review
        {'country': 'Canada', 'state': 'ON'},
        {'country': 'Canada', 'state': 'Quebec'},
        {'country': 'Germany'},
        {'country': 'Israel'},
        {'country': 'Austria'},
        {'country': 'Japan'},
        {'country': 'Brazil'},
        {'country': 'Venezuela'},
        {'country': 'Atlantis'},
    ]
    for s in samples:
        print(s, '->', assign_tm(s))
