import assert from "node:assert/strict";
import test from "node:test";
import {readAnswerStream} from "../stream.mjs";

function response(text, size = 1) {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  return new Response(new ReadableStream({pull(controller) {
    if (offset === bytes.length) return controller.close();
    controller.enqueue(bytes.slice(offset, offset += size));
  }}), {headers: {"content-type": "text/event-stream"}});
}

test("handles split CRLF, comments and multibyte characters without exposing metadata", async () => {
  const source = ': ping\r\n\r\nevent: text_delta\r\ndata: {"text":"Café"}\r\n\r\n' +
    'event: done\r\ndata: {"reply":"Café","request_id":"sample"}\r\n\r\n';
  const parts = [];
  const result = await readAnswerStream(response(source), (text) => parts.push(text));
  assert.equal(parts.join(""), "Café");
  assert.equal(result.request_id, "sample");
});

test("a failure after partial text never becomes a completed reply", async () => {
  const source = 'event: text_delta\ndata: {"text":"Partial"}\n\n' +
    'event: error\ndata: {"message":"The service is unavailable."}\n\n';
  let partial = "";
  await assert.rejects(readAnswerStream(response(source), (text) => {partial += text;}), /unavailable/);
  assert.equal(partial, "Partial");
});

test("a reset discards superseded narration and validates only the final draft", async () => {
  const source = 'event: text_delta\ndata: {"text":"Let me check."}\n\n' +
    'event: text_reset\ndata: {}\n\n' +
    'event: text_delta\ndata: {"text":"The ticket is open."}\n\n' +
    'event: done\ndata: {"reply":"The ticket is open.","request_id":"sample"}\n\n';
  let draft = "";
  let resets = 0;
  const result = await readAnswerStream(response(source), text => { draft += text; }, () => { draft = ""; resets++; });
  assert.equal(resets, 1);
  assert.equal(draft, result.reply);
  assert.equal(draft, "The ticket is open.");
});

for (const [name, source] of [
  ["missing completion", 'event: text_delta\ndata: {"text":"Partial"}\n\n'],
  ["mismatched completion", 'event: done\ndata: {"reply":"Invented"}\n\n'],
  ["unexpected tool payload", 'event: tool\ndata: {"secret":"never render"}\n\n'],
  ["malformed JSON", 'event: text_delta\ndata: {broken}\n\n'],
  ["data after completion", 'event: text_delta\ndata: {"text":"OK"}\n\nevent: done\ndata: {"reply":"OK"}\n\nevent: text_delta\ndata: {"text":"bad"}\n\n'],
]) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(readAnswerStream(response(source), () => {}));
  });
}

test("reports an HTTP authentication error before reading any answer", async () => {
  await assert.rejects(readAnswerStream(new Response('{"message":"Invalid credentials."}', {
    status: 401, headers: {"content-type": "application/json"},
  }), () => assert.fail("No answer expected")), /Invalid credentials/);
});
