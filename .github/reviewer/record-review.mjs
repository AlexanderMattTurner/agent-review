// Turn the plugin read's structured output (findings.json) into the review.json that
// post-pr-review.mjs posts, and distil the read's cost into cost.json. Both files
// travel to the post job; the execution log never does, because only the redacting
// log upload may publish an agent transcript.
//
// The SEVERITY is decided here, never by the model. The vendored code-review prompt
// returns a category and a 0-100 confidence per finding; the table below maps the
// pair onto the severities config/review-severities.json gates on. A finding under
// the plugin's own 80 cut is dropped again here, so a stray low-confidence entry
// cannot open a thread that holds the merge.
//
// Env: PR_INPUT_DIR (holds findings.json; review.json and cost.json are written
// here), EXECUTION_FILE (the winning attempt's log; empty writes a cost-less record).
import { readFileSync, writeFileSync } from "node:fs";
import { isMain } from "./lib-cli-args.mjs";
import { readRunCost } from "./lib-review-cost.mjs";

export const MIN_CONFIDENCE = 80;
export const STRONG_CONFIDENCE = 90;

/**
 * The severity one finding posts at, or null when it is dropped.
 * @param {string} category bug | security | claude-md
 * @param {number} confidence 0-100
 * @returns {"blocking"|"warning"|"nit"|null}
 */
export function severityOf(category, confidence) {
  if (!Number.isInteger(confidence) || confidence < MIN_CONFIDENCE) return null;
  const strong = confidence >= STRONG_CONFIDENCE;
  if (category === "bug" || category === "security") {
    return strong ? "blocking" : "warning";
  }
  if (category === "claude-md") return strong ? "warning" : "nit";
  throw new Error(`unknown finding category: ${JSON.stringify(category)}`);
}

/**
 * The review.json post-pr-review.mjs reads, from the plugin's structured output.
 * @param {{summary?: string, diagram?: string, findings?: any[]}} output
 */
export function toReview(output) {
  const findings = [];
  for (const f of output.findings ?? []) {
    const severity = severityOf(f.category, f.confidence);
    if (!severity) continue;
    findings.push({
      path: f.path,
      line: f.line,
      side: f.side,
      ...(f.start_line ? { start_line: f.start_line } : {}),
      severity,
      title: f.title,
      body: `${f.body}\n\n<sub>${f.category} · confidence ${f.confidence}</sub>`,
      ...(f.suggestion ? { suggestion: f.suggestion } : {}),
    });
  }
  return {
    summary: output.summary ?? "",
    ...(output.diagram ? { diagram: output.diagram } : {}),
    findings,
  };
}

if (isMain(import.meta.url)) {
  const dir = process.env.PR_INPUT_DIR;
  if (!dir) throw new Error("PR_INPUT_DIR required");
  // A missing findings.json throws: the read produced no verdict, and posting an
  // empty review would record a read that never happened.
  const output = JSON.parse(readFileSync(`${dir}/findings.json`, "utf8"));
  const review = toReview(output);
  writeFileSync(`${dir}/review.json`, JSON.stringify(review, null, 2));
  const { cost, model } = readRunCost(process.env.EXECUTION_FILE || "");
  writeFileSync(
    `${dir}/cost.json`,
    JSON.stringify({ type: "result", total_cost_usd: cost, model }),
  );
  process.stderr.write(
    `kept ${review.findings.length} of ${(output.findings ?? []).length} finding(s); cost ${cost ?? "(unknown)"}\n`,
  );
}
