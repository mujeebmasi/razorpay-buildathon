/**
 * The API contract, mirrored from the Python payload builders in
 * `server/app.py`.
 *
 * These are hand-written rather than generated, and the trade is deliberate:
 * a generator would need a schema layer on the Python side that exists only to
 * feed the frontend. Hand-written types stay honest as long as they are
 * changed alongside the endpoint, and `npm run typecheck` then catches every
 * consumer that assumed the old shape.
 */

export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info';

export type Tier = 'T0' | 'T1' | 'T2' | 'T3' | 'T4' | 'T5' | 'T6' | 'T7';

export type SourceKind = 'psp' | 'bank' | 'ledger';

/** One fact that supported or contradicted a decision. */
export interface Evidence {
  kind: string;
  detail: string;
  /** Signed: positive supports the conclusion, negative counts against it. */
  weight: number;
  records: string[];
}

export interface DailyPoint {
  date: string;
  matched: number;
  broken: number;
  matched_display: string;
  broken_display: string;
}

export interface ExposureRow {
  reason: string;
  count: number;
  amount: number;
  amount_display: string;
  severity: Severity;
}

export interface RejectedProposal {
  match: string;
  invariant: string;
  detail: string;
  adjudicated: boolean;
}

export interface Scorecard {
  total_cases: number;
  accuracy: number;
  auto_resolve_rate: number;
  match_precision: number;
  match_recall: number;
  match_f1: number;
  exception_recall: number;
  reason_accuracy: number;
  /** The number that matters most. Zero is the whole game. */
  false_match_rate: number;
  verdicts: Record<string, number>;
  by_scenario: Record<string, Record<string, number>>;
  by_difficulty: Record<string, Record<string, number>>;
}

/** Present only when a tool-using agent ran. */
export interface AgentUsage {
  requests: number
  prompt_tokens: number
  completion_tokens: number
  tool_calls: number
  seconds: number
  decided: number
  declined: number
  failed: number
  throttled: number
}

export interface RunSummary {
  records: number;
  seconds: number;
  throughput_per_second: number;
  settlements: number;
  bank_lines: number;
  payments: number;
  quarantined: number;
  matches: number;
  exceptions: number;
  match_rate: number;

  value_matched: number;
  value_at_risk: number;
  value_matched_display: string;
  value_at_risk_display: string;
  reserve_display: string;
  rounding_display: string;
  fee_recovery_display: string;
  journal_debits_display: string;
  journal_credits_display: string;

  tier_counts: Partial<Record<Tier, number>>;
  reason_counts: Record<string, number>;
  severity_counts: Partial<Record<Severity, number>>;
  stage_timings: Record<string, number>;
  counters: Record<string, number>;

  adjudicator: string;
  adjudicator_kind: string;
  adjudicated: number;
  adjudicator_abstained: number;

  verifier_checks: number;
  verifier_rejections: number;
  verifier_rejected_adjudications: number;
  violations: Record<string, number>;
  rejected_examples: RejectedProposal[];

  journal_entries: number;
  journal_balances: boolean;
  journal_debits: number;
  journal_credits: number;

  daily: DailyPoint[];
  exposure_by_reason: ExposureRow[];

  /** Absent when the batch ships without held-out labels. */
  scorecard?: Scorecard;

  /** Absent unless the run used `--adjudicator agent`. */
  agent?: AgentUsage;
}

export interface ExceptionSummary {
  id: string;
  reason: string;
  severity: Severity;
  source: SourceKind;
  subjects: string[];
  amount: number;
  amount_display: string;
  as_of: string;
  summary: string;
  owner: string;
  action: string;
  delta: number | null;
  candidate_count: number;
}

export interface RelatedRecord {
  kind: string;
  id: string;
  amount: string;
  date: string;
  detail: string;
}

export interface ExceptionDetail extends ExceptionSummary {
  candidates: string[];
  evidence: Evidence[];
  records: RelatedRecord[];
  /** The tools the agent called while investigating this one, in order. */
  agent_trace: string[];
}

export interface ExceptionPage {
  total: number;
  offset: number;
  returned: number;
  has_more: boolean;
  total_value: number;
  total_value_display: string;
  severity_counts: Record<Severity, number>;
  items: ExceptionSummary[];
}

export interface MatchRow {
  id: string;
  tier: Tier;
  reason: string;
  confidence: number;
  bank_lines: string[];
  settlements: string[];
  payment_count: number;
  bank_total: number;
  bank_total_display: string;
  residual: number;
  adjudicator: string | null;
  rationale?: string;
  evidence?: Evidence[];
}

export interface MatchPage {
  total: number;
  items: MatchRow[];
}

export interface TrialBalanceRow {
  account: string;
  amount: string;
  direction: 'Dr' | 'Cr';
  raw: number;
}

export interface JournalLine {
  account: string;
  direction: 'Dr' | 'Cr';
  amount: string;
  memo: string;
}

export interface JournalEntry {
  id: string;
  date: string;
  narrative: string;
  lines: JournalLine[];
}

export interface JournalPayload {
  balanced: boolean;
  debits: string;
  credits: string;
  entry_count: number;
  trial_balance: TrialBalanceRow[];
  entries: JournalEntry[];
}

export type Difficulty = 'trivial' | 'routine' | 'hard' | 'unresolvable';

export interface ScenarioRow {
  key: string;
  title: string;
  description: string;
  difficulty: Difficulty;
  disposition: 'match' | 'exception' | 'ignore';
  expected_reason: string;
  cases: number;
  correct: number;
  verdicts: Record<string, number>;
}

export interface ScenarioPayload {
  scenarios: ScenarioRow[];
}

export type ViewName =
  | 'overview'
  | 'exceptions'
  | 'matches'
  | 'journal'
  | 'scenarios';
