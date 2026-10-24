import assert from "node:assert/strict";
import test from "node:test";
import {readFile} from "node:fs/promises";
import {randomUUID} from "node:crypto";
import vm from "node:vm";
import {readAnswerStream} from "../stream.mjs";
import {reviewRequest} from "../review.mjs";
import {renderReply} from "../render.mjs";

class Element {
  constructor() {
    this.dataset = {};
    this.attributes = {};
    this.events = {};
    this.children = [];
    this.value = "";
    this.disabled = false;
    this.hidden = false;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}

async function page(fetcher) {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const form = element("#connection");
  form.elements = [element("#user"), element("#token"), element("#session")];
  const chat = element("#chat");
  chat.clearMessages = () => {};
  const source = (await readFile(new URL("../app.js", import.meta.url), "utf8"))
    .replace(/^import .*;\n/gm, "");
  await vm.runInNewContext(`(async () => {${source}\n})()`, {
    document: {querySelector: element, createElement: () => new Element()},
    customElements: {whenDefined: async () => {}},
    crypto: {randomUUID}, fetch: fetcher, readAnswerStream, reviewRequest, renderReply,
    matchMedia: () => ({matches: false}), setTimeout,
  });
  return {
    element, chat,
    async connect() {
      element("#user").value = "user-001";
      element("#token").value = "test-token";
      await form.events.submit({preventDefault() {}});
    },
  };
}

function signals({failRender = false} = {}) {
  const events = [];
  return {
    events,
    onOpen() { events.push({kind: "open"}); },
    async onResponse(value) {
      assert.equal(value.error, undefined, "Do not use the component's broken error-only stream path.");
      events.push({kind: "response", ...value});
      if (failRender) throw new Error("Rendering failed");
    },
    onClose() { events.push({kind: "close"}); },
  };
}

const message = {messages: [{text: "Read the VPN ticket."}]};
const approval = () => Response.json({approval: null});
const tools = () => Response.json({tools: []});
function answer(text = "The ticket is open.") {
  return new Response(`event: text_delta\ndata: ${JSON.stringify({text})}\n\n` +
    `event: done\ndata: ${JSON.stringify({reply: text, request_id: "test-request"})}\n\n`,
    {headers: {"content-type": "text/event-stream"}});
}

function closed(stream) {
  assert.equal(stream.events.filter(e => e.kind === "open").length, 1);
  assert.equal(stream.events.filter(e => e.kind === "close").length, 1);
}

function idle(ui) {
  assert.equal(ui.element("#status").dataset.busy, "false");
  assert.equal(ui.element("#chat").attributes["aria-busy"], "false");
  assert.equal(ui.element("#keyboard-hint").hidden, false);
  assert.equal(ui.element("#busy-hint").hidden, true);
  assert.equal(ui.element("#user").disabled, false);
}

for (const [operation, arguments_, labels, heading] of [
  ["create_ticket", {title: "VPN account", description: "Access to project Atlas."}, ["Title", "Details"], "Create ticket"],
  ["add_comment", {ticket_id: "12", body: "Restarting did not help."}, ["Ticket", "Comment"], "Add comment"],
]) {
  test(`restored ${operation} review shows exact content without machine metadata`, async () => {
    const proposal = {operation, arguments: arguments_, approval_id: "private-action", status: "pending"};
    const ui = await page(async () => Response.json({approval: proposal}));
    await ui.connect();
    assert.equal(ui.element("#review-title").textContent, heading);
    assert.equal(ui.element("#review").hidden, false);
    const fields = ui.element("#proposal").children.map(field => field.textContent);
    assert.deepEqual(fields.filter((_, i) => i % 2 === 0), labels);
    assert.deepEqual(fields.filter((_, i) => i % 2 === 1), Object.values(arguments_));
    assert.equal(fields.includes(operation), false);
    assert.equal(fields.includes("private-action"), false);
    assert.equal(ui.element("#approve").disabled, false);
  });
}

test("unauthenticated submission shows an error, closes, and makes no request", async () => {
  const ui = await page(() => assert.fail("Unauthenticated input must not reach the API."));
  const stream = signals();
  await ui.chat.connect.handler(message, stream);
  closed(stream);
  assert.equal(ui.element("#status").dataset.error, "true");
  assert.equal(ui.element("#status").attributes.role, "alert");
  const error = stream.events.find(e => e.role === "error");
  assert.match(error.text, /Connect with your user ID and access token/);
  assert.equal(ui.chat.displayLoadingBubble, false);
});

test("a failed connection cannot leave credentials authorized for chat", async () => {
  let calls = 0;
  const ui = await page(async () => {
    calls++;
    return Response.json({message: "Invalid credentials"}, {status: 401});
  });
  await ui.connect();
  const stream = signals();
  await ui.chat.connect.handler(message, stream);
  assert.equal(calls, 1);
  closed(stream);
  idle(ui);
  assert.match(ui.element("#status").textContent, /Connect with your user ID/);
});

test("HTTP 401 clears the connection and permits a later successful reconnect", async () => {
  let authorized = false;
  let chatCalls = 0;
  const ui = await page(async (url) => {
    if (url === "/chat/stream") {
      chatCalls++;
      return authorized ? answer() : Response.json({}, {status: 401});
    }
    return url.endsWith("/tools") ? tools() : approval();
  });
  await ui.connect();
  const failed = signals();
  await ui.chat.connect.handler(message, failed);
  closed(failed);
  idle(ui);
  assert.match(ui.element("#status").textContent, /no longer authenticated/);
  await ui.chat.connect.handler(message, signals());
  assert.equal(chatCalls, 1);
  authorized = true;
  await ui.connect();
  const succeeded = signals();
  await ui.chat.connect.handler(message, succeeded);
  closed(succeeded);
  idle(ui);
  assert.equal(ui.element("#status").textContent, "Reply complete.");
});

test("authentication failure in SSE also clears the connection", async () => {
  let chatCalls = 0;
  const ui = await page(async url => {
    if (url !== "/chat/stream") return approval();
    chatCalls++;
    return new Response('event: error\ndata: {"code":"authentication_failed","message":"Authentication failed."}\n\n',
      {headers: {"content-type": "text/event-stream"}});
  });
  await ui.connect();
  const stream = signals();
  await ui.chat.connect.handler(message, stream);
  closed(stream);
  idle(ui);
  await ui.chat.connect.handler(message, signals());
  assert.equal(chatCalls, 1);
});

test("pending reply changes hints, preserves its busy state, and resets after completion", async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await page(async url => url === "/chat/stream" ? pending : url.endsWith("/tools") ? tools() : approval());
  await ui.connect();
  const stream = signals();
  const running = ui.chat.connect.handler(message, stream);
  assert.equal(ui.element("#status").dataset.busy, "true");
  assert.equal(ui.element("#keyboard-hint").hidden, true);
  assert.equal(ui.element("#busy-hint").hidden, false);
  assert.equal(ui.element("#chat").attributes["aria-busy"], "true");
  const rejected = signals();
  await ui.chat.connect.handler(message, rejected);
  assert.equal(ui.element("#user").disabled, true);
  assert.match(rejected.events.find(e => e.role === "error").text, /current request/);
  release(answer());
  await running;
  idle(ui);
  closed(stream);
});

test("a network failure before the first token ends loading and allows another request", async () => {
  let fail = true;
  const ui = await page(async url => {
    if (url !== "/chat/stream") return url.endsWith("/tools") ? tools() : approval();
    if (fail) throw new TypeError("Network request failed");
    return answer();
  });
  await ui.connect();
  const stream = signals();
  await ui.chat.connect.handler(message, stream);
  closed(stream);
  idle(ui);
  assert.match(stream.events.find(e => e.role === "error").text, /Network request failed/);
  fail = false;
  await ui.chat.connect.handler(message, signals());
  assert.equal(ui.element("#status").textContent, "Reply complete.");
});

test("answer fragments reach the chat before the response completes", async () => {
  let streamController;
  let firstRendered;
  const rendered = new Promise(resolve => { firstRendered = resolve; });
  const response = new Response(new ReadableStream({start(controller) {
    streamController = controller;
  }}), {headers: {"content-type": "text/event-stream"}});
  const ui = await page(async url => url === "/chat/stream" ? response : url.endsWith("/tools") ? tools() : approval());
  await ui.connect();
  const stream = signals();
  const onResponse = stream.onResponse;
  stream.onResponse = async value => {
    await onResponse(value);
    firstRendered();
  };
  const running = ui.chat.connect.handler(message, stream);
  const encode = value => new TextEncoder().encode(value);
  streamController.enqueue(encode('event: text_delta\ndata: {"text":"The ticket "}\n\n'));
  await rendered;
  const visiblePrefix = stream.events.filter(event => event.kind === "response").map(event => event.text).join("");
  assert.ok(visiblePrefix.length > 0 && "The ticket ".startsWith(visiblePrefix));
  assert.equal(stream.events.some(event => event.kind === "close"), false);
  assert.equal(ui.element("#status").textContent, "Writing the reply...");
  assert.equal(ui.element("#status").dataset.busy, "true");
  streamController.enqueue(encode('event: text_delta\ndata: {"text":"is open."}\n\n' +
    'event: done\ndata: {"reply":"The ticket is open.","request_id":"progressive"}\n\n'));
  streamController.close();
  await running;
  assert.equal(stream.events.filter(event => event.kind === "response").map(event => event.text).join(""), "The ticket is open.");
  closed(stream);
  idle(ui);
});

test("even an error-rendering exception closes the stream and releases controls", async () => {
  const ui = await page(async url => {
    if (url === "/chat/stream") throw new TypeError("Network request failed");
    return approval();
  });
  await ui.connect();
  const stream = signals({failRender: true});
  await assert.rejects(ui.chat.connect.handler(message, stream), /Rendering failed/);
  closed(stream);
  idle(ui);
});

test("the final draft overwrites narration instead of appending to it", async () => {
  const source = 'event: text_delta\ndata: {"text":"Let me check."}\n\n' +
    'event: text_reset\ndata: {}\n\n' +
    'event: text_delta\ndata: {"text":"The ticket "}\n\n' +
    'event: text_delta\ndata: {"text":"is open."}\n\n' +
    'event: done\ndata: {"reply":"The ticket is open.","request_id":"replacement"}\n\n';
  const ui = await page(async url => url === "/chat/stream" ? new Response(source,
    {headers: {"content-type": "text/event-stream"}}) : url.endsWith("/tools") ? tools() : approval());
  await ui.connect();
  const stream = signals();
  await ui.chat.connect.handler(message, stream);
  let visible = "";
  for (const event of stream.events.filter(event => event.kind === "response")) {
    visible = event.overwrite ? event.text : visible + event.text;
  }
  assert.equal(visible, "The ticket is open.");
  assert.equal(stream.events.filter(event => event.overwrite).length, 1);
  closed(stream);
  idle(ui);
});
