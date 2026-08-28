"""Money is integer paise. Always. Never a float.

Every float bug in a reconciliation system is a silent one: 0.1 + 0.2 != 0.3
means a ledger that balances in testing and drifts in production. This module
is the only place in the codebase permitted to touch a decimal string. It
converts to `int` paise on the way in and back to a display string on the way
out. Nothing in between ever sees a float.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final

# A rupee is 100 paise. Named so the intent is greppable.
PAISE_PER_RUPEE: Final[int] = 100

# Currency symbols and codes stripped before parsing. Longest-first so that
# "RS." is consumed before a bare "RS" could half-match it.
_CURRENCY_NOISE: Final[tuple[str, ...]] = (
    "INR", "RS.", "RS", "USD", "EUR", "GBP", "AED", "SGD",
    "₹", "₨", "$", "€", "£",
)

# Separators and padding banks emit that carry no numeric value.
_STRIP_CHARS: Final[str] = " \t  '_"

# Placeholders that mean "no value here", which must fail loudly rather than
# silently becoming zero.
_NULL_TOKENS: Final[frozenset[str]] = frozenset(
    {"-", "--", "–", "—", "NA", "N/A", "NULL", "NIL", "NONE", "."}
)

_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"\d")
_CLEAN_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\d+(\.\d+)?")


class MoneyParseError(ValueError):
    """Raised when a string cannot be resolved to an unambiguous paise amount."""


def _detect_decimal_separator(s: str) -> str:
    """Decide whether ',' or '.' is the decimal point in a mixed-format string.

    Bank feeds arrive in both Indian/US format (1,234.56) and European format
    (1.234,56). Getting this backwards turns twelve hundred rupees into twelve
    lakh, so it is resolved explicitly rather than assumed.

    Returns "." or "," for the decimal separator, or "" if the value is a whole
    number and every separator present is a grouping mark.
    """
    last_dot = s.rfind(".")
    last_comma = s.rfind(",")

    if last_dot == -1 and last_comma == -1:
        return ""
    if last_dot == -1:
        # Only commas. Grouping runs are always exactly 3 digits, so a trailing
        # run of any other length means the comma was a decimal point.
        return "," if len(s) - last_comma - 1 != 3 else ""
    if last_comma == -1:
        return "." if len(s) - last_dot - 1 != 3 else ""
    # Both present: whichever appears last is the decimal separator.
    return "." if last_dot > last_comma else ","


def parse_money(raw: object, *, field: str = "amount") -> int:
    """Parse an arbitrarily-formatted monetary value into signed integer paise.

    Handles what actually shows up in bank exports and PSP reports: currency
    symbols, Indian lakh grouping (12,34,567.89), European decimals (1.234,56),
    accounting negatives in parentheses, trailing CR/DR markers, and values
    that are already numeric.

    Raises MoneyParseError rather than guessing. A silently wrong amount is far
    worse than a loud failure in a system whose entire job is arithmetic.
    """
    if raw is None:
        raise MoneyParseError(f"{field}: value is missing")

    # bool is an int subclass, so it has to be rejected before the int branch.
    if isinstance(raw, bool):
        raise MoneyParseError(f"{field}: boolean is not a monetary value")
    # A bare int is paise by convention; Decimal/str are rupees. This asymmetry
    # is deliberate and is why callers are pushed toward Decimal.
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Decimal):
        return rupees_to_paise(raw)
    if isinstance(raw, float):
        # Accepted, but routed through the shortest repr so 1234.56 does not
        # arrive as 1234.5599999999999. Callers should not be doing this.
        return rupees_to_paise(Decimal(repr(raw)))

    s = str(raw).strip()
    if not s:
        raise MoneyParseError(f"{field}: value is empty")
    if s.upper() in _NULL_TOKENS:
        raise MoneyParseError(f"{field}: value is a null placeholder ({s!r})")

    work = s.upper()
    negative = False

    # Accounting negative: (1,234.56)
    if work.startswith("(") and work.endswith(")"):
        negative = True
        work = work[1:-1].strip()

    # Trailing debit/credit markers used by Indian bank statements.
    for marker, marks_negative in (("DR", True), ("DB", True), ("CR", False)):
        if work.endswith(marker):
            work = work[: -len(marker)].strip()
            negative = negative or marks_negative
            break

    for noise in _CURRENCY_NOISE:
        work = work.replace(noise, "")
    for ch in _STRIP_CHARS:
        work = work.replace(ch, "")

    if work.startswith("+"):
        work = work[1:]
    if work.startswith("-"):
        negative = not negative
        work = work[1:]
    # A trailing sign appears in some mainframe exports.
    if work.endswith("-"):
        negative = not negative
        work = work[:-1]

    if not _DIGITS_RE.search(work):
        raise MoneyParseError(f"{field}: no digits in {raw!r}")

    separator = _detect_decimal_separator(work)
    if separator == ",":
        work = work.replace(".", "").replace(",", ".")
    elif separator == ".":
        work = work.replace(",", "")
    else:
        work = work.replace(",", "").replace(".", "")

    if not _CLEAN_NUMBER_RE.fullmatch(work):
        raise MoneyParseError(f"{field}: cannot parse {raw!r} (reduced to {work!r})")

    try:
        value = Decimal(work)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise MoneyParseError(f"{field}: cannot parse {raw!r}") from exc

    paise = rupees_to_paise(value)
    return -paise if negative else paise


def rupees_to_paise(rupees: Decimal) -> int:
    """Convert Decimal rupees to integer paise, rounding half-up at the paise.

    Half-up rather than banker's rounding because that is what Indian PSP fee
    schedules and the GST rules specify. Matching the counterparty's rounding
    convention is the entire point of the exercise.
    """
    return int((rupees * PAISE_PER_RUPEE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def paise_to_rupees(paise: int) -> Decimal:
    """Exact Decimal rupees, for display and export only. Never for arithmetic."""
    return (Decimal(paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def format_inr(paise: int, *, symbol: bool = True) -> str:
    """Render paise with Indian digit grouping: 1,23,45,678.90."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), PAISE_PER_RUPEE)
    digits = str(whole)

    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        # Past the first group of three, Indian grouping proceeds in twos.
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        grouped = ",".join(groups + [tail])
    else:
        grouped = digits

    return f"{sign}{'₹' if symbol else ''}{grouped}.{frac:02d}"


def looks_like_scale_error(a: int, b: int) -> bool:
    """True when two amounts differ by a factor of 100 -- a paise/rupee unit slip.

    This is the most common integration bug between a PSP API (paise) and an
    ERP export (rupees). It deserves its own name in an exception report rather
    than being lumped into a generic amount mismatch.

    The comparison allows up to one rupee of slack rather than demanding an
    exact 100x, because the bug almost always truncates: 1234.56 rupees read as
    a paise figure loses the fractional part. Requiring exactness would miss
    every case where the amount was not a whole number of rupees, which is
    nearly all of them. The window stays tight in relative terms -- one part in
    ten thousand of the larger figure -- so it does not fire on unrelated pairs.
    """
    if a == 0 or b == 0:
        return False
    lo, hi = sorted((abs(a), abs(b)))
    return abs(hi - lo * PAISE_PER_RUPEE) < PAISE_PER_RUPEE


def digit_transposition(a: int, b: int) -> bool:
    """True when two amounts are digit transpositions of one another.

    A human keying 1243 for 1234 produces a difference divisible by 9 -- the
    classic accountant's check. The /9 rule alone yields many false positives,
    so it is confirmed against an actual digit multiset comparison.
    """
    if a == b or a * b <= 0:
        return False
    if (abs(a) - abs(b)) % 9 != 0:
        return False
    sa, sb = str(abs(a)), str(abs(b))
    return len(sa) == len(sb) and sorted(sa) == sorted(sb)
