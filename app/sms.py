"""Small helpers about SMS text itself: phone format and segment counting."""

from __future__ import annotations

import re

# ASCII digits only, matched against the whole string. "\d" would also accept
# digits from other scripts and "$" would let a trailing newline through.
E164 = re.compile(r"\+[1-9][0-9]{7,14}")

# GSM 03.38 basic character set. Text made only of these (plus the extension
# set below) travels as 7 bit and gets 160 characters per single segment.
GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# These cost two septets each (escape + character).
GSM7_EXTENDED = frozenset("^{}\\[~]|€")


def is_valid_phone(phone: str) -> bool:
    return bool(E164.fullmatch(phone or ""))


def segment_info(body: str) -> dict:
    """Describe how a carrier would split ``body``.

    Returns encoding ("GSM-7" or "UCS-2"), units used and segment count.
    This is an estimate for the reviewer, not a billing figure.
    """
    if all(ch in GSM7_BASIC or ch in GSM7_EXTENDED for ch in body):
        units = sum(2 if ch in GSM7_EXTENDED else 1 for ch in body)
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        # UCS-2 counts UTF-16 code units, so an emoji costs two.
        units = len(body.encode("utf-16-be")) // 2
        single, multi, encoding = 70, 67, "UCS-2"
    if units == 0:
        segments = 0
    elif units <= single:
        segments = 1
    else:
        segments = -(-units // multi)
    return {"encoding": encoding, "units": units, "segments": segments}
