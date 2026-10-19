// The starter service worker in a sandbox: node --test api-contract/web-kit/tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../public/stream-sw.js", import.meta.url), "utf8");
const ORIGIN = "https://watch.example";
const STREAM = `${ORIGIN}/api/v1/media/7/stream`;

// A worker with two open pages; fetch answers from `answer(authorization)`
function worker(answer) {
  const listeners = {};
  const sent = [];
  const fetched = [];
  const page = (id) => ({ id, postMessage: (data) => sent.push({ to: id, data }) });
  const pages = [page("a"), page("b")];
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    skipWaiting() {},
    clients: {
      claim: async () => {},
      matchAll: async () => pages,
      get: async (id) => pages.find((p) => p.id === id),
    },
  };
  const fetch = async (url, init = {}) => {
    const authorization = init.headers ? init.headers.get("authorization") : null;
    fetched.push(authorization);
    return { status: answer(authorization) };
  };
  vm.runInNewContext(SOURCE, { self, fetch, Headers, URL, setTimeout, Promise });
  const give = (token) => listeners.message({ data: { type: "token", token } });
  const request = (options = {}) => {
    const event = {
      clientId: options.clientId ?? "a",
      request: { method: "GET", url: STREAM, headers: new Headers(options.headers ?? {}),
        mode: options.mode ?? "no-cors", destination: options.destination ?? "video" },
      respondWith(promise) { this.answer = promise; },
    };
    listeners.fetch(event);
    return event;
  };
  return { request, give, sent, fetched };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("a page load or iframe of the stream URL gets no token", () => {
  const w = worker(() => 206);
  w.give("T1");
  assert.equal(w.request({ mode: "navigate", destination: "document" }).answer, undefined);
  assert.equal(w.request({ mode: "navigate", destination: "iframe" }).answer, undefined);
  assert.equal(w.request({ destination: "" }).answer, undefined, "fetch() calls carry their own token");
});

test("video and audio requests get the token", async () => {
  const w = worker(() => 206);
  w.give("T1");
  for (const destination of ["video", "audio"]) {
    const answer = await w.request({ destination }).answer;
    assert.equal(answer.status, 206);
  }
  assert.deepEqual(w.fetched, ["Bearer T1", "Bearer T1"]);
});

test("only the page that played the media is asked for a token", async () => {
  const w = worker(() => 206);
  const event = w.request({ clientId: "b" });
  await tick();
  assert.deepEqual(w.sent.map((m) => m.to), ["b"]);
  w.give("T1");
  assert.equal((await event.answer).status, 206);
});

test("streams refused together ask for one new token, then all retry with it", async () => {
  const w = worker((authorization) => (authorization === "Bearer OLD" ? 401 : 206));
  w.give("OLD");
  const first = w.request();
  const second = w.request();
  for (let i = 0; i < 5; i += 1) await tick();
  const asks = w.sent.filter((m) => m.data.type === "need-token");
  assert.equal(asks.length, 1, "one refresh for both streams (refresh tokens are single use)");
  // objects of the sandbox have its own prototypes: compare their JSON
  assert.equal(JSON.stringify(asks[0]), JSON.stringify({ to: "a", data: { type: "need-token", expired: true } }));
  w.give("NEW");
  assert.equal((await first.answer).status, 206);
  assert.equal((await second.answer).status, 206);
});
