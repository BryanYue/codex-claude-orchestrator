export const meta = {
  name: "codex-full-review",
  description: "Inspect correctness, architecture and maintainability; retain unverified findings in a complete report"
};
// The parent passes its original request, sources, scope and run-owned report path.
if (!args || typeof args.user_request !== "string" || !args.user_request.trim() ||
    typeof args.report_path !== "string" || !args.report_path) {
  throw new Error("user_request and report_path are required");
}
const scope = args.review_scope || "full";
if (!["defects", "quality", "full"].includes(scope)) throw new Error("Invalid review_scope");
const dimensions = scope === "defects" ? ["correctness, security and recovery", "test coverage"] :
  scope === "quality" ? ["architecture and responsibilities", "maintainability, duplication, prompts and documentation"] :
  ["correctness, security and recovery", "test coverage", "architecture and responsibilities", "maintainability, duplication, prompts and documentation"];
const context = JSON.stringify(args);
const reports = await Promise.all(dimensions.map(async (dimension) => {
  try {
    const report = await agent(
      "Original user request: " + args.user_request + "\nReview context: " + context +
      "\nInspect " + dimension + ". Work in the independent copy; original sources are write-protected. " +
      "Use evidence, distinguish reproduced/code_confirmed/probable/subjective. Zero findings is valid. " +
      "Return findings, inspected coverage, checks actually run and unresolved questions. " +
      "Do not publish or write the final report file; the synthesis step owns it.",
      { label: dimension, phase: "review" });
    return { dimension, report, state: report == null ? "missing" : "returned" };
  } catch (error) {
    return { dimension, state: "failed", error: String(error) };
  }
}));
const synthesis = await agent(
  "Original user request: " + args.user_request + "\nReview context: " + context +
  "\nDimension reports: " + JSON.stringify(reports) +
  "\nVerify the findings against source, merge duplicates and keep unverified items explicitly unresolved. " +
  "Never discard a failed/missing dimension. Write the COMPLETE JSON to exactly " + args.report_path +
  ". Required fields: status (completed or blocked), summary, evidence, checks, unresolved. " +
  "Include coverage and findings; each finding has unique id, category (defect/design/maintainability/test/documentation), " +
  "confidence (reproduced/code_confirmed/probable/subjective), summary and evidence array. " +
  "Confirmed findings need evidence. Incomplete required coverage must be blocked. Return the same complete JSON; do not claim acceptance.",
  { label: "synthesize complete report", phase: "verification" });
if (synthesis == null) throw new Error("Report synthesis did not complete");
return { dimensions: reports, synthesis };
