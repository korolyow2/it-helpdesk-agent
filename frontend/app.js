import {readAnswerStream} from "/assets/stream.mjs";
import {reviewRequest} from "/assets/review.mjs";

const form = document.querySelector("#connection");
const status = document.querySelector("#status");
const tools = document.querySelector("#tools");
const session = document.querySelector("#session");
const review = document.querySelector("#review");
const proposalFields = document.querySelector("#proposal");
const reviewButtons = [document.querySelector("#approve"), document.querySelector("#decline")];
let credentials = null;
let busy = false;
let pending = null;
let queuedReview = null;
session.value = crypto.randomUUID();

function showStatus(message, error = false) {
  status.textContent = message;
  status.dataset.error = String(error);
}

function showTools(items, empty = "No tools were called for this reply.") {
  tools.replaceChildren();
  for (const item of items.length ? items : [{name: empty}]) {
    const row = document.createElement("li");
    row.textContent = item.status ? `${item.name} — ${item.status}` : item.name;
    tools.append(row);
  }
}

function lockConnection(value) {
  busy = value;
  for (const field of form.elements) field.disabled = value;
  for (const button of reviewButtons) button.disabled = value || !pending || pending.status === "approved" && button.id === "decline";
}

function showProposal(proposal) {
  pending = proposal && ["pending", "approved"].includes(proposal.status) ? proposal : null;
  review.hidden = !proposal;
  proposalFields.replaceChildren();
  if (!proposal) return;
  document.querySelector("#review-status").textContent = proposal.status === "completed"
    ? "The previously uncertain action is now confirmed completed. No further change will run."
    : proposal.status === "approved"
    ? "Already approved. You may retry this exact action to resolve an interrupted outcome."
    : "Waiting for your explicit decision.";
  for (const [name, value] of Object.entries({operation: proposal.operation, ...proposal.arguments})) {
    const term = document.createElement("dt");
    term.textContent = name;
    const detail = document.createElement("dd");
    detail.textContent = String(value);
    proposalFields.append(term, detail);
  }
  for (const button of reviewButtons) button.disabled = busy || !pending || pending.status === "approved" && button.id === "decline";
}

async function restoreProposal() {
  const response = await fetch(`/chat/sessions/${encodeURIComponent(credentials.session)}/approval`, {
    headers: {Authorization: `Bearer ${credentials.token}`},
  });
  if (!response.ok) throw new Error("The current proposal could not be verified. Reconnect or try again.");
  showProposal((await response.json()).approval);
}

await customElements.whenDefined("deep-chat");
const chat = document.querySelector("#chat");
chat.textInput = {placeholder: {text: "Describe your IT issue or provide a ticket reference."}};
chat.introMessage = {text: "I can find tickets and propose a ticket or comment. Every change requires your approval."};
chat.requestBodyLimits = {maxMessages: 1};
chat.submitButtonStyles = {stop: {container: {default: {display: "none"}}}};

chat.connect = {
  stream: true,
  handler: async (body, signals) => {
    if (!credentials || busy) {
      await signals.onResponse({error: "Connect with your credentials before sending a message."});
      signals.onClose();
      return;
    }
    const message = body.messages?.at(-1)?.text;
    const decision = queuedReview?.message === message ? queuedReview : null;
    queuedReview = null;
    if (typeof message !== "string" || !message.trim() || message.length > 10000) {
      await signals.onResponse({error: "Enter a message of up to 10,000 characters."});
      signals.onClose();
      return;
    }

    lockConnection(true);
    showStatus("Waiting for the Helpdesk response…");
    showTools([], "Waiting for this request to complete.");
    try {
      const response = await fetch("/chat/stream", {
        method: "POST",
        headers: {"Content-Type": "application/json", Authorization: `Bearer ${credentials.token}`},
        body: JSON.stringify({user_id: credentials.user, session_id: credentials.session, message,
          ...(decision ? {approval_id: decision.approval_id} : {})}),
      });
      signals.onOpen();
      const result = await readAnswerStream(response, (text) => signals.onResponse({text}));
      showStatus("Reply complete.");
      try {
        const details = await fetch(`/chat/requests/${encodeURIComponent(result.request_id)}/tools`, {
          headers: {Authorization: `Bearer ${credentials.token}`},
        });
        if (!details.ok) throw new Error("Tool details unavailable.");
        showTools((await details.json()).tools);
      } catch {
        showTools([], "Tool activity is unavailable for this completed reply.");
      }
      await restoreProposal();
    } catch (error) {
      const message = `Reply incomplete. ${error.message}`;
      showStatus(message, true);
      showTools([], "Request did not complete; tool activity is unavailable.");
      await signals.onResponse({error: message});
      await restoreProposal().catch(() => showProposal(null));
    } finally {
      signals.onClose();
      lockConnection(false);
    }
  },
};

for (const button of reviewButtons) {
  button.addEventListener("click", () => {
    if (busy || !credentials || !pending) return;
    queuedReview = reviewRequest(pending, button.id);
    chat.submitUserMessage({text: queuedReview.message});
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const next = {user: document.querySelector("#user").value.trim(),
    token: document.querySelector("#token").value.trim(), session: session.value.trim()};
  // Visible browser history is never replayed to the backend or shared across identities.
  chat.clearMessages();
  credentials = next;
  queuedReview = null;
  showProposal(null);
  showTools([], "No completed request yet.");
  lockConnection(true);
  try {
    await restoreProposal();
    showStatus("Ready. A pending proposal from this session is restored when available.");
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    lockConnection(false);
  }
});

document.querySelector("#new-session").addEventListener("click", () => {
  session.value = crypto.randomUUID();
  credentials = null;
  queuedReview = null;
  showProposal(null);
  chat.clearMessages();
  showTools([], "No completed request yet.");
  showStatus("New session selected. Click Connect to start.");
});
