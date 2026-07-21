import re


def parse_phone(raw):
    """
    Parse a phone string into its components.

    Returns a dict:
      {
        'type': 'work' | 'mobile',   # which Oracle fields to fill
        'country_code': str,          # e.g. '1', '33'
        'area_code':    str,          # e.g. '281'
        'number':       str,          # e.g. '997-3300'
      }
    Returns None if the string is empty or unparseable.

    Examples handled:
      '281-997-3300'             → work  | 1  | 281 | 997-3300
      '+1-310-739-8930'          → work  | 1  | 310 | 739-8930
      '+3-368-571-6706'          → mobile| 3  | 368 | 571-6706
      '+00-861-598-818-0124'     → mobile| 86 | 159 | 8818-0124
      '(713) 941-3797'           → work  | 1  | 713 | 941-3797
    """
    if not raw or not str(raw).strip():
        return None

    s = str(raw).strip()

    # Strip leading/trailing whitespace and common noise
    s = re.sub(r'[\s\(\)]', '-', s)      # spaces and parens → dashes
    s = re.sub(r'-{2,}', '-', s)          # collapse multiple dashes
    s = re.sub(r'^-|-$', '', s)            # strip leading/trailing dashes

    # Determine if there's an explicit country code prefix (+X or 00X)
    has_plus = s.startswith('+')
    has_double_zero = s.startswith('00')

    if has_plus:
        s = s[1:]  # remove leading +
    elif has_double_zero:
        s = s[2:]  # remove leading 00

    parts = s.split('-')
    parts = [p for p in parts if p]  # remove empty segments

    if not parts:
        return None

    # --- Determine country code ---
    if has_plus or has_double_zero:
        # First segment is country code — but handle +00CC where CC runs into next digits
        raw_cc = parts[0].lstrip('0')
        if not raw_cc:
            # e.g. '+00-86-...' → raw_cc was '00', actual CC is next segment
            country_code = parts[1].lstrip('0') or '0'
            remaining = parts[2:]
        else:
            country_code = raw_cc
            remaining = parts[1:]
    else:
        # No explicit country code — assume US (+1)
        country_code = '1'
        remaining = parts

    country_code = country_code or '1'

    # --- Build area code and number from remaining parts ---
    if not remaining:
        return None

    if len(remaining) == 1:
        # Everything lumped together, e.g. '2819973300'
        digits = re.sub(r'\D', '', remaining[0])
        if len(digits) >= 10:
            area_code = digits[:3]
            number = digits[3:6] + '-' + digits[6:]
        elif len(digits) >= 7:
            area_code = ''
            number = digits[:3] + '-' + digits[3:]
        else:
            area_code = ''
            number = digits
    elif len(remaining) == 2:
        area_code = re.sub(r'\D', '', remaining[0])
        num_digits = re.sub(r'\D', '', remaining[1])
        number = num_digits[:4] + '-' + num_digits[4:] if len(num_digits) > 4 else num_digits
    else:
        # 3+ parts: first is area code, rest form the number
        area_code = re.sub(r'\D', '', remaining[0])
        num_parts = remaining[1:]
        joined = '-'.join(num_parts)
        number = joined

    phone_type = 'work' if country_code == '1' else 'mobile'

    return {
        'type': phone_type,
        'country_code': country_code,
        'area_code': area_code,
        'number': number,
    }


if __name__ == '__main__':
    tests = [
        '281-997-3300',
        '+1-310-739-8930',
        '+3-368-571-6706',
        '+00-861-598-818-0124',
        '(713) 941-3797',
        '1-800-555-1234',
        '',
        None,
    ]
    for t in tests:
        print(f'{str(t):<35} -> {parse_phone(t)}')
