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

The same sheet now sits in reference/TM_Territories.pdf, on the dashboard's
Reference tab. It is a scan with no text layer, so it cannot be parsed -- the
tables below are a transcription, and tests/test_territory_assignment.py
transcribes it a second time, independently, and asserts the two agree. When
the sheet is reissued, update both: a change made in only one place fails the
tests rather than quietly routing leads to the wrong rep.

KNOWN DIVERGENCE FROM THAT PDF -- deliberate, do not "correct" it back:
  * 2026-08-10: Lisa Wolf left the company. The Americas (non-US/Canada) list
    below is now John Pan. The PDF still reads Lisa Wolf, and being a scan it
    cannot be edited here, so the code is deliberately ahead of it until the
    sheet is reissued.
  * 2026-08-14: Daniel Smoliga was replaced by John Pan, who held the
    NORTHEAST territory in addition to the Americas list.
  * 2026-08-18: John Pan's NORTHEAST territories (US states CT, DE, MA, ME, NH,
    NJ, NY, RI, VT, PA Zip 17000 & above, and Canadian provinces NL, NS, QC)
    were transferred to Bill Rost. John Pan continues to hold the Americas list.
  * 2026-09-28: Jeff Frederick left the company. His SOUTHERN CALIFORNIA
    territory (CA Zip 93599 & below, Arizona, New Mexico, Nevada Clark County)
    passed to John Pan, who also keeps the Americas list.
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

    # Americas (non-US/Canada) - John Pan
    'mexico': 'John Pan', 'guatemala': 'John Pan', 'nicaragua': 'John Pan',
    'costa rica': 'John Pan', 'cuba': 'John Pan', 'dominican republic': 'John Pan',
    'puerto rico': 'John Pan', 'colombia': 'John Pan', 'ecuador': 'John Pan',
    'peru': 'John Pan', 'guyana': 'John Pan', 'suriname': 'John Pan',
    'french guiana': 'John Pan', 'brazil': 'John Pan', 'bolivia': 'John Pan',
    'chile': 'John Pan', 'paraguay': 'John Pan', 'argentina': 'John Pan',
    'uruguay': 'John Pan',
}

# --- Domestic: US states ---
# California, Pennsylvania, and Nevada are each split within the source table itself
# (by ZIP or county) rather than being one state -> TM entry -- handled as special
# cases in assign_tm() below, not in this dict.
US_STATE_TO_TM = {
    # Bill Rost (NORTHEAST) -- transferred from John Pan on 2026-08-18
    'connecticut': 'Bill Rost', 'delaware': 'Bill Rost',
    'massachusetts': 'Bill Rost', 'maine': 'Bill Rost',
    'new hampshire': 'Bill Rost', 'new jersey': 'Bill Rost',
    'new york': 'Bill Rost', 'rhode island': 'Bill Rost',
    'vermont': 'Bill Rost', 'pennsylvania': 'Bill Rost',

    # Keaton Kaess (WEST)
    'alaska': 'Keaton Kaess', 'colorado': 'Keaton Kaess', 'hawaii': 'Keaton Kaess',
    'idaho': 'Keaton Kaess', 'montana': 'Keaton Kaess', 'oregon': 'Keaton Kaess',
    'utah': 'Keaton Kaess', 'washington': 'Keaton Kaess', 'wyoming': 'Keaton Kaess',

    # John Pan (SOUTHERN CALIFORNIA)
    'arizona': 'John Pan', 'new mexico': 'John Pan',

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
    # Bill Rost (NORTHEAST) -- transferred from John Pan on 2026-08-18
    'nl': 'Bill Rost', 'newfoundland and labrador': 'Bill Rost',
    'ns': 'Bill Rost', 'nova scotia': 'Bill Rost',
    'qc': 'Bill Rost', 'quebec': 'Bill Rost',

    # Keaton Kaess (WEST)
    'ab': 'Keaton Kaess', 'alberta': 'Keaton Kaess',
    'bc': 'Keaton Kaess', 'british columbia': 'Keaton Kaess',
    'nwt': 'Keaton Kaess', 'nt': 'Keaton Kaess', 'northwest territories': 'Keaton Kaess',
    'sk': 'Keaton Kaess', 'saskatchewan': 'Keaton Kaess',
    'yt': 'Keaton Kaess', 'yukon': 'Keaton Kaess',

    # Wade Kahout (MIDWEST)
    'mb': 'Wade Kahout', 'manitoba': 'Wade Kahout',
    'nu': 'Wade Kahout', 'nunavut': 'Wade Kahout',

    # Bill Rost (MIDEAST)
    'on': 'Bill Rost', 'ontario': 'Bill Rost',
}

# Canonical choices for any UI that allows a manual Territory Manager override.
# Derive this from the routing tables so the dropdown cannot drift away from the
# names assign_tm() actually produces when territories change hands.
TERRITORY_MANAGERS = tuple(sorted({
    tm
    for mapping in (COUNTRY_TO_TM, US_STATE_TO_TM, CANADA_PROVINCE_TO_TM)
    for tm in mapping.values()
    if tm
}))

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
_CA_SOUTHERN_MAX_ZIP = 93599   # <= this -> John Pan (Southern CA), else -> Keaton Kaess (West)


# The ZIP ranges those two states actually occupy. Only these two states are
# split by ZIP, so only these two can be misrouted by a ZIP that does not
# belong to the state -- and a lead arrived with state "Pennsylvania" and ZIP
# 38133 (Bartlett, Tennessee), which the split rule would have handed to the
# Northeast rep with no sign anything was wrong. When they disagree, neither
# value can be trusted, so the lead goes to a human instead.
_STATE_ZIP_RANGES = {
    'california': (90000, 96199),
    'pennsylvania': (15000, 19699),
}

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


# ---------------------------------------------------------------------------
# Customer Relationship Rep (CRR) -- reference only
#
# Who to talk to about a lead, shown on the dashboard and deliberately NEVER
# sent to Oracle: Oracle's Owner is the TM above. Transcribed from
# reference/Sales_List.pdf, whose MR/TM column was checked row by row against
# the TM tables above -- all 29 rows of its second page agreed, which is what
# makes the row alignment trustworthy.
#
# The sheet has no EMEA or Asia table. Keaton supplied the EMEA rule directly
# on 2026-08-10: everything EMEA is Elif, except Serdar Eren's territory,
# which is Sandra.
# ---------------------------------------------------------------------------

US_STATE_TO_CRR = {
    # Left-hand column of Sales_List.pdf page 1
    'alaska': 'Teresa Meier', 'alabama': 'Teresa Meier', 'arkansas': 'Barbara Stubbs',
    'arizona': 'Trinidad Bivian', 'colorado': 'Teresa Meier',
    'connecticut': 'Claudine Allen', 'delaware': 'Claudine Allen',
    'florida': 'Teresa Meier', 'georgia': 'Teresa Meier', 'hawaii': 'Teresa Meier',
    'iowa': 'Barbara Stubbs', 'idaho': 'Teresa Meier', 'illinois': 'Barbara Stubbs',
    'indiana': 'Alicia Sabillon', 'kansas': 'Barbara Stubbs', 'kentucky': 'Alicia Sabillon',
    'louisiana': 'Barbara Stubbs', 'massachusetts': 'Claudine Allen',
    'maryland': 'Claudine Allen', 'maine': 'Claudine Allen', 'michigan': 'Alicia Sabillon',
    'minnesota': 'Barbara Stubbs', 'missouri': 'Barbara Stubbs',
    'mississippi': 'Teresa Meier', 'montana': 'Teresa Meier',
    'north carolina': 'Teresa Meier',

    # Right-hand column, whose reps are on page 3
    'north dakota': 'Teresa Meier', 'nebraska': 'Trinidad Bivian',
    'new hampshire': 'Claudine Allen', 'new jersey': 'Claudine Allen',
    'new mexico': 'Barbara Stubbs', 'new york': 'Claudine Allen',
    'ohio': 'Alicia Sabillon', 'oklahoma': 'Barbara Stubbs', 'oregon': 'Teresa Meier',
    # Both halves of Pennsylvania are Claudine Allen, unlike the TM split.
    'pennsylvania': 'Claudine Allen',
    'rhode island': 'Claudine Allen', 'south carolina': 'Teresa Meier',
    'south dakota': 'Teresa Meier', 'tennessee': 'Alicia Sabillon',
    'texas': 'Barbara Stubbs', 'utah': 'Teresa Meier', 'virginia': 'Claudine Allen',
    'vermont': 'Claudine Allen', 'washington': 'Teresa Meier',
    'wisconsin': 'Barbara Stubbs', 'west virginia': 'Claudine Allen',
    'wyoming': 'Teresa Meier',
}

CANADA_PROVINCE_TO_CRR = {
    # "Canada West: AB, BC, & SK"
    'ab': 'Teresa Meier', 'alberta': 'Teresa Meier',
    'bc': 'Teresa Meier', 'british columbia': 'Teresa Meier',
    'sk': 'Teresa Meier', 'saskatchewan': 'Teresa Meier',
    # "Mid Canada: MB & NU"
    'mb': 'Claudine Allen', 'manitoba': 'Claudine Allen',
    'nu': 'Claudine Allen', 'nunavut': 'Claudine Allen',
    # "Eastern Canada: ON" and "NB, NL, NS, QC, PEI"
    'on': 'Claudine Allen', 'ontario': 'Claudine Allen',
    'nb': 'Claudine Allen', 'new brunswick': 'Claudine Allen',
    'nl': 'Claudine Allen', 'newfoundland and labrador': 'Claudine Allen',
    'ns': 'Claudine Allen', 'nova scotia': 'Claudine Allen',
    'qc': 'Claudine Allen', 'quebec': 'Claudine Allen',
    'pei': 'Claudine Allen', 'pe': 'Claudine Allen', 'prince edward island': 'Claudine Allen',
    # NWT and Yukon are not on the sales list, though they are on the TM sheet.
}

# "CRR SPECIAL ASSIGNMENTS", page 2 of the sales list.
_CRR_PUERTO_RICO = 'Trinidad Bivian'
_CRR_LATIN_AMERICA = 'Alicia Sabillon'

# EMEA and Asia, per Keaton on 2026-08-10. The sales list has no table for
# either region. "Sandra van der Maat" was confirmed by him as the right
# Sandra (she is the BV Office entry on the list); "Elif" appears on no sheet
# here, so it is recorded exactly as given.
_CRR_EMEA_DEFAULT = 'Elif'
_CRR_EMEA_SERDAR = 'Sandra van der Maat'
_EMEA_TMS = {'Davide Amato', 'Marcus James', 'Ralph Delcourt', 'Serdar Eren'}
_SERDAR = 'Serdar Eren'

# Asia is Alan Lam throughout -- including the Oceania countries (Australia,
# New Zealand), which the territory sheet files under its Asia section with
# Sam Tam rather than giving them a region of their own.
_CRR_ASIA = 'Alan Lam'
_ASIA_TMS = {'Alan Lam', 'Nozomi Kato', 'Sam Tam'}

# Derived from COUNTRY_TO_TM only, never from the US/Canada tables: John Pan
# holds Southern California as well as the Americas, and a San Diego lead is
# not a Latin America lead.
_LATIN_AMERICA_COUNTRIES = {
    country for country, tm in COUNTRY_TO_TM.items() if tm == 'John Pan'
} | {'puerto rico'}


def assign_crr(lead, tm=None):
    """
    Return (crr_name, basis) -- the sales person to talk to about this lead.

    Reference only. Oracle's Owner is the TM from assign_tm(); this never
    reaches Oracle, and is computed for display rather than stored so it
    cannot leak into a submission payload by accident.

    `tm` is the already-assigned Territory Manager, which is what decides the
    EMEA cases: the sales list has no EMEA table, and the rule is defined in
    terms of whose territory it is.
    """
    country = (lead.get('country') or '').strip().lower()
    state_raw = (lead.get('state') or '').strip()
    zip5 = _zip5(lead.get('zip'))

    if country == 'puerto rico':
        return _CRR_PUERTO_RICO, 'Puerto Rico'
    if country in _LATIN_AMERICA_COUNTRIES:
        return _CRR_LATIN_AMERICA, f'{country.title()} (Mexico, Central and South America)'

    if tm in _EMEA_TMS:
        if tm == _SERDAR:
            return _CRR_EMEA_SERDAR, f'EMEA, {_SERDAR}'
        return _CRR_EMEA_DEFAULT, f'EMEA, {tm}'

    if tm in _ASIA_TMS:
        return _CRR_ASIA, f'Asia, {tm}'

    if not country or country in _US_COUNTRY_NAMES:
        if not state_raw:
            return None, 'No state on file -- cannot determine the sales rep'
        state = _normalize_us_state(state_raw)

        if state == 'california':
            if not zip5:
                return None, 'California -- need a ZIP code to tell North from South'
            if int(zip5) <= _CA_SOUTHERN_MAX_ZIP:
                return 'Trinidad Bivian', f'California, ZIP {zip5} (<= {_CA_SOUTHERN_MAX_ZIP})'
            return 'Teresa Meier', f'California, ZIP {zip5} (> {_CA_SOUTHERN_MAX_ZIP})'

        if state == 'nevada':
            # Same Clark County split as the TM sheet, and the same heuristic.
            if zip5 and zip5[:3] in _CLARK_COUNTY_NV_ZIP3:
                return 'Trinidad Bivian', f'Nevada, ZIP {zip5} (Las Vegas / Clark County)'
            if not zip5:
                return None, 'Nevada -- need a ZIP code to tell Clark County apart'
            return 'Teresa Meier', f'Nevada, ZIP {zip5} (outside Clark County)'

        crr = US_STATE_TO_CRR.get(state)
        if crr:
            return crr, f'US state: {state_raw}'
        return None, f'US state "{state_raw}" is not on the sales list'

    if country in _CANADA_COUNTRY_NAMES:
        if not state_raw:
            return None, 'Canada -- no province on file'
        province = _normalize_us_state(state_raw)
        crr = CANADA_PROVINCE_TO_CRR.get(province) or CANADA_PROVINCE_TO_CRR.get(
            state_raw.strip().lower()
        )
        if crr:
            return crr, f'Canada province: {state_raw}'
        return None, f'Canadian province "{state_raw}" is not on the sales list'

    return None, f'{country.title() or "This country"} is not on the sales list'


def _normalize_us_state(state):
    # Trailing punctuation and doubled spaces come straight from how people
    # write addresses -- a real lead arrived with "Tn." and went unassigned
    # because it matched neither "TN" nor "tennessee".
    s = ' '.join(state.strip().strip('.,').split())
    if len(s) == 2 and s.upper() in US_STATE_ABBR:
        return US_STATE_ABBR[s.upper()].lower()
    return s.lower()


def assign_tm(lead):
    """
    Return (tm_name, basis) for a lead, from the territory sheet.

    `basis` names the rule that produced the answer ("Ohio", "California, ZIP
    92610 (<= 93599)") so a reviewer can see why a lead landed with a
    particular rep, and a None tm always comes with the reason it could not be
    decided rather than a silent blank.

    Until 2026-08-10 this returned Keaton Kaess unconditionally -- an override
    added while the pipeline was being evaluated, which made every one of the
    rules below dead code. Removing it is what turns territory assignment back
    on; see reference/TM_Territories.pdf for the sheet it implements.
    """
    country = (lead.get('country') or '').strip().lower()
    state_raw = (lead.get('state') or '').strip()
    zip5 = _zip5(lead.get('zip'))

    if not country or country in _US_COUNTRY_NAMES:
        if not state_raw:
            return None, 'No state on file -- cannot determine US territory'
        state = _normalize_us_state(state_raw)

        # Only the ZIP-split states can be misrouted by a ZIP belonging to a
        # different state, so that is the only place this is checked.
        if state in _STATE_ZIP_RANGES and zip5:
            low, high = _STATE_ZIP_RANGES[state]
            if not low <= int(zip5) <= high:
                return None, (
                    f'State "{state_raw}" and ZIP {zip5} disagree -- {zip5} is not a '
                    f'{state.title()} ZIP. Check the address before assigning.'
                )

        if state == 'california':
            if not zip5:
                return None, 'California -- need a ZIP code to determine North/South split'
            if int(zip5) <= _CA_SOUTHERN_MAX_ZIP:
                return 'John Pan', f'California, ZIP {zip5} (<= {_CA_SOUTHERN_MAX_ZIP})'
            return 'Keaton Kaess', f'California, ZIP {zip5} (> {_CA_SOUTHERN_MAX_ZIP})'


        if state == 'nevada':
            if not zip5:
                return None, 'Nevada -- need a ZIP code to determine the Clark County split'
            if zip5[:3] in _CLARK_COUNTY_NV_ZIP3:
                return 'John Pan', f'Nevada, ZIP {zip5} -- inferred Clark County (ZIP-prefix heuristic, verify)'
            return 'Keaton Kaess', f'Nevada, ZIP {zip5} -- inferred not Clark County (ZIP-prefix heuristic, verify)'

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
        {'country': 'USA', 'state': 'PA', 'zip': '15222'},   # Pittsburgh -> Bill Rost
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
