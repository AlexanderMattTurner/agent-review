// Shared cost accounting for the PR-review footnote — used by the reviewer
// (post-pr-review.mjs) and record-review.mjs for reading a Claude run's
// cost, formatting dollars, and the "PRs per Max 20x weekly allowance" line.
import { readFileSync } from "node:fs";

/**
 * What a caller may learn about one run's price; absent when unavailable.
 * @typedef {{cost?: number, model?: string}} RunCost
 */

/**
 * `total_cost_usd` and the model that ran, from the Claude action's execution log,
 * which is either an array of streamed events whose terminal `type: "result"`
 * carries the API-equivalent cost, or an object with the field directly — the
 * shape of the cost.json record-review.mjs distils from that log.
 * INVARIANT — a missing or unparsable log yields {}; a missing cost never breaks posting.
 * @param {string} [executionFile] the log path; falls back to the environment.
 * @returns {RunCost}
 */
export function readRunCost(executionFile) {
  const file =
    executionFile ||
    process.env.EXECUTION_FILE ||
    (process.env.RUNNER_TEMP
      ? `${process.env.RUNNER_TEMP}/claude-execution-output.json`
      : "");
  if (!file) return {};
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return {};
  }
  const events = Array.isArray(parsed) ? parsed : [parsed];
  let cost;
  let model;
  for (const ev of events) {
    if (ev && typeof ev === "object") {
      if (typeof ev.total_cost_usd === "number") cost = ev.total_cost_usd;
      if (model === undefined && typeof ev.model === "string") model = ev.model;
    }
  }
  return { cost, model };
}

/** Sub-cent costs keep four decimals; everything else two.
 * @param {number} cost @returns {string} */
export function formatDollars(cost) {
  return cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2);
}

// PROBLEM CLASS — the assumed Max 20x weekly allowance is stated by more than one
// reporter (this footnote, and METRICS.md's Claude-usage chart), so a
// literal here would let the two publish different denominators for one week.
// The number lives in config/claude-budget.json; both consumers read it.
const BUDGET_CONFIG = new URL(
  "../../config/claude-budget.json",
  import.meta.url,
);

// The assumed Max 20x weekly budget (override with MAX20X_WEEKLY_USD); 0 when
// unset/invalid so callers drop budget-relative text. `configUrl` exists so a
// test can point the read at a file it controls.
export function weeklyBudget(configUrl = BUDGET_CONFIG) {
  // The assumed Max 20x weekly API-equivalent budget; 0 when unset or invalid.
  const override = process.env.MAX20X_WEEKLY_USD;
  let raw;
  if (override) {
    raw = Number.parseFloat(override);
  } else {
    // An unreadable or malformed SSOT blanks one decorative footnote line, and must
    // never throw: `plansLine` calls this as a default argument and post-pr-review.mjs
    // calls `plansLine` on the posting path, so a throw here loses the whole review.
    try {
      raw = JSON.parse(readFileSync(configUrl, "utf8")).max20x_weekly_usd;
    } catch {
      return 0;
    }
  }
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

/**
 * The final footnote line: roughly how many PRs fit in a Max 20x weekly allowance
 * at this per-PR cost. `Number.isFinite` is not a type guard, so the two later
 * reads carry the cast its check already justifies: an undefined cost leaves by
 * the guard above them.
 * @param {number|undefined} totalCost
 * @param {number} [weekly] the weekly allowance in dollars.
 * @returns {string} the footnote, or "" when it cannot be estimated.
 */
export function plansLine(totalCost, weekly = weeklyBudget()) {
  if (
    !Number.isFinite(totalCost) ||
    /** @type {number} */ (totalCost) <= 0 ||
    !weekly
  )
    return "";
  const prs = Math.floor(weekly / /** @type {number} */ (totalCost));
  return `<sub>📉 ~${prs.toLocaleString("en-US")} PRs/week at this rate on a Max 20× plan.</sub>`;
}
