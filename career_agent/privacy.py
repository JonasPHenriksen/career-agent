"""Redact national ID numbers, bank and card numbers before text is sent to a cloud model.

Names, emails and phone numbers are left alone: the model needs context, and your contact
details are added to exported documents from Settings, not from the model.
"""
from __future__ import annotations

import re
from datetime import date

# Danish CPR (DDMMYY-XXXX), Norwegian fødselsnummer (11 digits), Swedish personnummer.
_CPR = re.compile(r"\b(\d{2})(\d{2})(\d{2})[- ]?(\d{4})\b")
_NO_FNR = re.compile(r"\b(\d{2})(\d{2})(\d{2})(\d{5})\b")
_SE_PNR = re.compile(r"\b(19|20)?(\d{2})(\d{2})(\d{2})[-+](\d{4})\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# Danish bank account "reg.nr. 1234 konto 1234567890"
_DK_ACCOUNT = re.compile(r"(?i)\b(reg\.?\s*(?:nr\.?)?\s*:?\s*)\d{4}([\s,/-]*(?:konto(?:nr\.?)?\s*:?\s*)?)\d{6,10}\b")


def _valid_date(dd: str, mm: str, yy: str) -> bool:
    try:
        d, m = int(dd), int(mm)
        if not (1 <= d <= 31 and 1 <= m <= 12):
            return False
        date(2000 + int(yy) if int(yy) < 50 else 1900 + int(yy), m, d)
        return True
    except ValueError:
        return False


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> str:
    if not text:
        return text

    def cpr(m: re.Match) -> str:
        return "[ID-NUMBER]" if _valid_date(m.group(1), m.group(2), m.group(3)) else m.group(0)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[CARD-NUMBER]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    text = _IBAN.sub("[BANK-ACCOUNT]", text)
    text = _DK_ACCOUNT.sub(lambda m: f"{m.group(1)}[BANK-ACCOUNT]", text)
    text = _SE_PNR.sub(lambda m: "[ID-NUMBER]" if _valid_date(m.group(4), m.group(3), m.group(2)) else m.group(0), text)
    text = _CPR.sub(cpr, text)
    text = _NO_FNR.sub(cpr, text)
    text = _CARD.sub(card, text)
    return text
