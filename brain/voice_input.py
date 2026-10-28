"""Conservative English/Urdu speech digits; phone validity stays in domain code.

No grouped-number arithmetic, fuzzy words, private vocabulary or transliteration
is used. A None result means repeat the number as individual digits.
"""

import re

_DIGIT_WORDS = dict(zip("zero one two three four five six seven eight nine".split(), "0123456789"))
_DIGIT_WORDS.update(dict(zip("صفر ایک دو تین چار پانچ چھ سات آٹھ نو".split(), "0123456789")))
_DIGIT_WORDS["چھے"] = "6"
_DECIMALS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_phone_input(value: str) -> str | None:
    """Return explicit digits/punctuation, or None when extraction is ambiguous.

    Accepted numeral scripts are ASCII and the Arabic/Persian decimal digits used
    in Urdu. Other ASR scripts must be retried rather than silently repaired.
    The caller must still use PatientCreate.phone_number for phone validation.
    """
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        return None
    normalized = value.strip().casefold().translate(_DECIMALS)
    if re.fullmatch(r"\+?[0-9 ()-]+", normalized):
        numeric_digits = re.sub(r"[^0-9]", "", normalized)
        return normalized if 0 < len(numeric_digits) <= 15 else None
    tokens = normalized.split()
    prefix = ""
    if tokens and tokens[0] in {"plus", "پلس"}:
        prefix = "+"
        tokens = tokens[1:]
    if not tokens:
        return None
    digits = []
    for token in tokens:
        if token in _DIGIT_WORDS:
            digits.append(_DIGIT_WORDS[token])
        elif re.fullmatch(r"[0-9]+", token):
            digits.append(token)
        else:
            return None
    number = "".join(digits)
    return prefix + number if len(number) <= 15 else None
