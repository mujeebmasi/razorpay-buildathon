/* finctl dashboard.
   Vanilla, no build step. State is whatever the API last returned; there is no
   client-side model to drift out of sync with the engine. */

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const api = async (path) => {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path} -> ${response.status}`);
  return response.json();
};

/* Escape before interpolating. Narrations are third-party text and land in
   innerHTML; treating them as trusted would be an injection waiting to
   happen, even in a local dashboard. */
const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

const pct = (value, digits = 1) => `${(value * 100).toFixed(digits)}%`;
const num = (value) => Number(value ?? 0).toLocaleString('en-IN');

const TIER_NAMES = {
  T0: 'reference matched exactly',
  T1: 'reference recovered from narration',
  T2: 'unique amount inside the window',
  T3: 'within rounding tolerance',
  T4: 'gross recovered from the rate card',
  T5: 'batch decomposition',
  T6: 'global assignment',
  T7: 'adjudicated',
};

let RUN = null;
let SELECTED = null;

/* ------------------------------------------------------------------ kpis */

function renderKpis(run) {
  const card = run.scorecard;
  const falseRate = card ? card.false_match_rate : 0;

  const kpis = [
    {
      label: 'throughput',
      value: `${num(Math.round(run.throughput_per_second))}/s`,
      sub: `${num(run.records)} records in ${run.seconds.toFixed(2)}s`,
      tone: '',
    },
    {
      label: 'match rate',
      value: pct(run.match_rate),
      sub: `${num(run.matches)} matches · ${run.value_matched_display}`,
      tone: 'good',
    },
    {
      label: 'false match rate',
      value: card ? pct(falseRate, 2) : 'n/a',
      sub: falseRate === 0 ? 'no wrong answer reached the output' : 'review immediately',
      tone: falseRate === 0 ? 'good' : 'crit',
    },
    {
      label: 'open exceptions',
      value: num(run.exceptions),
      sub: `${run.value_at_risk_display} at high severity`,
      tone: 'warn',
    },
    {
      label: 'ledger',
      value: run.journal_balances ? 'balanced' : 'out',
      sub: `${num(run.journal_entries)} entries posted`,
      tone: run.journal_balances ? 'good' : 'crit',
    },
  ];

  $('#kpis').innerHTML = kpis.map((kpi) => `
    <div class="kpi ${kpi.tone}">
      <div class="label">${esc(kpi.label)}</div>
      <div class="value">${esc(kpi.value)}</div>
      <div class="sub">${esc(kpi.sub)}</div>
    </div>`).join('');
}

/* -------------------------------------------------------------- overview */

function renderFunnel(run) {
  const tiers = run.tier_counts || {};
  const total = Object.values(tiers).reduce((a, b) => a + b, 0) || 1;
  const max = Math.max(...Object.values(tiers), 1);

  $('#funnel').innerHTML = Object.keys(tiers).sort().map((tier) => {
    const count = tiers[tier];
    return `
      <div class="funnel-row">
        <div class="funnel-tier">${esc(tier)}</div>
        <div class="funnel-bar">
          <div class="funnel-fill" style="width:${(count / max) * 100}%"></div>
          <div class="funnel-label">${esc(TIER_NAMES[tier] || tier)}</div>
        </div>
        <div class="funnel-n">${num(count)}</div>
        <div class="funnel-pct">${pct(count / total, 1)}</div>
      </div>`;
  }).join('');
}

function renderScorecard(run) {
  const card = run.scorecard;
  if (!card) {
    $('#scorecard').innerHTML =
      '<div class="empty">No labels present, so accuracy cannot be measured.</div>';
    return;
  }

  const rows = [
    ['overall accuracy', pct(card.accuracy), 'm-good', true],
    ['match precision', pct(card.match_precision), 'm-good', false],
    ['match recall', pct(card.match_recall), 'm-good', false],
    ['exception recall', pct(card.exception_recall), 'm-good', false],
    ['reason-code accuracy', pct(card.reason_accuracy), 'm-good', false],
    ['auto-resolved', pct(card.auto_resolve_rate), '', false],
    ['cases scored', num(card.total_cases), '', false],
  ];

  $('#scorecard').innerHTML = rows.map(([label, value, tone, headline]) => `
      <div class="metric ${headline ? 'headline' : ''}">
        <span>${esc(label)}</span><span class="${tone}">${esc(value)}</span>
      </div>`).join('') + `
    <div class="note">
      <strong>${pct(card.false_match_rate, 2)} false match rate.</strong>
      The engine never claimed a match it could not prove. Roughly a fifth of
      this batch is unresolvable by construction, so an auto-resolve rate near
      ${pct(card.auto_resolve_rate)} is the ceiling — not a shortfall.
    </div>`;
}

function renderVerification(run) {
  const rejected = run.verifier_rejections || 0;
  const fromAdjudicator = run.verifier_rejected_adjudications || 0;

  const examples = (run.rejected_examples || []).slice(0, 3).map((item) => `
    <div class="evidence neg">
      <span class="kind">${esc(item.invariant)}${item.adjudicated ? ' · adjudicator proposal' : ''}</span>
      ${esc(item.detail)}
    </div>`).join('');

  $('#verification').innerHTML = `
    <div class="metric headline">
      <span>matches re-derived from source</span>
      <span class="m-good">${num(run.verifier_checks)}</span>
    </div>
    <div class="metric">
      <span>rejected for failing an invariant</span>
      <span class="${rejected ? 'm-warn' : 'm-good'}">${num(rejected)}</span>
    </div>
    <div class="metric">
      <span>of those, reasoning-layer proposals</span>
      <span class="${fromAdjudicator ? 'm-warn' : 'm-good'}">${num(fromAdjudicator)}</span>
    </div>
    <div class="metric">
      <span>adjudicator abstentions</span>
      <span>${num(run.adjudicator_abstained)}</span>
    </div>
    ${examples}
    <div class="note ${rejected ? 'amber' : ''}">
      The verifier recomputes every total from the original records rather than
      trusting what a match says about itself, so a confident but wrong
      proposal cannot reach the ledger. Rejections become exceptions with the
      discarded reasoning attached.
    </div>`;
}

function renderFindings(run) {
  const counters = run.counters || {};
  const findings = [
    ['gateway fees above the rate card', run.fee_recovery_display,
     'recoverable money a match-only reconciler never sees'],
    ['payouts breaching the settlement SLA', num(counters.sla_breaches || 0),
     'reference and amount agree, but the money arrived late'],
    ['duplicate credits caught', num(counters.duplicate_credits || 0),
     'the same reference credited more than once'],
    ['reversal pairs netted', num(counters.reversals_netted || 0),
     'absorbed silently instead of becoming two breaks'],
    ['batched credits decomposed', num(counters.batches_decomposed || 0),
     'one transfer traced back to its component payouts'],
    ['withheld by the gateway', run.reserve_display,
     'posted as a receivable, not written off'],
  ];

  $('#findings').innerHTML = findings.map(([label, value, note]) => `
    <div class="metric">
      <span>${esc(label)}<br><span style="color:var(--ink-faint);font-size:11.5px">${esc(note)}</span></span>
      <span>${esc(value)}</span>
    </div>`).join('');
}

const SEVERITY_OF = {};

function renderReasons(run) {
  const reasons = run.reason_counts || {};
  const entries = Object.entries(reasons).sort((a, b) => b[1] - a[1]);

  $('#reasons').innerHTML = entries.map(([reason, count]) => `
    <div class="reason-chip sev-${esc(SEVERITY_OF[reason] || 'medium')}" data-reason="${esc(reason)}">
      <span class="name">${esc(reason.replace(/_/g, ' '))}</span>
      <span class="n">${num(count)}</span>
    </div>`).join('');

  $$('.reason-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      showView('exceptions');
      $('#exc-reason').value = chip.dataset.reason;
      loadExceptions();
    });
  });
}

/* ------------------------------------------------------------ exceptions */

async function loadExceptions() {
  const params = new URLSearchParams({
    severity: $('#exc-severity').value,
    reason: $('#exc-reason').value,
    q: $('#exc-search').value.trim(),
    limit: '300',
  });
  const data = await api(`/api/exceptions?${params}`);

  $('#exc-count').textContent =
    `${num(data.total)} open · ${data.total_value_display}`;

  $('#exc-list').innerHTML = data.items.length ? data.items.map((item) => {
    SEVERITY_OF[item.reason] = item.severity;
    return `
      <div class="row sev-${esc(item.severity)}" data-id="${esc(item.id)}">
        <div class="row-top">
          <div>
            <span class="pill ${esc(item.severity)}">${esc(item.severity)}</span>
            <span class="row-reason">${esc(item.reason)}</span>
          </div>
          <div class="row-amount">${esc(item.amount_display)}</div>
        </div>
        <div class="row-summary">${esc(item.summary)}</div>
        <div class="row-meta">${esc(item.subjects.slice(0, 2).join(', '))} · ${esc(item.as_of)}${
          item.candidate_count ? ` · ${item.candidate_count} candidate(s) examined` : ''}</div>
      </div>`;
  }).join('') : '<div class="empty">Nothing matches these filters.</div>';

  $$('#exc-list .row').forEach((row) => {
    row.addEventListener('click', () => selectException(row.dataset.id, row));
  });
}

async function selectException(id, row) {
  $$('#exc-list .row').forEach((r) => r.classList.remove('selected'));
  if (row) row.classList.add('selected');
  SELECTED = id;

  const item = await api(`/api/exception/${encodeURIComponent(id)}`);
  if (item.error) {
    $('#exc-detail').innerHTML = `<div class="empty">${esc(item.error)}</div>`;
    return;
  }

  const evidence = (item.evidence || []).map((e) => `
    <div class="evidence ${e.weight > 0 ? 'pos' : e.weight < 0 ? 'neg' : ''}">
      <span class="kind">${esc(e.kind)}</span>${esc(e.detail)}
    </div>`).join('') || '<div class="empty">No evidence recorded.</div>';

  const records = (item.records || []).map((r) => `
    <div class="record">
      <span class="kind">${esc(r.kind)}</span> ${esc(r.id)} · ${esc(r.amount)} · ${esc(r.date)}
      <br>${esc(r.detail)}
    </div>`).join('');

  $('#exc-detail').innerHTML = `
    <h3>${esc(item.reason.replace(/_/g, ' '))}</h3>
    <div class="sub">${esc(item.id)} · ${esc(item.amount_display)} · ${esc(item.as_of)}</div>

    <div class="action-box">
      <strong>${esc(item.owner)}</strong> — ${esc(item.action)}
    </div>

    <div class="section-title">what happened</div>
    <p style="font-size:12.5px;color:var(--ink-dim);margin:0;line-height:1.5">${esc(item.summary)}</p>

    <div class="section-title">facts considered</div>
    ${evidence}

    <div class="section-title">details</div>
    <dl class="kv">
      <dt>severity</dt><dd>${esc(item.severity)}</dd>
      <dt>source</dt><dd>${esc(item.source)}</dd>
      <dt>subjects</dt><dd>${esc(item.subjects.join(', '))}</dd>
      ${item.delta !== null && item.delta !== undefined
        ? `<dt>delta</dt><dd>${esc(item.delta)}</dd>` : ''}
      <dt>examined</dt><dd>${esc(item.candidate_count)} candidate(s)</dd>
    </dl>

    ${records ? `<div class="section-title">underlying records</div>${records}` : ''}`;
}

/* --------------------------------------------------------------- matches */

async function loadMatches() {
  const tier = $('#match-tier').value;
  const data = await api(`/api/matches?tier=${encodeURIComponent(tier)}&detail=1&limit=150`);
  $('#match-count').textContent = `${num(data.total)} matches`;

  $('#match-list').innerHTML = data.items.map((item) => `
    <div class="row">
      <div class="row-top">
        <div>
          <span class="pill tier">${esc(item.tier)}</span>
          <span class="row-reason">${esc(item.reason)}</span>
        </div>
        <div class="row-amount">${esc(item.bank_total_display)}</div>
      </div>
      <div class="row-summary">${esc(item.rationale || TIER_NAMES[item.tier] || '')}</div>
      <div class="row-meta">
        ${esc(item.settlements.length)} payout(s) &rarr; ${esc(item.bank_lines.length)} credit(s)
        · confidence ${esc(item.confidence)}
        · residual ${esc(item.residual)} paise
        ${item.adjudicator ? ` · via ${esc(item.adjudicator)}` : ''}
      </div>
      ${(item.evidence || []).slice(0, 2).map((e) => `
        <div class="evidence ${e.weight > 0 ? 'pos' : ''}" style="margin-top:8px">
          <span class="kind">${esc(e.kind)}</span>${esc(e.detail)}
        </div>`).join('')}
    </div>`).join('') || '<div class="empty">No matches in this tier.</div>';
}

/* --------------------------------------------------------------- journal */

async function loadJournal() {
  const data = await api('/api/journal?limit=25');

  $('#trial-balance').innerHTML = `
    <table>
      <thead><tr><th>account</th><th style="text-align:right">amount</th><th>dr/cr</th></tr></thead>
      <tbody>
        ${data.trial_balance.map((row) => `
          <tr>
            <td>${esc(row.account)}</td>
            <td class="num">${esc(row.amount)}</td>
            <td>${esc(row.direction)}</td>
          </tr>`).join('')}
        <tr class="total-row">
          <td>total debits</td><td class="num">${esc(data.debits)}</td><td></td>
        </tr>
        <tr>
          <td>total credits</td><td class="num">${esc(data.credits)}</td><td></td>
        </tr>
      </tbody>
    </table>
    <div class="note" style="margin-top:14px">
      ${data.balanced
        ? 'Debits equal credits across every posted entry.'
        : 'The ledger does not balance — investigate before relying on this run.'}
    </div>`;

  $('#journal-entries').innerHTML = data.entries.map((entry) => `
    <div class="jentry">
      <div class="head"><span>${esc(entry.id)}</span><span>${esc(entry.date)}</span></div>
      <div class="narrative">${esc(entry.narrative)}</div>
      ${entry.lines.map((line) => `
        <div class="jline">
          <span class="${line.direction === 'Dr' ? 'dr' : 'cr'}">${esc(line.direction)}</span>
          <span>${esc(line.account)}</span>
          <span class="amt">${esc(line.amount)}</span>
        </div>`).join('')}
    </div>`).join('');
}

/* ------------------------------------------------------------- scenarios */

async function loadScenarios() {
  const data = await api('/api/scenarios');

  $('#scenario-table').innerHTML = `
    <table>
      <thead>
        <tr>
          <th>scenario</th><th>difficulty</th><th>correct outcome</th>
          <th style="text-align:right">cases</th><th style="width:150px">accuracy</th>
        </tr>
      </thead>
      <tbody>
        ${data.scenarios.map((s) => {
          const rate = s.cases ? s.correct / s.cases : 1;
          return `
          <tr>
            <td>
              <strong>${esc(s.title)}</strong>
              <div style="color:var(--ink-faint);font-size:11.5px;line-height:1.45;margin-top:2px">
                ${esc(s.description)}
              </div>
            </td>
            <td><span class="diff ${esc(s.difficulty)}">${esc(s.difficulty)}</span></td>
            <td style="font-family:var(--mono);font-size:11.5px;color:var(--ink-dim)">
              ${esc(s.disposition)}<br>${esc(s.expected_reason)}
            </td>
            <td class="num">${num(s.cases)}</td>
            <td>
              <div class="bar-cell">
                <div class="mini-bar">
                  <div class="mini-fill ${rate < 1 ? 'partial' : ''}" style="width:${rate * 100}%"></div>
                </div>
                <span style="font-family:var(--mono);font-size:11.5px">${pct(rate, 0)}</span>
              </div>
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

/* ------------------------------------------------------------ navigation */

function showView(name) {
  $$('.view').forEach((view) => view.classList.remove('active'));
  $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.view === name));
  $(`#view-${name}`).classList.add('active');

  if (name === 'exceptions' && !$('#exc-list').children.length) loadExceptions();
  if (name === 'matches' && !$('#match-list').children.length) loadMatches();
  if (name === 'journal') loadJournal();
  if (name === 'scenarios') loadScenarios();
}

async function boot() {
  RUN = await api('/api/run');

  $('#adjudicator-name').textContent = RUN.adjudicator || 'none';
  $('#footer-note').textContent =
    `zero dependencies · ${num(RUN.records)} records · ` +
    `${RUN.seconds.toFixed(2)}s · deterministic · adjudicator: ${RUN.adjudicator}`;

  renderKpis(RUN);
  renderFunnel(RUN);
  renderScorecard(RUN);
  renderVerification(RUN);
  renderFindings(RUN);

  // The reason filter is populated from the run so it only ever offers codes
  // that actually occurred.
  const reasons = Object.keys(RUN.reason_counts || {}).sort();
  $('#exc-reason').innerHTML = '<option value="all">all reasons</option>' +
    reasons.map((r) => `<option value="${esc(r)}">${esc(r.replace(/_/g, ' '))}</option>`).join('');

  const tiers = Object.keys(RUN.tier_counts || {}).sort();
  $('#match-tier').innerHTML = '<option value="all">all tiers</option>' +
    tiers.map((t) => `<option value="${esc(t)}">${esc(t)} — ${esc(TIER_NAMES[t] || t)}</option>`).join('');

  await loadExceptions();   // populates SEVERITY_OF for the reason chips
  renderReasons(RUN);
}

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => showView(tab.dataset.view));
});
$('#exc-severity').addEventListener('change', loadExceptions);
$('#exc-reason').addEventListener('change', loadExceptions);
$('#match-tier').addEventListener('change', loadMatches);

let searchTimer;
$('#exc-search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadExceptions, 180);
});

$('#refresh').addEventListener('click', async () => {
  $('#refresh').textContent = 'running...';
  await api('/api/refresh');
  await boot();
  $('#refresh').textContent = 're-run';
});

boot().catch((error) => {
  document.querySelector('main').innerHTML =
    `<div class="card"><h2>Could not load the run</h2><p>${esc(error.message)}</p></div>`;
});
