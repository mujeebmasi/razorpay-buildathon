"""PSP rate card: forward calculation, verification, and inversion.

The forward direction (gross -> fee -> net) is straightforward. The inverse
(net -> gross) is the interesting one, and it is what lets a bare bank credit
be traced back to the customer payment that produced it when no identifier
survived the trip.

Inversion is non-trivial because the fee schedule rounds twice -- once on the
commission and again on the GST charged on that commission. Those roundings
make the mapping from gross to net non-injective in places: several adjacent
gross amounts can produce the same net. The honest response is to return every
gross that could have produced the observed net and let the caller treat a
multi-valued answer as ambiguity rather than silently taking the first.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Final, Iterable, Mapping

# GST on payment-gateway commission, as an exact fraction to avoid any float.
GST_NUMERATOR: Final[int] = 18
GST_DENOMINATOR: Final[int] = 100


@dataclass(frozen=True, slots=True)
class RateCardEntry:
    """One line of the negotiated rate card.

    `rate_bps` is basis points of the gross (100 bps = 1%). `flat_paise` is a
    per-transaction fixed component. `threshold_paise` and `rate_bps_above`
    express the tiered pricing that debit cards actually carry, where the rate
    changes above a ticket size.
    """

    method: str
    rate_bps: int
    flat_paise: int = 0
    threshold_paise: int | None = None
    rate_bps_above: int | None = None

    def commission(self, gross: int) -> int:
        """Commission in paise before GST, rounded half-up at the paise."""
        bps = self.rate_bps
        if self.threshold_paise is not None and gross > self.threshold_paise:
            bps = self.rate_bps_above if self.rate_bps_above is not None else bps
        pct = (Decimal(gross) * Decimal(bps) / Decimal(10_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return int(pct) + self.flat_paise


#: A representative negotiated rate card. UPI and RuPay debit are zero-MDR by
#: regulation; the rest reflect typical mid-market gateway pricing.
DEFAULT_RATE_CARD: Final[Mapping[str, RateCardEntry]] = {
    "upi":           RateCardEntry("upi", rate_bps=0),
    "rupay_debit":   RateCardEntry("rupay_debit", rate_bps=0),
    "debit_card":    RateCardEntry("debit_card", rate_bps=40, threshold_paise=200_000,
                                   rate_bps_above=90),
    "credit_card":   RateCardEntry("credit_card", rate_bps=200),
    "netbanking":    RateCardEntry("netbanking", rate_bps=190),
    "wallet":        RateCardEntry("wallet", rate_bps=200),
    "emi":           RateCardEntry("emi", rate_bps=250),
    "amex":          RateCardEntry("amex", rate_bps=250),
    "international": RateCardEntry("international", rate_bps=300),
}

#: Fallback for a method absent from the card. Used only so the engine degrades
#: to a flagged deviation rather than crashing on an unrecognised method.
_UNKNOWN_METHOD = RateCardEntry("unknown", rate_bps=200)


def gst_on(commission: int) -> int:
    """GST payable on a commission amount, rounded half-up at the paise."""
    return int(
        (Decimal(commission) * GST_NUMERATOR / GST_DENOMINATOR).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def fee_breakdown(
    gross: int, method: str, card: Mapping[str, RateCardEntry] = DEFAULT_RATE_CARD
) -> tuple[int, int]:
    """Return (commission, gst) for a gross amount under the given rate card."""
    entry = card.get(method, _UNKNOWN_METHOD)
    commission = entry.commission(gross)
    return commission, gst_on(commission)


def net_from_gross(
    gross: int, method: str, card: Mapping[str, RateCardEntry] = DEFAULT_RATE_CARD
) -> int:
    """What should reach the bank for a given gross payment."""
    commission, gst = fee_breakdown(gross, method, card)
    return gross - commission - gst


def verify_fee(
    gross: int,
    charged_fee: int,
    charged_tax: int,
    method: str,
    *,
    card: Mapping[str, RateCardEntry] = DEFAULT_RATE_CARD,
    tolerance: int = 1,
) -> tuple[bool, int, str]:
    """Check a charged fee against the contracted rate card.

    Returns (ok, delta_paise, explanation). `tolerance` defaults to a single
    paise to absorb the legitimate half-up rounding disagreement between two
    implementations, while still catching a genuine overcharge.

    A positive delta means the PSP charged more than the card allows, which is
    recoverable money and is why this check exists at all.
    """
    expected_commission, expected_gst = fee_breakdown(gross, method, card)
    expected_total = expected_commission + expected_gst
    actual_total = charged_fee + charged_tax
    delta = actual_total - expected_total

    if abs(delta) <= tolerance:
        return True, delta, f"fee matches rate card for {method}"

    direction = "overcharged" if delta > 0 else "undercharged"
    return (
        False,
        delta,
        (
            f"{direction} by {abs(delta)} paise on {method}: "
            f"expected {expected_commission}+{expected_gst} GST = {expected_total}, "
            f"charged {charged_fee}+{charged_tax} = {actual_total}"
        ),
    )


def invert_net_to_gross(
    net: int,
    method: str,
    *,
    card: Mapping[str, RateCardEntry] = DEFAULT_RATE_CARD,
    search_radius: int = 4,
) -> list[int]:
    """Every gross amount that could produce this net under the rate card.

    Strategy: close the algebraic form ignoring rounding to get a seed, then
    verify candidates in a small neighbourhood by running the forward
    calculation. The forward direction is exact, so anything that survives is
    genuinely a valid pre-image rather than an approximation.

    Returns a sorted list. Empty means the net is unreachable under this card,
    which is itself informative -- it usually means the method is misattributed.
    A list longer than one means the rounding made the answer genuinely
    ambiguous, and the caller must not just pick one.
    """
    entry = card.get(method, _UNKNOWN_METHOD)

    # Seed: net = g - r*g - 0.18*r*g  =>  g = (net + flat*1.18) / (1 - 1.18r)
    effective = Decimal(entry.rate_bps) / Decimal(10_000)
    gst_multiplier = Decimal(GST_NUMERATOR + GST_DENOMINATOR) / Decimal(GST_DENOMINATOR)
    denominator = Decimal(1) - effective * gst_multiplier
    if denominator <= 0:
        return []  # a rate card that consumes the entire payment is not invertible

    seed = int(
        ((Decimal(net) + Decimal(entry.flat_paise) * gst_multiplier) / denominator)
        .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    # A tiered card has a discontinuity at the threshold, so the algebraic seed
    # can land on the wrong side of it. Seeding from both tiers covers that.
    seeds = {seed}
    if entry.threshold_paise is not None and entry.rate_bps_above is not None:
        alt_effective = Decimal(entry.rate_bps_above) / Decimal(10_000)
        alt_denominator = Decimal(1) - alt_effective * gst_multiplier
        if alt_denominator > 0:
            seeds.add(
                int(
                    ((Decimal(net) + Decimal(entry.flat_paise) * gst_multiplier) / alt_denominator)
                    .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
            )

    found: set[int] = set()
    for s in seeds:
        for candidate in range(max(1, s - search_radius), s + search_radius + 1):
            if net_from_gross(candidate, method, card) == net:
                found.add(candidate)
    return sorted(found)


def invert_across_methods(
    net: int,
    methods: Iterable[str] = tuple(DEFAULT_RATE_CARD),
    *,
    card: Mapping[str, RateCardEntry] = DEFAULT_RATE_CARD,
) -> dict[str, list[int]]:
    """Invert a net against several plausible methods at once.

    When the payment method is unknown -- a common state for a bare bank credit
    -- this produces the full hypothesis space. Every entry is a real candidate
    the caller must disambiguate with other evidence, not a ranked guess.
    """
    return {
        method: grosses
        for method in methods
        if (grosses := invert_net_to_gross(net, method, card=card))
    }
