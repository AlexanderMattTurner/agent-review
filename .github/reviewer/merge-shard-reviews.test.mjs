// Behavior tests for merge-shard-reviews.mjs: build a real PR_INPUT_DIR plus one
// `<leg>` directory per delivered leg (the merged-download layout), run the fold
// in-process, and assert the review.json, cost.json and incomplete marker it leaves for
// post-pr-review.sh.
//
// The load-bearing case is the missing leg: the posted review must say the read is
// incomplete, and must leave the marker that keeps it from counting as a read.
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { INCOMPLETE_FILE, main } from "./merge-shard-reviews.mjs";

const dirs = [];
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop(), { recursive: true, force: true });
});

const SHARDS = [
  { name: "shard-00.diff", lines: 3000, files: ["a.py", "b.py"] },
  { name: "shard-01.diff", lines: 1200, files: ["c.py"] },
];

/**
 * A PR_INPUT_DIR (with a manifest when `shards` is given) and one artifact
 * directory per entry of `legs`, each holding that leg's review.json and cost.json.
 * @param {Record<string, {review: object, cost: number|null, model?: string}>} legs
 * @param {{name: string, lines: number, files: string[]}[] | null} shards
 */
function setup(legs, shards) {
  const root = mkdtempSync(join(tmpdir(), "msr-"));
  dirs.push(root);
  const prInput = join(root, "pr-input");
  mkdirSync(prInput, { recursive: true });
  if (shards) {
    mkdirSync(join(prInput, "shards"));
    writeFileSync(
      join(prInput, "shards", "manifest.json"),
      JSON.stringify({ shards, total_lines: 4200, total_files: 3 }),
    );
  }
  const legDir = join(root, "legs");
  for (const [name, leg] of Object.entries(legs)) {
    const dir = join(legDir, name);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "review.json"), JSON.stringify(leg.review));
    writeFileSync(
      join(dir, "cost.json"),
      JSON.stringify({
        type: "result",
        total_cost_usd: leg.cost,
        model: leg.model ?? "opus",
      }),
    );
  }
  const output = join(root, "github_output");
  writeFileSync(output, "");
  return {
    prInput,
    output,
    env: {
      PR_INPUT_DIR: prInput,
      SHARD_REVIEW_DIR: legDir,
      GITHUB_OUTPUT: output,
    },
  };
}

/** @param {string} prInput @param {string} name */
const readJson = (prInput, name) =>
  JSON.parse(readFileSync(join(prInput, name), "utf8"));

const finding = (path) => ({
  path,
  line: 1,
  severity: "blocking",
  title: path,
});

test("every shard's findings fold into one review with one summed price", () => {
  const { prInput, output, env } = setup(
    {
      "shard-00.diff": {
        review: { summary: "first slice", findings: [finding("a.py")] },
        cost: 1.1,
      },
      "shard-01.diff": {
        review: { summary: "second slice", findings: [finding("c.py")] },
        cost: 0.1,
      },
    },
    SHARDS,
  );
  assert.deepEqual(main({ env }), { complete: true, delivered: 2, planned: 2 });
  assert.deepEqual(readJson(prInput, "review.json"), {
    summary:
      "_Read 4200 of 4200 diff lines across 3 files in 2 shards._\n\nfirst slice\n\nsecond slice",
    findings: [finding("a.py"), finding("c.py")],
  });
  assert.deepEqual(readJson(prInput, "cost.json"), {
    type: "result",
    total_cost_usd: 1.2,
    model: "opus",
  });
  assert.equal(existsSync(join(prInput, INCOMPLETE_FILE)), false);
  assert.equal(readFileSync(output, "utf8"), "complete=true\n");
});

test("a shard that delivered nothing makes the review say the read is incomplete", () => {
  const { prInput, output, env } = setup(
    {
      "shard-00.diff": {
        review: { summary: "first slice", findings: [finding("a.py")] },
        cost: 1.1,
      },
    },
    SHARDS,
  );
  assert.deepEqual(main({ env }), {
    complete: false,
    delivered: 1,
    planned: 2,
  });
  const review = readJson(prInput, "review.json");
  assert.deepEqual(review.findings, [finding("a.py")]);
  assert.equal(
    review.summary,
    "_This read is INCOMPLETE. 1 of 2 shards delivered no review (shard-01.diff), so it " +
      "read about 3000 of 4200 diff lines. It does not count as a review of this head._" +
      "\n\nfirst slice",
  );
  // A partial sum would render exactly like a whole price, so none is written.
  assert.deepEqual(readJson(prInput, "cost.json"), {
    type: "result",
    total_cost_usd: null,
    model: null,
  });
  assert.equal(
    readFileSync(join(prInput, INCOMPLETE_FILE), "utf8"),
    "1 of 2 shards delivered no review: shard-01.diff (c.py).\n",
  );
  assert.equal(readFileSync(output, "utf8"), "complete=false\n");
});

test("an unsharded read's one leg passes through whole, diagram and all", () => {
  const review = {
    summary: "whole read",
    diagram: "graph TD; a-->b",
    findings: [finding("a.py")],
  };
  const { prInput, env } = setup({ whole: { review, cost: 2.5 } }, null);
  assert.deepEqual(main({ env }), { complete: true, delivered: 1, planned: 1 });
  assert.deepEqual(readJson(prInput, "review.json"), review);
  assert.equal(readJson(prInput, "cost.json").total_cost_usd, 2.5);
});

test("a read where no leg delivered refuses to write a review", () => {
  const { prInput, env } = setup({}, SHARDS);
  assert.throws(() => main({ env }), /none of the 2 read leg\(s\) delivered/);
  assert.equal(existsSync(join(prInput, "review.json")), false);
});

test("an artifact for a leg this run never planned is refused", () => {
  const { env } = setup(
    { "shard-07.diff": { review: { findings: [] }, cost: 1 } },
    SHARDS,
  );
  assert.throws(() => main({ env }), /no planned leg is named "shard-07.diff"/);
});
