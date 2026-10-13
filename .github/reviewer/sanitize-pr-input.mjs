// Filter untrusted PR text through the agent-sanitizer before it reaches
// the review agent. Reads UTF-8 on stdin, writes the sanitized text on stdout,
// and writes a human-readable report of everything it neutralized on stderr
// (empty when the input was clean).
//
// Layer 1 only (`html: false`): strips payload-capable invisible/format (Cf)
// characters and ANSI/SGR escapes and normalizes lone UTF-16 surrogates — the
// injection vectors — while leaving the visible bytes untouched, so the diff
// stays byte-faithful and reviewable. Running the HTML layer would splice out
// legitimate HTML/markdown in the changed files and corrupt the review, so the
// exfil-URL scan is run separately and NON-destructively: suspicious URLs are
// reported, never removed.
//
// Exits 3 when the sanitizer itself throws on this input, with the stack on
// stderr and nothing on stdout. A PR can trip the base branch's sanitizer with its
// own text (a fix for that very crash quotes the input), so the caller holds such a
// PR for a human instead of failing. Any other non-zero exit is a broken install.
//
// Usage: node sanitize-pr-input.mjs < raw.txt > cleaned.txt 2> report.txt
import { sanitize } from "agent-sanitizer";
import { detectExfil } from "agent-sanitizer/html";

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = Buffer.concat(chunks).toString("utf8");

const SANITIZER_THREW = 3;

let cleaned, found, warnings, threats;
try {
  ({ cleaned, found, warnings } = await sanitize(input, { html: false }));
  threats = detectExfil(input) || [];
} catch (error) {
  process.stderr.write(`${error?.stack ?? error}\n`);
  process.exit(SANITIZER_THREW);
}

const exfilReasons = [
  ...new Set(
    threats.map(
      (threat) =>
        `${threat.isImage ? "image" : "link"} to ${threat.target}: ${threat.reason}`,
    ),
  ),
];

process.stdout.write(cleaned);

const report = [...warnings];
if (found.length > 0)
  report.unshift(`Neutralized categories: ${found.join(", ")}`);
if (exfilReasons.length > 0)
  report.push(`Exfil-shaped URLs detected: ${exfilReasons.join("; ")}`);
if (report.length > 0) process.stderr.write(`${report.join("\n")}\n`);
