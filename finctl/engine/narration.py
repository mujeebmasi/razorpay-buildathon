"""Recovering payment references out of free-text bank narration.

A bank statement's narration field is the only place the UTR usually survives,
and every bank mangles it differently:

    NEFT-AXISP00123456789-RAZORPAY SOFTWARE PRIVATE LIMITED
    MMT/IMPS/512345678901/Settlement/RAZORPAY
    RTGS-HDFCR52026071500123456-RZPY PAYOUT
    BY TRANSFER-NEFT*SBIN0000001*RZPX123456789012*RAZORPAY
    ACH C- RAZORPAY SOFTWARE-1234567890123456 (truncated at 40 ch

The approach is deliberately two-stage. First, extract every token that could
structurally be a reference and score it by shape alone -- no knowledge of what
we are looking for, so the extraction cannot be biased toward confirming a
hypothesis. Second, compare candidates against known UTRs with a bounded edit
distance that models the specific corruptions banks introduce: truncation at a
field-width limit, digit transposition, and separator injection.

Keeping those stages separate matters. A single-stage "does the narration
contain the UTR" check silently succeeds on substring coincidences, and on a
5,000-line statement, coincidences happen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Iterable

# Payment-rail prefixes that mark the following token as a reference. Their
# presence is strong positional evidence, quite apart from the token's shape.
_RAIL_TOKENS: Final[frozenset[str]] = frozenset({
    "NEFT", "RTGS", "IMPS", "UPI", "ACH", "MMT", "P2A", "P2M",
    "TRF", "TRANSFER", "PAYOUT", "SETTLEMENT", "CMS", "INF", "INB",
})

# Words that are never references, however reference-shaped they look. Bank
# narrations are full of counterparty names in the same token positions.
_STOPWORDS: Final[frozenset[str]] = frozenset({
    "RAZORPAY", "SOFTWARE", "PRIVATE", "LIMITED", "LTD", "PVT", "INDIA",
    "BANK", "BY", "TO", "FROM", "FOR", "THE", "AND", "CR", "DR",
    "SETTLEMENT", "PAYMENT", "PAYOUT", "TRANSFER", "CREDIT", "DEBIT",
    "MERCHANT", "COLLECTION", "ACCOUNT", "REF", "REFNO", "TXN",
})

# Narration is split on everything that is not alphanumeric. Banks use -, /,
# *, ., :, and plain spaces interchangeably, sometimes several in one line.
_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]+")

# Shapes that a real reference takes on Indian payment rails.
_SHAPE_RULES: Final[tuple[tuple[re.Pattern[str], str, float], ...]] = (
    (re.compile(r"^[A-Z]{4}[A-Z0-9]{12}$"), "neft_utr", 0.95),      # 16ch, IFSC-prefixed
    (re.compile(r"^[A-Z]{4}[A-Z0-9]{18}$"), "rtgs_utr", 0.95),      # 22ch
    (re.compile(r"^\d{12}$"), "imps_rrn", 0.90),                     # 12-digit RRN
    (re.compile(r"^[A-Z]{2,6}\d{8,16}$"), "prefixed_ref", 0.75),     # RZPX123456789012
    (re.compile(r"^[A-Z0-9]{14,24}$"), "long_alnum", 0.60),
    (re.compile(r"^\d{9,18}$"), "numeric_ref", 0.55),
    (re.compile(r"^[A-Z0-9]{10,13}$"), "short_alnum", 0.40),
)

# A reference must contain digits; an all-alpha token of any length is a name.
_HAS_DIGIT: Final[re.Pattern[str]] = re.compile(r"\d")


@dataclass(frozen=True, slots=True)
class RefCandidate:
    """A token from a narration that might be a payment reference.

    `shape` names the rail format it structurally resembles. `score` combines
    shape confidence with positional evidence. `position` is the token index,
    retained because references cluster near the start of a narration and that
    is weak but real signal.
    """

    token: str
    shape: str
    score: float
    position: int
    preceded_by_rail: bool

    def __str__(self) -> str:
        return f"{self.token} ({self.shape}, {self.score:.2f})"


def normalize_ref(value: str | None) -> str:
    """Reduce a reference to its comparable core: uppercase alphanumerics only.

    Banks insert and drop separators freely, so 'AXIS-P001 234' and
    'AXISP001234' are the same reference and must compare equal.
    """
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def extract_candidates(narration: str) -> list[RefCandidate]:
    """Every plausible reference token in a narration, best-scoring first.

    Scoring is on shape and position only. No target UTR is consulted, so this
    cannot be talked into confirming a match that is not structurally there.
    """
    if not narration:
        return []

    tokens = [t for t in _TOKEN_SPLIT.split(narration.upper()) if t]
    candidates: list[RefCandidate] = []

    for index, token in enumerate(tokens):
        if token in _STOPWORDS or not _HAS_DIGIT.search(token):
            continue

        shape, base_score = "", 0.0
        for pattern, shape_name, shape_score in _SHAPE_RULES:
            if pattern.match(token):
                shape, base_score = shape_name, shape_score
                break
        if not shape:
            continue

        # A rail marker immediately before the token is strong evidence that
        # this is the reference rather than an account number or a date.
        preceded = index > 0 and tokens[index - 1] in _RAIL_TOKENS
        score = base_score + (0.10 if preceded else 0.0)
        # References appear early; a reference-shaped token at position 9 is
        # more often an account number or a running balance.
        score -= min(index, 8) * 0.015

        candidates.append(
            RefCandidate(
                token=token,
                shape=shape,
                score=round(min(score, 1.0), 4),
                position=index,
                preceded_by_rail=preceded,
            )
        )

    # Sorted by score, then by token so ties resolve identically on every run.
    candidates.sort(key=lambda c: (-c.score, c.token))
    return candidates


@lru_cache(maxsize=100_000)
def bounded_edit_distance(a: str, b: str, max_distance: int = 2) -> int:
    """Damerau-Levenshtein distance, abandoned once it exceeds `max_distance`.

    Transposition is a first-class edit here (not two substitutions) because
    the dominant human error in re-keying a reference is swapping adjacent
    characters, and treating it as distance 1 keeps a real match reachable at a
    tight threshold.

    Returns `max_distance + 1` to signal "further apart than we care about",
    which lets callers reject without paying for the full matrix.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_distance:
        return max_distance + 1

    previous_previous: list[int] = []
    previous = list(range(len(b) + 1))

    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        row_best = current[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                current[j - 1] + 1,        # insertion
                previous[j] + 1,           # deletion
                previous[j - 1] + cost,    # substitution
            )
            # Damerau transposition of two adjacent characters.
            if (
                i > 1
                and j > 1
                and ca == b[j - 2]
                and a[i - 2] == cb
                and previous_previous
            ):
                current[j] = min(current[j], previous_previous[j - 2] + cost)
            row_best = min(row_best, current[j])
        # Every future row is monotonically non-decreasing from here, so once a
        # whole row exceeds the bound the answer cannot come back under it.
        if row_best > max_distance:
            return max_distance + 1
        previous_previous, previous = previous, current

    return previous[len(b)]


def score_reference_match(utr: str, narration: str) -> tuple[float, str, str]:
    """How strongly a narration supports a specific UTR.

    Returns (score, mechanism, matched_token). The mechanism names *how* it
    matched, which goes straight into the evidence trail -- an operator
    reviewing a match needs to know the difference between "the UTR was printed
    in full" and "a token was two edits away from it".

    Truncation is handled explicitly as its own mechanism because bank exports
    routinely clip narration at a fixed field width, and the resulting prefix
    match is genuinely strong evidence rather than a weak partial.
    """
    target = normalize_ref(utr)
    if not target or not narration:
        return 0.0, "no_reference", ""

    flattened = normalize_ref(narration)

    # Whole UTR present verbatim somewhere in the narration.
    if target in flattened:
        return 1.0, "exact_substring", target

    candidates = extract_candidates(narration)
    best = (0.0, "no_match", "")

    for candidate in candidates:
        token = candidate.token

        if token == target:
            return 1.0, "exact_token", token

        # Truncation: the token is a proper prefix of the UTR, or the UTR
        # begins with the token. Long shared prefixes are decisive; short ones
        # are coincidence, so the threshold is on absolute length.
        if len(token) >= 8 and target.startswith(token):
            score = 0.70 + 0.02 * min(len(token) - 8, 8)
            if score > best[0]:
                best = (round(score, 4), "truncated_prefix", token)
            continue

        # Near-miss: transposed or mistyped characters.
        if abs(len(token) - len(target)) <= 2 and len(token) >= 10:
            distance = bounded_edit_distance(token, target, 2)
            if distance <= 2:
                score = {1: 0.85, 2: 0.65}[distance] if distance else 1.0
                if score > best[0]:
                    best = (score, f"edit_distance_{distance}", token)

    return best


def narration_mentions_any(narration: str, needles: Iterable[str]) -> bool:
    """Whether a narration contains any of several literal markers.

    Used for coarse classification -- spotting that a credit is a loan
    disbursal or an inter-account sweep rather than a settlement -- where a
    substring test is sufficient and edit distance would be overreach.
    """
    flattened = narration.upper()
    return any(needle.upper() in flattened for needle in needles)
