# finctl — an AI finance controller that refuses to guess

Three-way settlement reconciliation: **PSP payout report ↔ bank statement ↔ internal ledger**.
Every bank credit must be fully explained as

```
Σ(gross payments) − fees − GST − refunds − chargebacks − adjustments
```

…or it becomes an exception with a reason code, an owner, and a next action.

Built for **Razorpay Track 04 — AI Finance Controller**.

---

## Quickstart

No dependencies. No install. Python 3.11+ and nothing else.

```bash
python -m datagen.generate --cases 900 --out data
```

```bash
python -m finctl recon --data data
```

```bash
python -m finctl serve --data data --port 8000
```

The third command opens a dashboard at `http://127.0.0.1:8000` — match funnel,
accuracy scorecard, the exception register with full evidence trails, the
journal, and the edge-case catalogue.

```bash
python -m unittest discover -s tests -t .
```

---

## What it actually does

Measured on the shipped 900-case batch (`--seed 20260828`):

| | |
|---|---|
| **Throughput** | 5,320 records in 0.60s — **~8,800 records/second**. ~1,800/s at 29,526 records (see note). |
| **Match rate** | 75.7% of payouts closed automatically, 96.3% overall accuracy |
| **Match precision** | **100.0%** |
| **Exception recall** | **100.0%** — every planted break was caught |
| **Reason-code accuracy** | **100.0%** — right record *and* right reason |
| **False match rate** | **0.000%** across 9 independent batches up to 30k records |
| **Ledger** | Balanced. 730 entries, debits = credits, every run. |

Accuracy is measured against **held-out labels the engine never sees**. The
generator writes `truth.json` alongside the CSVs; the loader's manifest
deliberately excludes it.

Throughput falls at volume — ~8,800/s at 5.3k records, ~1,800/s at 29.5k — and
the reason is worth stating plainly: as density rises, fewer payouts resolve
deterministically, so more of them reach the per-record reasoning stage. The
deterministic cascade itself stays near-linear; the residual is what grows. On
real data carrying payout references this residual would be a fraction of the
size, since the hardest scenario here deliberately strips them.

### The number that matters

Match rate is easy to game — match everything and it hits 100%. The metric this
project optimises is the **false match rate**: how often the system confidently
paired records that don't belong together, or claimed to resolve something the
data cannot resolve.

It is zero, and it stays zero when a deliberately reckless reasoning layer is
plugged in (see *The guardrail*).

### What it does not do

Roughly a fifth of the synthetic batch is **unresolvable by construction** —
two identical payouts with no reference, a chargeback for a payment outside the
period, a credit no settlement explains. An engine scoring 100% auto-resolution
on this data would be lying. The ceiling is ~81% and the engine reaches 76.9%.

**The match rate is 75.7%, and that is the honest number.** The gap is almost
entirely **multi-way batch decomposition**, refused on purpose — see
*Calibration*. Those refusals dominate the exception register: of 367 open
items, ~160 are payouts that are probably components of a batched credit the
engine declined to split. Each one is cross-referenced to the credit it likely
belongs to, so the operator sees one situation rather than five orphans.

An earlier build reported a 90.6% match rate on this data. It was also producing
false matches at volume. The lower number is the better system.

---

## Why it's built this way

> *"Verification capacity, not generation speed, is the bottleneck."*

Taken literally, that means the interesting engineering is not in producing
answers. It's in **being able to reject them**.

### 1. A cascade, strongest evidence first

Each pass sees only what the previous one could not explain.

| Tier | Mechanism | Share |
|---|---|---|
| T0 | Reference matched exactly in the narration | 68.4% |
| T1 | Reference recovered — truncated, transposed, buried in bank noise | 8.2% |
| T2 | Unique amount and date, checked from **both** directions | 12.2% |
| T3 | Within rounding tolerance (5 paise) | 4.7% |
| T5 | Batch decomposition — subset-sum over a credit's components | 6.6% |
| T7 | Adjudicated — the reasoning layer, on the residual only | 0% here: it abstained on all 177 it saw, which is the correct answer for them |

Ordering is a correctness property, not just performance. Running a weak matcher
first lets it claim records a strong one would have matched correctly — and those
false matches are *invisible*, because the match rate goes **up**.

### 2. Every pass may decline

Ambiguity is a result. Two settlements of identical value on the same day with
no reference is genuinely undecidable, and the system says so rather than
flipping a coin. A wrong match costs an operator more than no match: they have
to discover it first.

### 3. The guardrail

The adjudicator **proposes**. It does not decide.

A verifier re-derives every match from the original records — it recomputes
totals rather than trusting what a match says about itself — and can veto:

- the two sides must balance within tolerance
- no record may be claimed by two matches
- every referenced record must exist
- nothing may match without positive evidence naming specific records
- a payout must arrive as a credit, not a debit
- the credit must land near the payout date

A rejected proposal becomes an exception **with its reasoning attached**, so the
guardrail firing is visible rather than silent.

`tests/test_guardrails.py` includes `RecklessAdjudicator` — a reasoning layer
that confidently matches the first candidate it is offered, every time, at
maximum confidence, with a plausible-sounding rationale. The tests assert that
it makes proposals, that the verifier overrules them, that no unbalanced match
reaches the ledger, that the journal still balances, and that **the false match
rate stays at zero**.

That is the whole thesis, executable.

### 4. Findings a matcher alone cannot produce

Money that reconciles perfectly and is still wrong:

- **Gateway overcharges** — fees recomputed against the contracted rate card.
  ₹4,505.27 recoverable in the sample batch. An overcharged payment still
  settles and still reconciles; only independent recomputation finds it.
- **SLA breaches** — right reference, right amount, nine banking days late.
  Matching it silently would bury a contract issue.
- **Partial settlements** — the payout matches its credit exactly while the PSP
  quietly withheld part of it. Only comparing against component payments reveals
  the gap; it posts to a *Gateway Reserve Receivable*, not a write-off.
- **Duplicate credits**, **unrecorded revenue**, **unlinked chargebacks**.

---

## The edge-case catalogue

30 named scenarios, weighted to mirror a real merchant's mix — mostly clean,
with a long tail of the awkward. Six are unresolvable by design.

<details>
<summary><b>All 30 scenarios</b></summary>

**Identifier damage** — clean settlement · UTR buried in bank noise · narration
clipped mid-UTR at a 40-char field limit · transposed UTR characters · no
reference at all

**Arithmetic** — paise-level rounding drift · gross recovered by inverting the
rate card · fee charged above the contracted rate

**Batching** — 3–6 payouts netted into one credit · one payout split across two
credits

**Timing** — late but inside slack · T+2 spanning Diwali · captured 23:45 UTC
(next day in IST) · beyond the settlement SLA

**Deductions** — refund netted off · chargeback plus dispute fee · chargeback for
an out-of-period payment · refund for an out-of-period payment

**Breaks** — payout never arrived · credit from outside the PSP · undisclosed
deduction · two identical payouts with no reference · paise/rupee 100× slip ·
transposed digits · same UTR credited twice · unverifiable FX · collected but
not booked · partial settlement on hold · unparseable amount field

**Must not be raised** — credit reversed the same day (nets to zero)

</details>

The generator emits **four bank dialects** with different date formats, amount
formats and narration templates. The parser is never told which is which.

---

## Calibration: the one deliberate trade

Batch decomposition is subset-sum, and it has a failure mode that only appears
at volume. With 200 candidate payouts there are more 6-element combinations than
distinct sums they could make, so *some* combination lands on almost any target.
Uniqueness doesn't rescue it — the solution is an artefact of pool size, not
evidence.

Early builds hit **0.88% false matches at 15,000 records** from exactly this.

Two mechanisms fix it:

1. **Cardinality derived from pool size.** Spurious hits scale with `C(n,k)`.
   Two payouts out of fifty is 1,225 combinations and means something; six out of
   fifty is 15.8 million and means nothing. The permitted subset size is whatever
   keeps the search space under a calibrated budget.

2. **An empirical density probe.** Re-run the search over a much wider window and
   count what lands there. If widening the tolerance from 5 paise to ₹20 turns up
   thirty combinations, the region is crowded and an exact hit proves nothing.
   Measuring beats modelling — real payout sums are skewed and clumpy, and an
   analytic estimate is wrong by an order of magnitude either way.

The budget was **swept, not chosen**:

| Budget | Mean accuracy | False matches |
|---|---|---|
| 5,000 | 96.3% | **0** |
| 20,000 | 97.2% | 8 |
| 60,000 | 97.7% | 5 |
| 200,000 | 97.5% | 7 |

5,000 is the only setting with zero false matches. It costs ~1–3 points of
recall, concentrated entirely in multi-way batch decomposition — every other
scenario stays at 100%. For a system whose headline promise is that it does not
invent matches, that is the right side of the trade, and `test_pipeline.py`
pins the loss to that one scenario so it can't quietly spread.

---

## Architecture

```
datagen/          seeded generator + 30-scenario catalogue, writes held-out truth.json
finctl/
  money.py        integer paise only, never a float. 10 input formats.
  timeutil.py     IST, banking days, RBI holidays, T+N windows
  models.py       frozen records, reason-code taxonomy with owners and actions
  ingest/         alias-tolerant parsing; bad rows quarantined, never coerced
  engine/
    feemodel.py   rate card forward, verification, and net→gross inversion
    narration.py  UTR recovery: exact, truncated, Damerau-Levenshtein
    subsetsum.py  meet-in-the-middle + branch-and-bound, with credibility tests
    index.py      three-way candidate generation, keeps matching near-linear
    reconcile.py  the cascade
  adjudicate/     offline evidence reasoner · Claude adapter (stdlib HTTP)
  verify/         independent invariant checks with veto power
  post/journal.py double-entry, idempotent, balanced
  report/         scorecard: 8 verdicts, precision/recall, per-scenario slice
server/           dependency-free dashboard (http.server + vanilla JS)
tests/            88 tests
```

### On the adjudicator

Two providers behind one interface:

- **`local`** (default) — a transparent weighted-evidence reasoner. Not a
  language model and it doesn't pretend to be; the dashboard names exactly what
  ran. It requires a *margin*, not just a threshold: two candidates at 0.81 and
  0.79 mean the evidence doesn't distinguish them, so it abstains.
- **`claude`** — real Anthropic Messages API code on the same interface, active
  the moment `ANTHROPIC_API_KEY` is set. It never sees raw money (amounts
  pre-formatted, arithmetic pre-computed), can only return an id from the
  candidate set it was given, is parsed as strict JSON, and is still subject to
  the verifier. Built but not exercised in this submission — no key was
  available, and inventing benchmark numbers for it would defeat the point.

```bash
python -m finctl recon --data data --adjudicator claude
```

---

## Design decisions worth defending

**Money is `int` paise, everywhere.** One module touches decimal strings. Every
float bug in a reconciler is silent: it balances in testing and drifts in
production.

**A bad row is quarantined, never coerced.** Defaulting a corrupt amount to zero
produces a run that balances while being wrong — the most dangerous outcome a
reconciliation system can have. Quarantined rows are excluded from every total
and surface as their own exception class.

**Uniqueness is checked from both directions.** A payout having one candidate
credit isn't enough; if that credit also fits another payout equally well,
choosing either is a coin flip with a confidence score attached.

**Reason codes carry owners.** `scale_error_suspected` routes to Engineering
("audit the currency unit on the feed"). `unlinked_chargeback` routes to Risk
("deadline-sensitive for representment"). Naming the break is most of the work;
naming who fixes it is the rest.

**Determinism throughout.** Same seed, same input, byte-identical output —
including journal entry ids, so re-running cannot double-post. Accuracy figures
are reproducible and a regression is attributable to a code change rather than a
different roll of the dice.

---

## Honest limitations

- The rate card and holiday calendar are hardcoded for the demo period. A real
  deployment reads both from a feed.
- Batch decomposition is refused on high-volume days without a payout reference.
  Real Razorpay settlement data links payments to a `settlement_id`, which would
  make this trivial — the synthetic scenario strips that link deliberately to
  test the hard path.
- The Claude adjudicator is written and unit-tested for its parsing and
  rejection contract, but has not been run against the live API.
- Ledger matching is one-to-one on `order_id`. Partial invoice application and
  multi-invoice payments are not modelled.
- Single-process. The cascade parallelises cleanly by settlement date, but that
  was not needed at this volume.

---

*Zero dependencies · pure Python standard library · deterministic · 88 tests*
