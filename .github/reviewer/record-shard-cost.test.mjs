// Behavior tests for record-shard-cost.mjs: run the real script against an
// execution log and assert the shard-cost.json it leaves for the synthesis job.
// The load-bearing case is the unreadable log — the file must still be written,
// with a null cost, so the reader can tell "this shard never ran the step" from
// "this shard ran it and had no cost to report".
import { describe, it, afterEach } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(__dirname, "record-shard-cost.mjs");

const dirs = [];
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop(), { recursive: true, force: true });
});

// Run the recorder with an execution log holding `events` (omit for no log at
// all) and return the parsed shard-cost.json it wrote.
function record(events) {
  const dir = mkdtempSync(join(tmpdir(), "rsc-"));
  dirs.push(dir);
  const out = join(dir, "shard-cost.json");
  const env = { ...process.env, SHARD_COST_FILE: out };
  delete env.RUNNER_TEMP; // else the reader probes the runner's real temp dir
  if (events === undefined) {
    env.EXECUTION_FILE = join(dir, "absent.json");
  } else {
    const log = join(dir, "exec.json");
    writeFileSync(log, JSON.stringify(events));
    env.EXECUTION_FILE = log;
  }
  execFileSync("node", [SCRIPT], { env, stdio: "pipe" });
  return JSON.parse(readFileSync(out, "utf8"));
}

// The escalated shape: a cheap read, then a full-price re-read of the same shard.
function recordEscalated(cheap, escalated, kept) {
  const dir = mkdtempSync(join(tmpdir(), "rsc-"));
  dirs.push(dir);
  const out = join(dir, "shard-cost.json");
  const env = { ...process.env, SHARD_COST_FILE: out };
  delete env.RUNNER_TEMP;
  const write = (name, events) => {
    const log = join(dir, name);
    writeFileSync(log, JSON.stringify(events));
    return log;
  };
  env.EXECUTION_FILE = write("cheap.json", cheap);
  env.EXECUTION_FILE_ESCALATED = write("escalated.json", escalated);
  if (kept !== undefined) env.ESCALATION_KEPT = kept;
  execFileSync("node", [SCRIPT], { env, stdio: "pipe" });
  return JSON.parse(readFileSync(out, "utf8"));
}

describe("record-shard-cost", () => {
  it("records the cost and model from the shard's execution log", () => {
    const parsed = record([
      { type: "system", subtype: "init", model: "claude-opus-4-8" },
      { type: "result", subtype: "success", total_cost_usd: 0.42 },
    ]);
    assert.deepEqual(parsed, { cost: 0.42, model: "claude-opus-4-8" });
  });

  it("writes a null cost when the execution log is missing", () => {
    assert.deepEqual(record(), { cost: null, model: null });
  });

  it("refuses to run without SHARD_COST_FILE", () => {
    const env = { ...process.env };
    delete env.SHARD_COST_FILE;
    assert.throws(() => execFileSync("node", [SCRIPT], { env, stdio: "pipe" }));
  });
});

describe("record-shard-cost, on a shard that escalated", () => {
  it("prices BOTH reads and credits the model whose findings post", () => {
    const got = recordEscalated(
      [{ total_cost_usd: 0.4, model: "low-1" }],
      [{ total_cost_usd: 1.6, model: "high-1" }],
    );
    // The escalated read replaced the cheap verdict, so the cheap read is spend
    // with no findings left — and the footer must still name what it cost.
    assert.deepEqual(got, { cost: 2, model: "high-1" });
  });

  it("drops the price when one of the two reads left no readable cost", () => {
    // Half a sum renders exactly like a whole one: ~40% of what the review cost,
    // with nothing in the footer marking it partial.
    const got = recordEscalated(
      [{ total_cost_usd: 0.4, model: "low-1" }],
      [{ model: "high-1" }],
    );
    assert.equal(got.cost, null);
  });

  it("credits the cheap model when the escalated read died and was discarded", () => {
    const got = recordEscalated(
      [{ total_cost_usd: 0.4, model: "low-1" }],
      [{ total_cost_usd: 0.1, model: "high-1" }],
      "false",
    );
    // Still both costs: the failed read spent real money. The published findings
    // are the cheap read's, so that is the model this shard is credited to.
    assert.deepEqual(got, { cost: 0.5, model: "low-1" });
  });
});
