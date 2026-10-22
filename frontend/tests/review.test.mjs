import assert from "node:assert/strict";
import test from "node:test";
import {reviewRequest} from "../review.mjs";

test("review carries only the explicit decision and exact displayed approval ID", () => {
  const proposal = {approval_id: "shown-action", status: "pending", arguments: {body: "Untrusted ticket text"}};
  const request = reviewRequest(proposal, "approve");
  proposal.approval_id = "newer-action";
  assert.deepEqual(request, {message: "approve", approval_id: "shown-action"});
});

test("decline is explicit and approved-action retries remain bound to the same action", () => {
  const proposal = {approval_id: "shown-action", status: "approved"};
  assert.throws(() => reviewRequest(proposal, "decline"));
  assert.deepEqual(reviewRequest({...proposal, status: "pending"}, "decline"), {message: "decline", approval_id: "shown-action"});
  assert.equal(reviewRequest(proposal, "approve").approval_id, "shown-action");
});

test("missing, terminal or implicit decisions cannot construct an approval request", () => {
  for (const [proposal, decision] of [[null, "approve"], [{status: "completed", approval_id: "old"}, "approve"],
    [{status: "pending", approval_id: "new"}, "yes"], [{status: "pending"}, "approve"]]) {
    assert.throws(() => reviewRequest(proposal, decision));
  }
});
