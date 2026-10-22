// Bind a review button to the exact displayed proposal; the server verifies ownership.
export function reviewRequest(proposal, decision) {
  if (!proposal || !["pending", "approved"].includes(proposal.status) ||
      typeof proposal.approval_id !== "string" || !proposal.approval_id ||
      !["approve", "decline"].includes(decision) || proposal.status === "approved" && decision !== "approve") {
    throw new Error("Select an active proposal and an explicit review decision.");
  }
  return {message: decision, approval_id: proposal.approval_id};
}
