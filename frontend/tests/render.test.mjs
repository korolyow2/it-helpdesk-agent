import assert from "node:assert/strict";
import test from "node:test";
import {renderReply} from "../render.mjs";

test("a batched response becomes visible before its final fragment", async () => {
  const text = "The proposed ticket is ready for your review.";
  const visible = [];
  let resume;
  let paused;
  const waiting = new Promise(resolve => { paused = resolve; });
  let first = true;
  const rendering = renderReply(text, value => { visible.push(value.text); }, {
    pause: () => {
      if (!first) return Promise.resolve();
      first = false;
      paused();
      return new Promise(resolve => { resume = resolve; });
    },
  });
  await waiting;
  assert.ok(visible.join("").length > 0);
  assert.notEqual(visible.join(""), text);
  resume();
  await rendering;
  assert.equal(visible.join(""), text);
});

test("replacement overwrites once and preserves Unicode text", async () => {
  const text = "✅ Your ticket is ready. Café laptop 🧑‍💻";
  const events = [];
  await renderReply(text, value => { events.push(value); }, {overwrite: true, pause: async () => {}});
  assert.equal(events.filter(value => value.overwrite).length, 1);
  assert.equal(events[0].overwrite, true);
  assert.equal(events.map(value => value.text).join(""), text);
  assert.ok(events.every(value => !value.text.includes("\uFFFD")));
});

test("reduced-motion or hidden-page rendering can deliver the original chunk immediately", async () => {
  const text = "The complete answer is available without animation.";
  const events = [];
  await renderReply(text, value => { events.push(value); }, {
    animate: false, overwrite: true, pause: () => assert.fail("No animation delay expected."),
  });
  assert.deepEqual(events, [{text, overwrite: true}]);
});
