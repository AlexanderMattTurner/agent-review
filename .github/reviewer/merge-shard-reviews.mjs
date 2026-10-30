// Fold the review.json and cost.json every read leg uploaded into the ONE review.json and
// cost.json that post-pr-review.sh posts, so a sharded PR gets a single review rather than N.
// A diff small enough for one read has one leg, `whole`, and the fold hands its files through.
//
// The merge is deliberately dumb — concatenate findings, concatenate summaries — because every
// shard read a disjoint slice of the same diff, so there is nothing to reconcile. Anchoring is
// unaffected: post-pr-review.mjs resolves each finding against the FULL diff.txt, which every
// shard's findings are still coordinates into.
//
// INVARIANT — a fold never claims a read the legs did not deliver. When a leg left no review, the
// review says which shards went unread and how many lines that leaves, the cost is dropped (a
// partial sum renders exactly like a whole one), and INCOMPLETE_FILE tells post-pr-review.sh to
// post it with no read marker and no coverage stamp, under a hold a human resolves. A fold with no
// leg at all throws: there is nothing to post.
//
// Env: PR_INPUT_DIR (holds shards/manifest.json when the diff was sharded; review.json, cost.json
// and INCOMPLETE_FILE are written here), SHARD_REVIEW_DIR (one `<leg>` directory per uploaded
// leg: each artifact carries its own `<leg>/` and the download merges them).
// GITHUB_OUTPUT, when set, gets complete=true|false.
import {
  appendFileSync,
  existsSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from "node:fs";

import { isMain } from "./lib-cli-args.mjs";
import { readRunCost } from "./lib-review-cost.mjs";

export const WHOLE_LEG = "whole";
export const INCOMPLETE_FILE = "incomplete-read.txt";

/**
 * The legs this read planned, from the manifest the sharder wrote, or the one `whole` leg.
 * @param {string} dir PR_INPUT_DIR
 * @returns {{legs: {name: string, lines?: number}[], manifest: any}}
 */
function plannedLegs(dir) {
  const path = `${dir}/shards/manifest.json`;
  if (!existsSync(path)) return { legs: [{ name: WHOLE_LEG }], manifest: null };
  const manifest = JSON.parse(readFileSync(path, "utf8"));
  return { legs: manifest.shards, manifest };
}

/**
 * Each delivered leg's review and cost, keyed by leg name. A directory naming no planned leg
 * throws: it is an artifact this run did not plan, and folding it would review foreign bytes.
 * @param {string} legDir SHARD_REVIEW_DIR
 * @param {Set<string>} planned
 * @returns {Map<string, {review: any, cost: import("./lib-review-cost.mjs").RunCost}>}
 */
function deliveredLegs(legDir, planned) {
  const delivered = new Map();
  const entries = existsSync(legDir)
    ? readdirSync(legDir, { withFileTypes: true })
    : [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const { name } = entry;
    if (!planned.has(name)) {
      throw new Error(
        `unexpected leg directory — no planned leg is named ${JSON.stringify(name)}`,
      );
    }
    const reviewPath = `${legDir}/${name}/review.json`;
    if (!existsSync(reviewPath)) continue;
    delivered.set(name, {
      review: JSON.parse(readFileSync(reviewPath, "utf8")),
      cost: readRunCost(`${legDir}/${name}/cost.json`),
    });
  }
  return delivered;
}

/**
 * The whole read's price: the sum of every leg, or nothing when any leg is missing or unpriced.
 * One model name only when every leg agrees.
 * @param {{cost: import("./lib-review-cost.mjs").RunCost}[]} legs
 * @param {number} planned
 */
function totalCost(legs, planned) {
  if (legs.length !== planned) return { cost: null, model: null };
  if (!legs.every((l) => Number.isFinite(l.cost.cost)))
    return { cost: null, model: null };
  // Rounds off the binary-floating-point tail (raw sums render as 1.2000000000000002).
  const cost =
    Math.round(legs.reduce((sum, l) => sum + l.cost.cost, 0) * 1e6) / 1e6;
  const models = new Set(legs.map((l) => l.cost.model ?? ""));
  const [model] = models;
  return { cost, model: models.size === 1 && model ? model : null };
}

/**
 * Fold the legs into `${PR_INPUT_DIR}/review.json` and `cost.json`.
 * @param {{env?: NodeJS.ProcessEnv}} [deps]
 * @returns {{complete: boolean, delivered: number, planned: number}}
 */
export function main({ env = process.env } = {}) {
  const dir = env.PR_INPUT_DIR;
  if (!dir) throw new Error("PR_INPUT_DIR required");
  const legDir = env.SHARD_REVIEW_DIR;
  if (!legDir) throw new Error("SHARD_REVIEW_DIR required");

  const { legs, manifest } = plannedLegs(dir);
  const delivered = deliveredLegs(legDir, new Set(legs.map((l) => l.name)));
  if (delivered.size === 0) {
    throw new Error(
      `none of the ${legs.length} read leg(s) delivered a review — nothing to post`,
    );
  }
  const missing = legs.filter((l) => !delivered.has(l.name));
  const complete = missing.length === 0;
  const kept = legs.filter((l) => delivered.has(l.name));
  const parts = kept.map((l) => delivered.get(l.name));

  let review;
  if (!manifest) {
    review = parts[0].review;
  } else {
    // No verdict fold: the merge gate reads finding severities off the posted threads, and each
    // shard's summary already states its own call. Diagrams describe one slice each, so none rides.
    const findings = parts.flatMap((p) =>
      Array.isArray(p.review.findings) ? p.review.findings : [],
    );
    const summaries = parts
      .map((p) =>
        typeof p.review.summary === "string" ? p.review.summary.trim() : "",
      )
      .filter(Boolean);
    // Numbers, not adjectives: the reader can check this against the PR's own line count.
    const readLines = kept.reduce((sum, l) => sum + (l.lines ?? 0), 0);
    const coverage = complete
      ? `_Read ${manifest.total_lines} of ${manifest.total_lines} diff lines ` +
        `across ${manifest.total_files} files in ${legs.length} shards._`
      : `_This read is INCOMPLETE. ${missing.length} of ${legs.length} shards delivered no ` +
        `review (${missing.map((l) => l.name).join(", ")}), so it read about ${readLines} of ` +
        `${manifest.total_lines} diff lines. It does not count as a review of this head._`;
    review = { summary: [coverage, ...summaries].join("\n\n"), findings };
  }

  writeFileSync(`${dir}/review.json`, JSON.stringify(review));
  const { cost, model } = totalCost(parts, legs.length);
  writeFileSync(
    `${dir}/cost.json`,
    JSON.stringify({ type: "result", total_cost_usd: cost, model }),
  );
  if (!complete) {
    writeFileSync(
      `${dir}/${INCOMPLETE_FILE}`,
      `${missing.length} of ${legs.length} shards delivered no review: ` +
        `${missing.map((l) => `${l.name} (${(l.files ?? []).join(", ")})`).join("; ")}.\n`,
    );
  }
  if (env.GITHUB_OUTPUT) {
    appendFileSync(env.GITHUB_OUTPUT, `complete=${complete}\n`);
  }
  process.stderr.write(
    `folded ${delivered.size} of ${legs.length} leg(s): ${complete ? "complete" : "INCOMPLETE"}\n`,
  );
  return { complete, delivered: delivered.size, planned: legs.length };
}

if (isMain(import.meta.url)) main();
