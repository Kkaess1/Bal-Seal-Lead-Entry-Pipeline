"""
ISO 3166-1 country lookup for Oracle's PrimaryContactCountry field.

Oracle's Create Lead UI lists the full ISO 3166 English short names in an
alphabetical dropdown. Confirmed from a screenshot of this tenant on
2026-07-31: the list includes Aland Islands, Anguilla and Antarctica, i.e. the
standard set with no tenant customization, so the alpha-2 codes below are the
right code set.

The REST field behind that dropdown takes the alpha-2 territory code -- that
matches the US/CA values this pipeline has been sending successfully. This has
NOT been verified for any other country against the live tenant, so NAME_BY_CODE
is kept alongside so oracle_api can send display names instead if codes are ever
rejected; see the country_format setting in config.json.
"""

import unicodedata

# ISO 3166-1 alpha-2 -> ISO English short name, as displayed in Oracle's
# dropdown. Keyed by code so the codes are guaranteed unique; the name -> code
# direction is derived below.
NAME_BY_CODE = {
    'AF': 'Afghanistan', 'AX': 'Aland Islands', 'AL': 'Albania', 'DZ': 'Algeria',
    'AS': 'American Samoa', 'AD': 'Andorra', 'AO': 'Angola', 'AI': 'Anguilla',
    'AQ': 'Antarctica', 'AG': 'Antigua and Barbuda', 'AR': 'Argentina',
    'AM': 'Armenia', 'AW': 'Aruba', 'AU': 'Australia', 'AT': 'Austria',
    'AZ': 'Azerbaijan', 'BS': 'Bahamas', 'BH': 'Bahrain', 'BD': 'Bangladesh',
    'BB': 'Barbados', 'BY': 'Belarus', 'BE': 'Belgium', 'BZ': 'Belize',
    'BJ': 'Benin', 'BM': 'Bermuda', 'BT': 'Bhutan', 'BO': 'Bolivia',
    'BQ': 'Bonaire, Sint Eustatius and Saba', 'BA': 'Bosnia and Herzegovina',
    'BW': 'Botswana', 'BV': 'Bouvet Island', 'BR': 'Brazil',
    'IO': 'British Indian Ocean Territory', 'BN': 'Brunei Darussalam',
    'BG': 'Bulgaria', 'BF': 'Burkina Faso', 'BI': 'Burundi', 'CV': 'Cabo Verde',
    'KH': 'Cambodia', 'CM': 'Cameroon', 'CA': 'Canada', 'KY': 'Cayman Islands',
    'CF': 'Central African Republic', 'TD': 'Chad', 'CL': 'Chile', 'CN': 'China',
    'CX': 'Christmas Island', 'CC': 'Cocos (Keeling) Islands', 'CO': 'Colombia',
    'KM': 'Comoros', 'CG': 'Congo', 'CD': 'Congo, Democratic Republic of the',
    'CK': 'Cook Islands', 'CR': 'Costa Rica', 'CI': "Cote d'Ivoire",
    'HR': 'Croatia', 'CU': 'Cuba', 'CW': 'Curacao', 'CY': 'Cyprus',
    'CZ': 'Czechia', 'DK': 'Denmark', 'DJ': 'Djibouti', 'DM': 'Dominica',
    'DO': 'Dominican Republic', 'EC': 'Ecuador', 'EG': 'Egypt',
    'SV': 'El Salvador', 'GQ': 'Equatorial Guinea', 'ER': 'Eritrea',
    'EE': 'Estonia', 'SZ': 'Eswatini', 'ET': 'Ethiopia',
    'FK': 'Falkland Islands', 'FO': 'Faroe Islands', 'FJ': 'Fiji',
    'FI': 'Finland', 'FR': 'France', 'GF': 'French Guiana',
    'PF': 'French Polynesia', 'TF': 'French Southern Territories',
    'GA': 'Gabon', 'GM': 'Gambia', 'GE': 'Georgia', 'DE': 'Germany',
    'GH': 'Ghana', 'GI': 'Gibraltar', 'GR': 'Greece', 'GL': 'Greenland',
    'GD': 'Grenada', 'GP': 'Guadeloupe', 'GU': 'Guam', 'GT': 'Guatemala',
    'GG': 'Guernsey', 'GN': 'Guinea', 'GW': 'Guinea-Bissau', 'GY': 'Guyana',
    'HT': 'Haiti', 'HM': 'Heard Island and McDonald Islands', 'VA': 'Holy See',
    'HN': 'Honduras', 'HK': 'Hong Kong', 'HU': 'Hungary', 'IS': 'Iceland',
    'IN': 'India', 'ID': 'Indonesia', 'IR': 'Iran', 'IQ': 'Iraq',
    'IE': 'Ireland', 'IM': 'Isle of Man', 'IL': 'Israel', 'IT': 'Italy',
    'JM': 'Jamaica', 'JP': 'Japan', 'JE': 'Jersey', 'JO': 'Jordan',
    'KZ': 'Kazakhstan', 'KE': 'Kenya', 'KI': 'Kiribati',
    'KP': "Korea, Democratic People's Republic of", 'KR': 'Korea, Republic of',
    'KW': 'Kuwait', 'KG': 'Kyrgyzstan', 'LA': "Lao People's Democratic Republic",
    'LV': 'Latvia', 'LB': 'Lebanon', 'LS': 'Lesotho', 'LR': 'Liberia',
    'LY': 'Libya', 'LI': 'Liechtenstein', 'LT': 'Lithuania', 'LU': 'Luxembourg',
    'MO': 'Macao', 'MG': 'Madagascar', 'MW': 'Malawi', 'MY': 'Malaysia',
    'MV': 'Maldives', 'ML': 'Mali', 'MT': 'Malta', 'MH': 'Marshall Islands',
    'MQ': 'Martinique', 'MR': 'Mauritania', 'MU': 'Mauritius', 'YT': 'Mayotte',
    'MX': 'Mexico', 'FM': 'Micronesia', 'MD': 'Moldova', 'MC': 'Monaco',
    'MN': 'Mongolia', 'ME': 'Montenegro', 'MS': 'Montserrat', 'MA': 'Morocco',
    'MZ': 'Mozambique', 'MM': 'Myanmar', 'NA': 'Namibia', 'NR': 'Nauru',
    'NP': 'Nepal', 'NL': 'Netherlands', 'NC': 'New Caledonia',
    'NZ': 'New Zealand', 'NI': 'Nicaragua', 'NE': 'Niger', 'NG': 'Nigeria',
    'NU': 'Niue', 'NF': 'Norfolk Island', 'MK': 'North Macedonia',
    'MP': 'Northern Mariana Islands', 'NO': 'Norway', 'OM': 'Oman',
    'PK': 'Pakistan', 'PW': 'Palau', 'PS': 'Palestine, State of',
    'PA': 'Panama', 'PG': 'Papua New Guinea', 'PY': 'Paraguay', 'PE': 'Peru',
    'PH': 'Philippines', 'PN': 'Pitcairn', 'PL': 'Poland', 'PT': 'Portugal',
    'PR': 'Puerto Rico', 'QA': 'Qatar', 'RE': 'Reunion', 'RO': 'Romania',
    'RU': 'Russian Federation', 'RW': 'Rwanda', 'BL': 'Saint Barthelemy',
    'SH': 'Saint Helena, Ascension and Tristan da Cunha',
    'KN': 'Saint Kitts and Nevis', 'LC': 'Saint Lucia', 'MF': 'Saint Martin',
    'PM': 'Saint Pierre and Miquelon',
    'VC': 'Saint Vincent and the Grenadines', 'WS': 'Samoa',
    'SM': 'San Marino', 'ST': 'Sao Tome and Principe', 'SA': 'Saudi Arabia',
    'SN': 'Senegal', 'RS': 'Serbia', 'SC': 'Seychelles', 'SL': 'Sierra Leone',
    'SG': 'Singapore', 'SX': 'Sint Maarten', 'SK': 'Slovakia', 'SI': 'Slovenia',
    'SB': 'Solomon Islands', 'SO': 'Somalia', 'ZA': 'South Africa',
    'GS': 'South Georgia and the South Sandwich Islands', 'SS': 'South Sudan',
    'ES': 'Spain', 'LK': 'Sri Lanka', 'SD': 'Sudan', 'SR': 'Suriname',
    'SJ': 'Svalbard and Jan Mayen', 'SE': 'Sweden', 'CH': 'Switzerland',
    'SY': 'Syrian Arab Republic', 'TW': 'Taiwan', 'TJ': 'Tajikistan',
    'TZ': 'Tanzania', 'TH': 'Thailand', 'TL': 'Timor-Leste', 'TG': 'Togo',
    'TK': 'Tokelau', 'TO': 'Tonga', 'TT': 'Trinidad and Tobago',
    'TN': 'Tunisia', 'TR': 'Turkiye', 'TM': 'Turkmenistan',
    'TC': 'Turks and Caicos Islands', 'TV': 'Tuvalu', 'UG': 'Uganda',
    'UA': 'Ukraine', 'AE': 'United Arab Emirates', 'GB': 'United Kingdom',
    'US': 'United States',
    'UM': 'United States Minor Outlying Islands', 'UY': 'Uruguay',
    'UZ': 'Uzbekistan', 'VU': 'Vanuatu', 'VE': 'Venezuela', 'VN': 'Viet Nam',
    'VG': 'Virgin Islands (British)', 'VI': 'Virgin Islands (U.S.)',
    'WF': 'Wallis and Futuna', 'EH': 'Western Sahara', 'YE': 'Yemen',
    'ZM': 'Zambia', 'ZW': 'Zimbabwe',
}

# Everyday names, former names, native names and abbreviations that leads
# actually arrive with. Every value here must be a key of NAME_BY_CODE --
# tests/test_country_codes.py enforces that.
ALIASES = {
    # United States / United Kingdom -- by far the most common inputs
    'USA': 'US', 'U S A': 'US', 'UNITED STATES OF AMERICA': 'US',
    'AMERICA': 'US', 'U S': 'US',
    'UK': 'GB', 'U K': 'GB', 'GREAT BRITAIN': 'GB', 'BRITAIN': 'GB',
    'ENGLAND': 'GB', 'SCOTLAND': 'GB', 'WALES': 'GB', 'NORTHERN IRELAND': 'GB',

    # Territory-list names that differ from the ISO short name
    'SOUTH KOREA': 'KR', 'KOREA': 'KR', 'REPUBLIC OF KOREA': 'KR',
    'NORTH KOREA': 'KP', 'DPRK': 'KP',
    'CZECH REPUBLIC': 'CZ', 'HOLLAND': 'NL', 'THE NETHERLANDS': 'NL',
    'IVORY COAST': 'CI', 'TURKEY': 'TR', 'BURMA': 'MM', 'SWAZILAND': 'SZ',
    'MACEDONIA': 'MK', 'CAPE VERDE': 'CV', 'EAST TIMOR': 'TL',
    'VATICAN': 'VA', 'VATICAN CITY': 'VA', 'UAE': 'AE',
    'RUSSIA': 'RU', 'VIETNAM': 'VN', 'SYRIA': 'SY', 'LAOS': 'LA',
    'BRUNEI': 'BN', 'MACAU': 'MO', 'PALESTINE': 'PS', 'BOSNIA': 'BA',
    'FALKLAND ISLANDS MALVINAS': 'FK', 'SAINT HELENA': 'SH',
    'BONAIRE': 'BQ', 'SOUTH GEORGIA': 'GS',

    # The two Congos -- distinct countries with distinct TMs in territory_map,
    # so these must not collapse into one another.
    'DEMOCRATIC REPUBLIC OF THE CONGO': 'CD', 'DEMOCRATIC REPUBLIC OF CONGO': 'CD',
    'DR CONGO': 'CD', 'DRC': 'CD', 'CONGO KINSHASA': 'CD',
    'REPUBLIC OF THE CONGO': 'CG', 'CONGO BRAZZAVILLE': 'CG',

    # Regions that arrive in place of the country on badges and signatures
    'SARDINIA': 'IT', 'SARDEGNA': 'IT', 'SICILY': 'IT',
    'HONG KONG SAR': 'HK', 'MAINLAND CHINA': 'CN', 'PRC': 'CN',

    # Native-language forms common on European tradeshow badges
    'DEUTSCHLAND': 'DE', 'OSTERREICH': 'AT', 'SCHWEIZ': 'CH', 'SUISSE': 'CH',
    'SVIZZERA': 'CH', 'ITALIA': 'IT', 'ESPANA': 'ES', 'BRASIL': 'BR',
    'NEDERLAND': 'NL', 'BELGIE': 'BE', 'BELGIQUE': 'BE', 'DANMARK': 'DK',
    'NORGE': 'NO', 'SVERIGE': 'SE', 'SUOMI': 'FI', 'POLSKA': 'PL',
    'MEXICO': 'MX', 'NIPPON': 'JP',
}


def normalize(value):
    """
    Fold a free-text country into a comparable key.

    Strips accents, punctuation and articles so 'Cote d'Ivoire', 'Côte
    d'Ivoire' and 'COTE DIVOIRE' all land on the same key.
    """
    if value is None:
        return ''
    text = unicodedata.normalize('NFKD', str(value))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = ''.join(ch if ch.isalnum() else ' ' for ch in text)
    text = ' '.join(text.split())
    if text.startswith('THE ') and text != 'THE NETHERLANDS':
        text = text[4:]
    return text


# name key -> code, built from the ISO names plus the aliases. Codes map to
# themselves so an already-coded value ('DE') passes through.
_CODE_BY_KEY = {}
for _code, _name in NAME_BY_CODE.items():
    _CODE_BY_KEY[normalize(_name)] = _code
    _CODE_BY_KEY[_code] = _code
for _alias, _code in ALIASES.items():
    _CODE_BY_KEY[normalize(_alias)] = _code


def to_alpha2(value):
    """Return the ISO 3166-1 alpha-2 code for a country, or None if unknown."""
    return _CODE_BY_KEY.get(normalize(value))


def to_display_name(value):
    """Return Oracle's dropdown label for a country, or None if unknown."""
    code = to_alpha2(value)
    return NAME_BY_CODE.get(code) if code else None
