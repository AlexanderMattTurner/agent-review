// Behavior tests for record-review.mjs: the step that turns the plugin read's
// structured output into the review.json post-pr-review.mjs posts. The severity
// table is the part that decides what holds the merge, so every row is driven;
// the CLI case runs the real script over a real PR_INPUT_DIR.
import { describe, it, after } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { severityOf, toReview } from "./record-review.mjs";
import { scratchDir, removeScratchDirs } from "./lib/test-helpers.mjs";

after(removeScratchDirs);

const SCRIPT = fileURLToPath(new URL("./record-review.mjs", import.meta.url));

describe("severityOf", () => {
  for (const [category, confidence, expected] of [
    ["bug", 100, "blocking"],
    ["bug", 90, "blocking"],
    ["bug", 89, "warning"],
    ["bug", 80, "warning"],
    ["bug", 79, null],
    ["security", 95, "blocking"],
    ["security", 85, "warning"],
    ["claude-md", 90, "warning"],
    ["claude-md", 80, "nit"],
    ["claude-md", 50, null],
  ]) {
    it(`${category} at ${confidence} posts as ${expected}`, () => {
      assert.equal(severityOf(category, confidence), expected);
    });
  }

  it("refuses a category the schema does not allow", () => {
    assert.throws(() => severityOf("style", 99), /unknown finding category/);
  });
});

describe("record-review CLI", () => {
  it("writes the mapped review and the cost record, dropping weak findings", () => {
    const dir = scratchDir("record-review-");
    const finding = {
      path: "a.py",
      line: 3,
      side: "RIGHT",
      category: "bug",
      confidence: 92,
      title: "off by one",
      body: "the loop skips the last item",
      suggestion: "for i in range(n + 1):",
    };
    writeFileSync(
      join(dir, "findings.json"),
      JSON.stringify({
        summary: "one bug",
        findings: [finding, { ...finding, confidence: 60, title: "weak" }],
      }),
    );
    const log = join(dir, "attempt.json");
    writeFileSync(
      log,
      JSON.stringify([
        { type: "system", model: "claude-opus-4-8" },
        { type: "result", is_error: false, total_cost_usd: 2.5 },
      ]),
    );
    execFileSync("node", [SCRIPT], {
      env: { ...process.env, PR_INPUT_DIR: dir, EXECUTION_FILE: log },
    });
    const review = JSON.parse(readFileSync(join(dir, "review.json"), "utf8"));
    assert.deepEqual(
      review,
      toReview({ summary: "one bug", findings: [finding] }),
    );
    assert.equal(review.findings[0].severity, "blocking");
    assert.equal(
      review.findings[0].body,
      "the loop skips the last item\n\n<sub>bug · confidence 92</sub>",
    );
    assert.deepEqual(JSON.parse(readFileSync(join(dir, "cost.json"), "utf8")), {
      type: "result",
      total_cost_usd: 2.5,
      model: "claude-opus-4-8",
    });
  });

  it("fails loud when the read left no findings", () => {
    const dir = scratchDir("record-review-");
    assert.throws(() =>
      execFileSync("node", [SCRIPT], {
        env: { ...process.env, PR_INPUT_DIR: dir, EXECUTION_FILE: "" },
        stdio: "pipe",
      }),
    );
  });
});
