import {readAnswerStream} from "/assets/stream.mjs";

const form = document.querySelector("#connection");
const status = document.querySelector("#status");
const tools = document.querySelector("#tools");
const session = document.querySelector("#session");
let credentials = null;
let busy = false;
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
}

await customElements.whenDefined("deep-chat");
const chat = document.querySelector("#chat");
chat.textInput = {placeholder: {text: "Describe your IT issue or provide a ticket reference."}};
chat.introMessage = {text: "I can help find related IT tickets and check their status."};
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
        body: JSON.stringify({user_id: credentials.user, session_id: credentials.session, message}),
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
    } catch (error) {
      const message = `Reply incomplete. ${error.message}`;
      showStatus(message, true);
      showTools([], "Request did not complete; tool activity is unavailable.");
      await signals.onResponse({error: message});
    } finally {
      signals.onClose();
      lockConnection(false);
    }
  },
};

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const next = {user: document.querySelector("#user").value.trim(),
    token: document.querySelector("#token").value.trim(), session: session.value.trim()};
  // Visible browser history is never replayed to the backend or shared across identities.
  chat.clearMessages();
  credentials = next;
  showTools([], "No completed request yet.");
  showStatus("Ready. Credentials will be verified when you send a message.");
});

document.querySelector("#new-session").addEventListener("click", () => {
  session.value = crypto.randomUUID();
  credentials = null;
  chat.clearMessages();
  showTools([], "No completed request yet.");
  showStatus("New session selected. Click Connect to start.");
});
