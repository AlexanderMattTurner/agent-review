// Persist ONE shard's review cost next to its review.json, so the synthesis job
// can sum the shards and post a cost footer for the whole review.
//
// Only the number travels. The execution log itself stays on the shard runner
// and reaches the public repository solely through the fail-closed redacting
// upload-agent-logs action; copying it into the shard artifact would publish an
// unredacted agent transcript.
//
// Writes the file unconditionally, including when the cost is unreadable: the
// reader treats one shard-cost.json per review.json as its completeness test, so
// a missing file and a cost-less file must be distinguishable — the first means
// a shard never ran this step (drop the footer), the second means it ran and had
// nothing to report (also drop the footer, but loudly, via a null cost).
import { writeFileSync } from "node:fs";
import { readRunCost } from "./lib-review-cost.mjs";

const out = process.env.SHARD_COST_FILE;
if (!out) throw new Error("SHARD_COST_FILE required");

// A shard that escalated paid for BOTH reads, and the footer prices the review.
// The model it is credited to is the one whose findings post: the escalated read,
// unless it died and the workflow restored the cheap verdict.
const cheap = readRunCost();
const escalated = process.env.EXECUTION_FILE_ESCALATED
  ? readRunCost(process.env.EXECUTION_FILE_ESCALATED)
  : {};
const costs = [cheap.cost, escalated.cost].filter(Number.isFinite);
// No readable cost at all stays null — the reader drops the footer rather than
// posting a price that is missing one of the two reads it should name.
const cost = costs.length ? costs.reduce((a, b) => a + b, 0) : undefined;
const kept = process.env.ESCALATION_KEPT !== "false";
const model = (kept && escalated.model) || cheap.model;

writeFileSync(
  out,
  JSON.stringify({ cost: cost ?? null, model: model ?? null }),
);
process.stderr.write(`recorded shard cost ${cost ?? "(unknown)"} to ${out}\n`);
