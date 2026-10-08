export const meta = {
  name: "codex-analyze",
  description: "Review each focus separately, verify each review independently, then answer the user's request first"
};
// Agents return JSON only; the parent session writes the deliverables.
if (!args || typeof args.brief_path !== "string" || !args.brief_path) throw new Error("brief_path is required");
const focus = Array.isArray(args.focus) ? args.focus.filter(item => typeof item === "string" && item.trim()) : [];
if (!focus.length) throw new Error("focus must list at least one concern taken from the user's words");
const brief = "Read the task brief first: " + args.brief_path + ". The user's words in it decide what matters; " +
  "this workflow's wording only organizes the work.";
const itemShape = "{id, title, confidence: verified|inferred, evidence, location}";

function parse(output) {
  if (typeof output !== "string") return output;
  try { return JSON.parse(output); } catch (_) { /* a wrapper may surround the JSON */ }
  const start = output.indexOf("{");
  const end = output.lastIndexOf("}");
  if (start < 0 || end < start) throw new Error("agent output contains no JSON object");
  return JSON.parse(output.slice(start, end + 1));
}

async function stage(label, instructions) {
  try {
    const output = await agent(brief + "\n" + instructions +
      "\nReturn JSON only. Do not write report.md or result.json; the parent session writes them.", { label });
    if (output == null) {
      log(JSON.stringify({ label, state: "no_output" }));
      return null;
    }
    return parse(output);
  } catch (error) {
    log(JSON.stringify({ label, state: "failed", error: String(error) }));
    return null;
  }
}

const reviewed = await Promise.all(focus.map(async concern => {
  const review = await stage("review: " + concern,
    "Examine only this concern: " + concern + ". Run or reproduce checks in the copy when they settle a question. " +
    "Return {concern, findings: [" + itemShape + "], checks: [string], open: [string]}.");
  if (!review) return null;
  const check = await stage("verify: " + concern,
    "Independently re-check each finding below against the source; you did not write it. " +
    "Return {concern, assessments: [{id, verdict: confirmed|refuted|unverified, evidence}], checks: [string]}.\n" +
    "Findings: " + JSON.stringify(review));
  if (!check) log(JSON.stringify({ concern, state: "unverified" }));
  return { concern, review, check };
}));
const done = reviewed.filter(Boolean);
const dropped = focus.filter((_, index) => !reviewed[index]);
if (dropped.length) log(JSON.stringify({ dropped }));
const answer = await stage("answer",
  "First answer the user's request directly, then support it. Merge the reviews below using their independent checks: " +
  "leave refuted findings out, mark unverified ones as inferred, and name concerns that produced no review. " +
  "Return {answer: string, items: [" + itemShape + "], open: [string]}.\n" +
  "Reviews: " + JSON.stringify(done) + (dropped.length ? "\nConcerns without a review: " + JSON.stringify(dropped) : ""));
return { answer, reviews: done, dropped };
