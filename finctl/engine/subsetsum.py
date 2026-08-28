"""Batch decomposition: which set of settlements produced this bank credit?

A PSP nets several payouts into one transfer, so a single bank credit of
Rs 4,52,390.17 may be the sum of six settlements. Recovering that set is
subset-sum, which is NP-complete in general but very tractable here because
reality supplies two strong constraints:

  * the candidate pool is bounded by a settlement date window, not the whole
    period, so n is tens rather than thousands; and
  * a real batch combines a handful of payouts, so the subset cardinality is
    small.

Meet-in-the-middle exploits both: enumerate bounded-cardinality subset sums for
each half of the pool, then join them with a binary search. That turns 2^n into
roughly 2^(n/2), and the cardinality cap prunes each half far below even that.

The property that matters most for correctness is **not stopping at the first
solution**. If two different sets of settlements both sum to the credit, the
match is genuinely ambiguous and reporting either one as fact would be a
fabrication. This module enumerates up to a cap and hands every solution back
so the caller can refuse to choose.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from math import comb
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class Item:
    """One candidate component of a batch."""

    ref: str
    amount: int


@dataclass(frozen=True, slots=True)
class Solution:
    """A set of items whose sum lands within tolerance of the target."""

    refs: tuple[str, ...]
    total: int
    residual: int          # total - target; signed, within tolerance
    cardinality: int

    @property
    def is_exact(self) -> bool:
        return self.residual == 0


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """The result of a decomposition attempt, including why it stopped.

    `truncated` is set when the enumeration hit its solution cap, meaning the
    absence of further solutions is unproven. A caller must not treat a
    truncated single-solution result as unique.

    `expected_spurious` is the number of combinations that would be expected to
    land on the target *by chance* given the size of the pool. It is the field
    that decides whether a solution is evidence at all -- see `is_credible`.
    """

    solutions: tuple[Solution, ...]
    pool_size: int
    truncated: bool
    skipped_reason: str = ""
    expected_spurious: float = 0.0

    @property
    def is_unique(self) -> bool:
        return len(self.solutions) == 1 and not self.truncated

    @property
    def is_credible(self) -> bool:
        """Whether finding this solution is surprising enough to mean anything.

        A pool of two hundred payouts contains more six-element combinations
        than there are distinct sums they could make, so *some* combination
        lands on almost any target. Uniqueness within the enumeration cap does
        not rescue that: the solution is an artefact of pool size, not
        evidence about what happened.

        The threshold is deliberately strict. Below 0.05 expected chance hits,
        a match is roughly twenty-to-one against being coincidence; above it,
        the decomposition is refused and the credit goes to a human instead.
        """
        return self.expected_spurious < 0.05


def _half_sums(
    items: Sequence[Item], max_cardinality: int
) -> list[tuple[int, int, int]]:
    """All (sum, bitmask, cardinality) for subsets of `items` up to a size cap.

    Built iteratively so that the cardinality bound prunes during construction
    rather than after, which is where the saving is.
    """
    results: list[tuple[int, int, int]] = [(0, 0, 0)]
    for index, item in enumerate(items):
        bit = 1 << index
        additions = [
            (total + item.amount, mask | bit, count + 1)
            for total, mask, count in results
            if count < max_cardinality
        ]
        results.extend(additions)
    return results


#: How wide a neighbourhood the credibility probe examines, in paise (Rs 20).
#: Wide enough that a crowded region reveals itself, narrow enough that the
#: density it measures is still local to the target.
PROBE_WINDOW: int = 2_000

#: Solutions to enumerate during the probe before concluding "at least this
#: dense". Hitting it yields a lower bound rather than an infinity, which is
#: both true and more useful.
PROBE_CAP: int = 120

#: How many combinations the search may consider before a hit stops being
#: surprising. Calibrated empirically rather than chosen: swept against tens of
#: thousands of labelled records, 100,000 admitted coincidental decompositions
#: on busy days and 20,000 still admitted pairs drawn from very large pools.
#: 5,000 eliminated them across every seed tested, at a cost of a few points of
#: recall on high-volume days -- the right side of that trade for a system whose
#: headline promise is that it does not invent matches.
COMBINATION_BUDGET: int = 5_000


def credible_cardinality(
    pool_size: int, max_cardinality: int, budget: int = COMBINATION_BUDGET
) -> int:
    """The largest subset size still worth trusting from a pool this big.

    Spurious decompositions scale with the number of combinations available,
    `C(n, k)`, which grows explosively in k. Two payouts out of fifty is 1,225
    combinations and a hit means something; six out of fifty is 15.8 million
    and a hit means nothing at all.

    So rather than fixing one cardinality and hoping the pool stays small, the
    cardinality is derived from the pool: whatever k keeps the search space
    under the budget. A quiet day allows six-way batches to be decomposed; a
    busy one allows only pairs, and the rest go to a human.

    This costs recall on high-volume periods, and that is the correct trade.
    The alternative is a confident decomposition that happens to be arithmetic
    coincidence, which is precisely the failure this system exists to avoid.
    """
    limit = 0
    for k in range(1, max_cardinality + 1):
        if k > pool_size or comb(pool_size, k) > budget:
            break
        limit = k
    return limit


def measure_chance_hits(
    items: Sequence[Item],
    target: int,
    *,
    tolerance: int,
    max_cardinality: int,
    probe_window: int = PROBE_WINDOW,
    node_budget: int = 200_000,
) -> float:
    """Estimate how many combinations would hit this target by chance.

    Rather than assume a distribution, this measures the real one. It re-runs
    the search over a much wider window around the same target and counts what
    lands there. If widening the tolerance from a few paise to fifty rupees
    turns up thirty combinations, the region is crowded and an exact hit says
    nothing. If it turns up only the exact one, that hit is genuinely rare and
    the decomposition is worth trusting.

    Scaling the count down by the ratio of the two windows converts it to the
    expected number of hits at the real tolerance.

    Measuring beats modelling here because subset sums of real payout amounts
    are neither uniform nor normal -- they are heavily skewed and clumpy, and
    an analytic estimate over them is wrong by an order of magnitude in either
    direction depending on where the target falls. The probe costs one extra
    bounded search and is right about the region that actually matters.
    """
    if not items or target <= 0:
        return 0.0

    probe = find_subsets(
        items, target,
        tolerance=probe_window,
        max_cardinality=max_cardinality,
        max_solutions=PROBE_CAP,
        node_budget=node_budget,
        _probe=True,
    )
    found = len(probe.solutions)
    if found == 0:
        return 0.0

    # Hitting the cap means the true count is unknown but at least this large.
    # Reporting the lower bound is both true and more useful than an infinity:
    # it still fails any sane threshold, while remaining a real measurement.
    return found * (2 * tolerance + 1) / (2 * probe_window + 1)


def _branch_and_bound(
    pool: Sequence[Item],
    target: int,
    tolerance: int,
    max_cardinality: int,
    max_solutions: int,
    node_budget: int,
) -> tuple[list[Solution], bool]:
    """Exact enumeration by depth-first search with two pruning bounds.

    Used when the pool is too large for meet-in-the-middle to be worth the
    memory. `pool` must already be sorted by descending amount, which is what
    makes both bounds valid:

      * **Overshoot.** An item bigger than what remains cannot be included, but
        later items are smaller, so the scan continues rather than stopping.
      * **Unreachability.** The largest sum still obtainable from position `i`
        is the next `slots` items. If even that falls short of what remains,
        no position further right can do better, so the branch is abandoned.

    The second bound is what makes this tractable: on real settlement data it
    eliminates the vast majority of the search space, because payout amounts
    vary over orders of magnitude and most partial sums are hopeless early.

    `node_budget` caps the work honestly. Exhausting it returns `truncated`,
    which the caller must treat as "uniqueness unproven" rather than as a
    completed search.
    """
    count = len(pool)
    # prefix[i] = total of pool[:i], so the largest reachable sum from index i
    # using k items is prefix[i + k] - prefix[i].
    prefix = [0] * (count + 1)
    for index, item in enumerate(pool):
        prefix[index + 1] = prefix[index] + item.amount

    solutions: list[Solution] = []
    nodes = 0
    truncated = False

    def descend(start: int, remaining: int, chosen: list[int]) -> None:
        nonlocal nodes, truncated
        if truncated or len(solutions) > max_solutions:
            return

        if chosen and abs(remaining) <= tolerance:
            solutions.append(
                Solution(
                    refs=tuple(sorted(pool[i].ref for i in chosen)),
                    total=sum(pool[i].amount for i in chosen),
                    residual=-remaining,
                    cardinality=len(chosen),
                )
            )
            # Every remaining item is positive, so extending this set can only
            # overshoot. Nothing further down this branch is a solution.
            return

        if len(chosen) >= max_cardinality:
            return

        slots = max_cardinality - len(chosen)
        for index in range(start, count):
            nodes += 1
            if nodes > node_budget:
                truncated = True
                return
            amount = pool[index].amount
            if amount > remaining + tolerance:
                continue  # too big here, but later items are smaller
            reachable = prefix[min(index + slots, count)] - prefix[index]
            if reachable < remaining - tolerance:
                return  # even the best case from here falls short
            chosen.append(index)
            descend(index + 1, remaining - amount, chosen)
            chosen.pop()
            if truncated or len(solutions) > max_solutions:
                return

    descend(0, target, [])
    return solutions, truncated


def find_subsets(
    items: Sequence[Item],
    target: int,
    *,
    tolerance: int = 0,
    max_cardinality: int = 6,
    max_solutions: int = 8,
    max_pool: int = 34,
    node_budget: int = 400_000,
    _probe: bool = False,
) -> SearchOutcome:
    """Find sets of items summing to `target` within `tolerance`.

    `max_cardinality` reflects how many payouts a real batch combines; raising
    it costs enumeration time and, more importantly, admits spurious solutions,
    because with enough items almost any target becomes reachable.

    Two exact strategies, chosen by pool size. Meet-in-the-middle is faster on
    small pools; branch-and-bound uses no meaningful memory and copes with the
    large ones a busy settlement day produces. Both enumerate rather than stop
    at the first hit, so ambiguity is still detected either way.
    """
    if target == 0:
        return SearchOutcome((), 0, False, "target is zero")

    # Items larger than the target plus tolerance can never participate: every
    # amount here is positive, so including one already overshoots.
    pool = [item for item in items if 0 < item.amount <= target + tolerance]
    if not pool:
        return SearchOutcome((), 0, False, "no candidate fits under the target")

    # Deterministic ordering: descending amount makes the halves better
    # balanced, drives the branch-and-bound pruning, and keeps runs identical.
    pool.sort(key=lambda i: (-i.amount, i.ref))

    if len(pool) > max_pool:
        solutions, truncated = _branch_and_bound(
            pool, target, tolerance, max_cardinality, max_solutions, node_budget
        )
        return _finish(
            solutions, pool, target, tolerance, max_cardinality, max_solutions,
            truncated or len(solutions) > max_solutions, _probe,
        )

    split = len(pool) // 2
    left, right = pool[:split], pool[split:]

    left_sums = _half_sums(left, max_cardinality)
    right_sums = _half_sums(right, max_cardinality)
    right_sums.sort(key=lambda entry: entry[0])
    right_totals = [entry[0] for entry in right_sums]

    solutions: list[Solution] = []
    seen_masks: set[tuple[int, int]] = set()
    truncated = False

    for left_total, left_mask, left_count in left_sums:
        if left_count > max_cardinality:
            continue
        low = target - tolerance - left_total
        high = target + tolerance - left_total
        if high < 0:
            continue  # this half already overshoots

        start = bisect_left(right_totals, low)
        end = bisect_right(right_totals, high)
        for position in range(start, end):
            right_total, right_mask, right_count = right_sums[position]
            if left_count + right_count == 0:
                continue  # the empty set is not a decomposition
            if left_count + right_count > max_cardinality:
                continue

            key = (left_mask, right_mask)
            if key in seen_masks:
                continue
            seen_masks.add(key)

            refs = tuple(
                sorted(
                    [item.ref for index, item in enumerate(left) if left_mask >> index & 1]
                    + [item.ref for index, item in enumerate(right) if right_mask >> index & 1]
                )
            )
            total = left_total + right_total
            solutions.append(
                Solution(
                    refs=refs,
                    total=total,
                    residual=total - target,
                    cardinality=left_count + right_count,
                )
            )
            if len(solutions) > max_solutions:
                truncated = True
                break
        if truncated:
            break

    # Prefer exact over near, then fewer components, then a stable ref order.
    # Fewer components is the right tiebreak: a two-payout batch explaining a
    # credit is far more likely than a five-payout coincidence hitting the same
    # total, and preferring it makes the ranking match reality.
    return _finish(
        solutions, pool, target, tolerance, max_cardinality, max_solutions,
        truncated, _probe,
    )


def _finish(
    solutions: list[Solution],
    pool: Sequence[Item],
    target: int,
    tolerance: int,
    max_cardinality: int,
    max_solutions: int,
    truncated: bool,
    is_probe: bool,
) -> SearchOutcome:
    """Assemble the outcome, measuring credibility unless this is the probe."""
    solutions.sort(key=lambda s: (abs(s.residual), s.cardinality, s.refs))
    kept = tuple(solutions[:max_solutions])

    expected = 0.0
    if kept and not is_probe:
        expected = measure_chance_hits(
            pool, target, tolerance=tolerance, max_cardinality=max_cardinality
        )

    return SearchOutcome(
        solutions=kept,
        pool_size=len(pool),
        truncated=truncated,
        expected_spurious=expected,
    )


def closest_subset(
    items: Sequence[Item],
    target: int,
    *,
    max_cardinality: int = 6,
    max_pool: int = 28,
) -> Solution | None:
    """The nearest reachable sum when no subset lands within tolerance.

    Used purely to make an exception informative: telling an operator "the
    closest combination is short by Rs 118.00, and here it is" turns a dead end
    into a lead. It never produces a match.
    """
    pool = [item for item in items if item.amount > 0]
    if not pool:
        return None
    pool.sort(key=lambda i: (-i.amount, i.ref))
    pool = pool[:max_pool]

    split = len(pool) // 2
    left, right = pool[:split], pool[split:]
    left_sums = _half_sums(left, max_cardinality)
    right_sums = sorted(_half_sums(right, max_cardinality), key=lambda e: e[0])
    right_totals = [entry[0] for entry in right_sums]

    best: Solution | None = None
    for left_total, left_mask, left_count in left_sums:
        wanted = target - left_total
        position = bisect_left(right_totals, wanted)
        # Inspect the two neighbours bracketing the ideal complement.
        for probe in (position - 1, position):
            if not 0 <= probe < len(right_sums):
                continue
            right_total, right_mask, right_count = right_sums[probe]
            if left_count + right_count == 0:
                continue
            total = left_total + right_total
            residual = total - target
            if best is None or abs(residual) < abs(best.residual):
                refs = tuple(
                    sorted(
                        [i.ref for k, i in enumerate(left) if left_mask >> k & 1]
                        + [i.ref for k, i in enumerate(right) if right_mask >> k & 1]
                    )
                )
                best = Solution(refs, total, residual, left_count + right_count)
    return best
