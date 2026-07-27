"""Normalize common domestic and international phone formats for Oracle."""

import re


_CALLING_CODES = {
    '1', '7', '20', '27', '30', '31', '32', '33', '34', '36', '39', '40',
    '41', '43', '44', '45', '46', '47', '48', '49', '51', '52', '53', '54',
    '55', '56', '57', '58', '60', '61', '62', '63', '64', '65', '66', '81',
    '82', '84', '86', '90', '91', '92', '93', '94', '95', '98', '211',
    '212', '213', '216', '218', '220', '221', '222', '223', '224', '225',
    '226', '227', '228', '229', '230', '231', '232', '233', '234', '235',
    '236', '237', '238', '239', '240', '241', '242', '243', '244', '245',
    '246', '248', '249', '250', '251', '252', '253', '254', '255', '256',
    '257', '258', '260', '261', '262', '263', '264', '265', '266', '267',
    '268', '269', '290', '291', '297', '298', '299', '350', '351', '352',
    '353', '354', '355', '356', '357', '358', '359', '370', '371', '372',
    '373', '374', '375', '376', '377', '378', '379', '380', '381', '382',
    '383', '385', '386', '387', '389', '420', '421', '423', '500', '501',
    '502', '503', '504', '505', '506', '507', '508', '509', '590', '591',
    '592', '593', '594', '595', '596', '597', '598', '599', '670', '672',
    '673', '674', '675', '676', '677', '678', '679', '680', '681', '682',
    '683', '685', '686', '687', '688', '689', '690', '691', '692', '850',
    '852', '853', '855', '856', '880', '886', '960', '961', '962', '963',
    '964', '965', '966', '967', '968', '970', '971', '972', '973', '974',
    '975', '976', '977', '992', '993', '994', '995', '996', '998',
}


def _split_calling_code(digits):
    for width in (3, 2, 1):
        candidate = digits[:width]
        if candidate in _CALLING_CODES:
            return candidate, digits[width:]
    return None, digits


def parse_phone(raw):
    """
    Return Oracle phone components or ``None`` for an unusable number.

    Extension text is deliberately omitted because Oracle has no extension
    field in this form. Numbers without an international prefix are treated
    as North American numbers.
    """
    if not raw or not str(raw).strip():
        return None

    value = str(raw).strip()
    value = re.split(r'\b(?:ext(?:ension)?|x)\b\.?\s*\d+', value, flags=re.I)[0]
    international = value.startswith('+') or value.startswith('00')
    digits = re.sub(r'\D', '', value)
    if value.startswith('00'):
        digits = digits[2:]

    if international:
        country_code, national = _split_calling_code(digits)
        if not country_code:
            return None
    else:
        country_code = '1'
        national = digits[1:] if len(digits) == 11 and digits.startswith('1') else digits

    if len(national) < 7:
        return None

    # Oracle splits phones into country/area/local fields. Area-code lengths
    # vary internationally; these common two-digit plans cover current data,
    # while three digits is a conservative fallback.
    area_width = 3 if country_code == '1' else (
        2 if country_code in {'20', '27', '30', '31', '32', '33', '34', '36',
                              '39', '40', '41', '43', '44', '45', '46', '47',
                              '48', '49'} else 3
    )
    area_code = national[:area_width]
    local = national[area_width:]
    number = f'{local[:3]}-{local[3:]}' if len(local) > 4 else local

    return {
        'type': 'work' if country_code == '1' else 'mobile',
        'country_code': country_code,
        'area_code': area_code,
        'number': number,
    }


if __name__ == '__main__':
    for sample in (
        '281-997-3300', '1-800-555-1234', '+1-310-739-8930',
        '+44 20 7946 0958', '+442012345678', '(713) 941-3797',
    ):
        print(f'{sample:<25} -> {parse_phone(sample)}')
