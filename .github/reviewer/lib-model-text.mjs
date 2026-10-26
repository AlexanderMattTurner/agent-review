// The one cleaning step for model-written text bound for a posted GitHub body.
//
// PROBLEM CLASS — model-written text forging a marker the reviewer reads back. A posted body
// carries hidden HTML-comment records (a coverage stamp, a severity or read marker, a block
// delimiter), and the model's text, derived from an untrusted diff, sits in the same body. Every
// poster runs model text through modelText, so none can forget a step or run them out of order.
//
// INVARIANT — two steps, in this order:
//   1. Layer-1 sanitize: strip invisible/format (Cf) characters and ANSI escapes. Visible bytes —
//      code, markdown, emoji — stay, so a legitimate suggestion is never corrupted.
//   2. Defuse every `<!--`, so the text opens no HTML comment and forges no record. The text
//      stays visible, and a fenced block gets the same treatment, since readers match raw bodies.
// Step 1 must run first: `<`, then U+200B, then `!--` holds no `<!--` until the strip removes the U+200B.
//
// CLI: `node lib-model-text.mjs < model.md > clean.md`, for a poster written in shell.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { sanitize } from "agent-sanitizer";

/** @param {unknown} text @returns {Promise<string>} */
export async function modelText(text) {
  if (typeof text !== "string" || !text) return "";
  const { cleaned } = await sanitize(text, { html: false });
  return cleaned.replaceAll("<!--", "&lt;!--");
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  process.stdout.write(await modelText(readFileSync(0, "utf8")));
}
