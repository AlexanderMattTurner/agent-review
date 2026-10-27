// Behavior tests for lib-review-cost.mjs: the cost accounting behind the PR
// review footnote. Every case drives the real functions over real files on
// disk — a Claude execution log or the cost record distilled from it — and
// asserts the value a caller renders from, never an internal call count.
import { describe, it, after } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import {
  readRunCost,
  formatDollars,
  weeklyBudget,
  plansLine,
} from "./lib-review-cost.mjs";
import { scratchDir, removeScratchDirs } from "./lib/test-helpers.mjs";

after(removeScratchDirs);

// Write `content` (serialized unless already a string) to a fresh execution-log
// file and return its path.
function execLog(content) {
  const path = join(scratchDir("lrc-exec-"), "claude-execution-output.json");
  writeFileSync(
    path,
    typeof content === "string" ? content : JSON.stringify(content),
  );
  return path;
}

// Run `fn` with `vars` applied to process.env, restoring the previous values
// (including absence) afterwards, so an env-driven case cannot leak into the
// next test.
function withEnv(vars, fn) {
  const saved = new Map(Object.keys(vars).map((k) => [k, process.env[k]]));
  for (const [k, v] of Object.entries(vars)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  try {
    return fn();
  } finally {
    for (const [k, v] of saved) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

describe("readRunCost", () => {
  it("reads the cost and model out of a streamed event array", () => {
    const file = execLog([
      { type: "system", subtype: "init", model: "claude-sonnet-5" },
      { type: "assistant" },
      { type: "result", subtype: "success", total_cost_usd: 0.42 },
    ]);
    assert.deepEqual(readRunCost(file), {
      cost: 0.42,
      model: "claude-sonnet-5",
    });
  });

  it("reads a log written as one object rather than an event array", () => {
    const file = execLog({ total_cost_usd: 1.5, model: "claude-opus-4-8" });
    assert.deepEqual(readRunCost(file), {
      cost: 1.5,
      model: "claude-opus-4-8",
    });
  });

  it("takes the last cost and the first model when events disagree", () => {
    const file = execLog([
      { model: "claude-sonnet-5", total_cost_usd: 1 },
      { model: "claude-opus-4-8", total_cost_usd: 2 },
    ]);
    assert.deepEqual(readRunCost(file), {
      cost: 2,
      model: "claude-sonnet-5",
    });
  });

  it("ignores non-object entries in the event array", () => {
    const file = execLog([null, "noise", 7, { total_cost_usd: 0.5 }]);
    assert.deepEqual(readRunCost(file), { cost: 0.5, model: undefined });
  });

  it("reports no cost when the log carries none", () => {
    const file = execLog([{ type: "system", model: "claude-sonnet-5" }]);
    assert.deepEqual(readRunCost(file), {
      cost: undefined,
      model: "claude-sonnet-5",
    });
  });

  it("reports no cost for a missing or unparsable log", () => {
    assert.deepEqual(
      readRunCost("/nonexistent/claude-execution-output.json"),
      {},
    );
    assert.deepEqual(readRunCost(execLog("{ not json")), {});
  });

  it("falls back to EXECUTION_FILE when no path is passed", () => {
    const file = execLog([{ total_cost_usd: 3, model: "m" }]);
    withEnv({ EXECUTION_FILE: file, RUNNER_TEMP: undefined }, () => {
      assert.deepEqual(readRunCost(), { cost: 3, model: "m" });
    });
  });

  it("falls back to the runner's temp dir when EXECUTION_FILE is unset", () => {
    // The Claude action leaves its log at $RUNNER_TEMP/claude-execution-output.json.
    const file = execLog([{ total_cost_usd: 4, model: "m" }]);
    withEnv({ EXECUTION_FILE: undefined, RUNNER_TEMP: dirname(file) }, () => {
      assert.deepEqual(readRunCost(), { cost: 4, model: "m" });
    });
  });

  it("reports no cost when neither the argument nor either env var names a file", () => {
    withEnv({ EXECUTION_FILE: "", RUNNER_TEMP: undefined }, () => {
      assert.deepEqual(readRunCost(), {});
    });
  });
});

describe("formatDollars", () => {
  it("keeps four decimals below a cent and two at or above it", () => {
    assert.equal(formatDollars(0.0009), "0.0009");
    assert.equal(formatDollars(0.00999), "0.0100");
    assert.equal(formatDollars(0.01), "0.01");
    assert.equal(formatDollars(12.3456), "12.35");
  });
});

describe("weeklyBudget", () => {
  it("takes the override from MAX20X_WEEKLY_USD", () => {
    withEnv({ MAX20X_WEEKLY_USD: "1500.5" }, () => {
      assert.equal(weeklyBudget(), 1500.5);
    });
  });

  it("defaults to a positive budget when the override is unset", () => {
    withEnv({ MAX20X_WEEKLY_USD: undefined }, () => {
      assert.ok(weeklyBudget() > 0);
    });
  });

  it("drops budget-relative text for an unusable override", () => {
    for (const value of ["not-a-number", "0", "-5"])
      withEnv({ MAX20X_WEEKLY_USD: value }, () => {
        // 0 is the signal plansLine reads as "cannot estimate".
        assert.equal(weeklyBudget(), 0, value);
        assert.equal(plansLine(1), "", value);
      });
  });

  it("treats an empty override as unset rather than as a zero budget", () => {
    withEnv({ MAX20X_WEEKLY_USD: "" }, () => {
      assert.ok(weeklyBudget() > 0);
      assert.match(plansLine(1), /PRs\/week/);
    });
  });

  it("reads the allowance from the config both reporters share", () => {
    // A literal here would let this footnote and METRICS.md's review-spend chart
    // publish different denominators for one week — the drift the SSOT prevents.
    const config = JSON.parse(
      readFileSync(
        new URL("../../config/claude-budget.json", import.meta.url),
        "utf8",
      ),
    );
    withEnv({ MAX20X_WEEKLY_USD: undefined }, () => {
      assert.equal(weeklyBudget(), config.max20x_weekly_usd);
    });
  });

  it("blanks the footnote rather than throwing on an unusable config", () => {
    // plansLine calls weeklyBudget() as a DEFAULT ARGUMENT on post-pr-review.mjs's
    // posting path, so a throw here loses the whole review over a footnote.
    const dir = scratchDir("review-budget-");
    const missing = pathToFileURL(join(dir, "absent.json"));
    const malformed = pathToFileURL(join(dir, "malformed.json"));
    writeFileSync(malformed, "{not json");
    withEnv({ MAX20X_WEEKLY_USD: undefined }, () => {
      assert.equal(weeklyBudget(missing), 0);
      assert.equal(weeklyBudget(malformed), 0);
    });
  });
});

describe("plansLine", () => {
  it("floors the PRs-per-week estimate and groups the thousands", () => {
    assert.equal(
      plansLine(0.16, 2000),
      "<sub>📉 ~12,500 PRs/week at this rate on a Max 20× plan.</sub>",
    );
    // floor(1000 / 3) = 333, not 333.33.
    assert.match(plansLine(3, 1000), /~333 PRs\/week/);
  });

  it("says ~0 PRs/week for a cost above the whole weekly budget", () => {
    assert.match(plansLine(2469, 1000), /~0 PRs\/week/);
  });

  it("emits nothing it cannot estimate", () => {
    for (const [cost, weekly, why] of [
      [0, 2000, "a zero cost"],
      [-1, 2000, "a negative cost"],
      [Number.NaN, 2000, "a non-finite cost"],
      [Number.POSITIVE_INFINITY, 2000, "an infinite cost"],
      [1, 0, "no budget"],
    ])
      assert.equal(plansLine(cost, weekly), "", why);
  });

  it("reads the budget from the environment when none is passed", () => {
    withEnv({ MAX20X_WEEKLY_USD: "1000" }, () => {
      assert.match(plansLine(10), /~100 PRs\/week/);
    });
  });
});
