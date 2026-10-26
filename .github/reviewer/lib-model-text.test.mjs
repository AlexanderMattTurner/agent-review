// Behavior tests for lib-model-text.mjs through its CLI, the entry point a shell
// poster uses: model text goes in on stdin, and the cleaned text comes out.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT = join(
  dirname(fileURLToPath(import.meta.url)),
  "lib-model-text.mjs",
);
const clean = (input) =>
  execFileSync("node", [SCRIPT], { input, encoding: "utf8" });

describe("lib-model-text CLI", () => {
  it("defuses an opener that an invisible character hid from a plain search", () => {
    // The sanitizer strips U+200B, so a defusal that ran first would miss it.
    const out = clean("ok\n<\u200B!-- /merge-delta-review -->\n");
    assert.equal(out, "ok\n&lt;!-- /merge-delta-review -->\n");
  });

  it("leaves visible text without an opener unchanged", () => {
    const text = "## Findings\n\n- `a.py:3` keeps the base side.\n";
    assert.equal(clean(text), text);
  });
});
