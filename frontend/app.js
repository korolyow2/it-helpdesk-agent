import {readAnswerStream} from "/assets/stream.mjs?v=dfc481e53e";
import {reviewRequest} from "/assets/review.mjs";
import {renderReply} from "/assets/render.mjs?v=793f908592";

const form = document.querySelector("#connection");
const status = document.querySelector("#status");
const tools = document.querySelector("#tools");
const session = document.querySelector("#session");
const review = document.querySelector("#review");
const proposalFields = document.querySelector("#proposal");
const reviewButtons = [document.querySelector("#approve"), document.querySelector("#decline")];
const keyboardHint = document.querySelector("#keyboard-hint");
const busyHint = document.querySelector("#busy-hint");
let credentials = null;
let busy = false;
let pending = null;
let queuedReview = null;
session.value = crypto.randomUUID();

function showStatus(message, error = false) {
  status.textContent = message;
  status.dataset.error = String(error);
  status.setAttribute("role", error ? "alert" : "status");
}

function showTools(items, empty = "No tools were called for this reply.") {
  tools.replaceChildren();
  for (const item of items.length ? items : [{name: empty}]) {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = item.name;
    row.append(name);
    if (item.status) {
      const state = document.createElement("span");
      state.className = "tool-state";
      state.dataset.status = item.status;
      state.textContent = item.status;
      row.append(state);
    } else {
      row.className = "empty-tool";
    }
    tools.append(row);
  }
}

function lockConnection(value) {
  busy = value;
  status.dataset.busy = String(value);
  chat.setAttribute("aria-busy", String(value));
  keyboardHint.hidden = value;
  busyHint.hidden = !value;
  for (const field of form.elements) field.disabled = value;
  for (const button of reviewButtons) button.disabled = value || !pending || pending.status === "approved" && button.id === "decline";
}

function showProposal(proposal) {
  pending = proposal && ["pending", "approved"].includes(proposal.status) ? proposal : null;
  review.hidden = !proposal;
  proposalFields.replaceChildren();
  if (!proposal) return;
  const isTicket = proposal.operation === "create_ticket";
  document.querySelector("#review-title").textContent = proposal.status === "completed"
    ? (isTicket ? "Ticket created" : "Comment added")
    : (isTicket ? "Create ticket" : "Add comment");
  document.querySelector("#review-status").textContent = proposal.status === "completed"
    ? "Your request is complete."
    : proposal.status === "approved"
    ? "Your approval is saved. Select Check result to confirm the outcome."
    : "Please review the details.";
  document.querySelector("#approve").textContent = proposal.status === "approved" ? "Check result" : "Approve";
  const fields = isTicket
    ? [["Title", proposal.arguments.title], ["Details", proposal.arguments.description]]
    : [["Ticket", proposal.arguments.ticket_id], ["Comment", proposal.arguments.body]];
  for (const [name, value] of fields) {
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
  if (response.status === 401) {
    credentials = null;
    showProposal(null);
    throw new Error("Authentication failed. Check your user ID and access token, then click Connect again.");
  }
  if (!response.ok) throw new Error("The current proposal could not be verified. Reconnect or try again.");
  showProposal((await response.json()).approval);
}

await customElements.whenDefined("deep-chat");
const chat = document.querySelector("#chat");
chat.chatStyle = {fontFamily: "inherit", fontSize: "15px", backgroundColor: "#ffffff"};
chat.messageStyles = {
  default: {
    shared: {
      outerContainer: {marginTop: "18px", marginBottom: "18px"},
      bubble: {fontSize: "15px", lineHeight: "1.65", padding: "13px 17px", borderRadius: "14px", maxWidth: "86%"},
    },
    ai: {bubble: {backgroundColor: "#f3f6fa", color: "#263951", border: "1px solid #e7edf4"}},
    user: {bubble: {backgroundColor: "#203a5e", color: "#ffffff"}},
    error: {bubble: {backgroundColor: "#fff1f2", color: "#9c2034", border: "1px solid #f0c9d0"}},
  },
};
chat.inputAreaStyle = {padding: "12px 0 10px", width: "100%", boxSizing: "border-box", backgroundColor: "#ffffff"};
chat.auxiliaryStyle = ".input-button:focus-visible { outline: 3px solid #2a7c87; outline-offset: 3px; }";
chat.textInput = {
  characterLimit: 10000,
  placeholder: {text: "Describe your issue or share a ticket reference...", style: {color: "#637185"}},
  styles: {
    container: {border: "1px solid #cbd6e3", borderRadius: "12px", backgroundColor: "#fafbfd", boxShadow: "none", width: "calc(100% - 40px)", maxHeight: "190px", boxSizing: "border-box"},
    text: {fontSize: "16px", lineHeight: "1.6", padding: "16px 60px 16px 16px", minHeight: "80px", maxHeight: "156px", color: "#203047"},
    focus: {border: "1px solid #2a7c87", boxShadow: "0 0 0 3px #2a7c871a"},
  },
};
chat.introMessage = {
  text: "Welcome to IT Helpdesk. Describe your issue or share a ticket reference.",
};
chat.requestBodyLimits = {maxMessages: 1};
chat.displayLoadingBubble = false;
chat.submitButtonStyles = {
  submit: {
    container: {
      default: {display: "flex", alignItems: "center", justifyContent: "center", width: "44px", height: "44px", borderRadius: "10px", backgroundColor: "#203a5e", bottom: "10px", right: "10px"},
      hover: {backgroundColor: "#152c4c"},
    },
    svg: {
      content: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" role="img" aria-label="Send message"><title>Send message</title><path d="M12 5v14M5 12l7-7 7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
      styles: {default: {filter: "brightness(0) invert(1)", width: "20px", height: "20px"}},
    },
  },
  disabled: {container: {default: {display: "flex", backgroundColor: "#e1e7ef"}}, svg: {styles: {default: {filter: "none", opacity: ".5"}}}},
  loading: {container: {default: {display: "none"}}},
  stop: {container: {default: {display: "none"}}},
};

chat.connect = {
  stream: true,
  handler: async (body, signals) => {
    let ownsRequest = false;
    try {
      signals.onOpen();
      if (!credentials) throw new Error("Connect with your user ID and access token before sending a message.");
      if (busy) throw new Error("Wait for the current request to finish before sending another message.");

      const message = body.messages?.at(-1)?.text;
      const decision = queuedReview?.message === message ? queuedReview : null;
      queuedReview = null;
      if (typeof message !== "string" || !message.trim() || message.length > 10000) {
        throw new Error("Enter a message of up to 10,000 characters.");
      }

      ownsRequest = true;
      lockConnection(true);
      showStatus("Waiting for the Helpdesk response...");
      showTools([], "Waiting for this request to complete.");
      const response = await fetch("/chat/stream", {
        method: "POST",
        headers: {"Content-Type": "application/json", Authorization: `Bearer ${credentials.token}`},
        body: JSON.stringify({user_id: credentials.user, session_id: credentials.session, message,
          ...(decision ? {approval_id: decision.approval_id} : {})}),
      });
      if (response.status === 401) {
        credentials = null;
        showProposal(null);
        throw new Error("Your connection is no longer authenticated. Check your access token and click Connect again.");
      }
      let answerStarted = false;
      let replaceAnswer = false;
      const result = await readAnswerStream(response, (text) => {
        if (!answerStarted) {
          showStatus("Writing the reply...");
          answerStarted = true;
        }
        const rendered = renderReply(text, value => signals.onResponse(value), {
          overwrite: replaceAnswer,
          animate: !document.hidden && !matchMedia("(prefers-reduced-motion: reduce)").matches,
        });
        replaceAnswer = false;
        return rendered;
      }, () => {
        answerStarted = false;
        replaceAnswer = true;
        showStatus("Working on your request...");
      });
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
      if (["authentication_required", "authentication_failed"].includes(error.code)) {
        credentials = null;
        showProposal(null);
      }
      const message = ownsRequest ? `Reply incomplete. ${error.message}` : error.message;
      showStatus(message, true);
      if (ownsRequest) showTools([], "Request did not complete; tool activity is unavailable.");
      // Deep Chat 2.5.1 cannot finalize an error-only stream before its first
      // text chunk. Render an application-owned error role, then close normally.
      await signals.onResponse({text: message, role: "error"});
      if (ownsRequest && credentials) await restoreProposal().catch(() => showProposal(null));
    } finally {
      try {
        signals.onClose();
      } finally {
        if (ownsRequest) lockConnection(false);
      }
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
  showStatus("Verifying your connection...");
  try {
    await restoreProposal();
    showStatus("Ready. A pending proposal from this session is restored when available.");
  } catch (error) {
    credentials = null;
    showProposal(null);
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
