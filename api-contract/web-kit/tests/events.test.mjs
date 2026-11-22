// The events starter against a fake server: node --test api-contract/web-kit/tests/ (Node 22.18+ runs the .ts file
// directly: its types are only stripped)
import { test } from "node:test";
import assert from "node:assert/strict";
import { SseParser, startEvents } from "../src/api/events.ts";

const encoder = new TextEncoder();
const sse = (id, type, extra = {}, more = "") => `id: ${id}\n${more}event: ${type}\ndata: ${JSON.stringify({ type, ...extra })}\n\n`;
const event = (id, position, type = "move.changed") =>
  sse(position, type, { id: String(id), v: 1, occurred_at: "2026-11-22T10:00:00Z", data: {}, refetch_within_ms: 0 });

/** A server: each connect takes the next answer, a status or a list of chunks (the stream then ends) */
function server(answers) {
  const requests = [];
  const fetch = async (url, init) => {
    requests.push({ url, headers: init.headers });
    const answer = answers.shift() ?? 503;
    if (typeof answer === "number") return new Response(answer === 204 ? null : "x", { status: answer });
    const body = new ReadableStream({
      start(controller) { for (const chunk of answer) controller.enqueue(encoder.encode(chunk)); controller.close(); },
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  };
  return { fetch, requests };
}

function start(fetch, overrides = {}) {
  const seen = { events: [], reloads: 0, stops: [], refreshes: 0 };
  const handle = startEvents({
    url: "/stream", token: () => "t", fetch, random: () => 0, protocol: () => "h2", hidden: () => false,
    refresh: async () => { seen.refreshes += 1; return true; },
    onEvent: (message, refetch) => refetch(() => seen.events.push(message.id)),
    onReload: (refetch) => refetch(() => { seen.reloads += 1; }),
    onStop: (why) => seen.stops.push(why),
    ...overrides,
  });
  return { seen, handle };
}

const until = async (condition, ms = 2000) => {
  const deadline = Date.now() + ms;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};

test("the parser follows WHATWG: split chunks, CRLF, comments, several data lines, id and retry rules", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push("id: 7\r\nevent: x\ndata: a"), []);
  assert.deepEqual(parser.push("\ndata: b\n: a comment\nretry: 1x\n\n"), [{ id: "7", event: "x", data: "a\nb" }]);
  assert.deepEqual(parser.push("id: a\0b\nretry: 250\ndata:c\n\n"), [{ retry: 250, data: "c" }]);
});

test("only on HTTP/2", async () => {
  const { fetch, requests } = server([]);
  const { seen } = start(fetch, { protocol: () => "http/1.1" });
  assert.deepEqual(seen.stops, ["not_http2"]);
  assert.equal(requests.length, 0);
});

test("204 and 401 stop for good", async () => {
  for (const [status, why] of [[204, "events_off"], [401, "unauthorized"]]) {
    const { fetch, requests } = server([status]);
    const { seen } = start(fetch);
    await until(() => seen.stops.length === 1);
    assert.deepEqual(seen.stops, [why]);
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(requests.length, 1, "no reconnect");
  }
});

test("events once, the position of every block, sent back on reconnect", async () => {
  const { fetch, requests } = server([
    [sse(1, "ready", {}, "retry: 10\n"), event(41, 2), event(41, 2), sse(3, "heartbeat")],
    [sse(3, "ready"), event(42, 4)],
  ]);
  const { seen, handle } = start(fetch);
  await until(() => seen.events.length === 2);
  assert.deepEqual(seen.events, ["41", "42"]);
  assert.equal(requests[0].headers["Last-Event-ID"], undefined);
  assert.equal(requests[1].headers["Last-Event-ID"], "3", "the heartbeat moved the position");
  assert.equal(requests[1].headers.Authorization, "Bearer t");
  handle.stop();
});

test("every domain event type reaches onEvent with its data", async () => {
  const mismatch = sse(3, "security.certificate_mismatch", {
    id: "43", v: 1, occurred_at: "2026-11-22T10:00:00Z", scope: { kind: "root" },
    data: { audit_log_id: 12, session_user_id: 5 }, refetch_within_ms: 0,
  });
  const { fetch } = server([[sse(1, "ready"), event(41, 2, "scope.changed"), mismatch]]);
  const received = [];
  const { handle } = start(fetch, { onEvent: (message) => received.push([message.type, message.data]) });
  await until(() => received.length === 2);
  assert.deepEqual(received, [["scope.changed", {}],
    ["security.certificate_mismatch", { audit_log_id: 12, session_user_id: 5 }]]);
  handle.stop();
});

test("a reload refetches what is on screen", async () => {
  const { fetch } = server([[sse(1, "ready"), sse(1, "reload", { refetch_within_ms: 0 })]]);
  const { seen, handle } = start(fetch);
  await until(() => seen.reloads === 1);
  handle.stop();
});

test("close: token_expired refreshes and reconnects, session_ended stops", async () => {
  const { fetch, requests } = server([
    [sse(1, "ready"), sse(1, "close", { data: { reason: "token_expired" } })],
    [sse(1, "ready"), sse(1, "close", { data: { reason: "session_ended" } })],
  ]);
  const { seen } = start(fetch);
  await until(() => seen.stops.length === 1);
  assert.equal(seen.refreshes, 1);
  assert.equal(requests.length, 2);
  assert.deepEqual(seen.stops, ["session_ended"]);
});

test("503 backs off and tries again", async () => {
  const { fetch, requests } = server([[sse(1, "ready", {}, "retry: 10\n")], 503, [sse(1, "ready"), event(7, 2)]]);
  const { seen, handle } = start(fetch);
  await until(() => seen.events.length === 1);
  assert.equal(requests.length, 3);
  handle.stop();
});

test("a hidden tab marks itself stale and refetches when it is shown", async () => {
  let visible = () => {};
  let hidden = true;
  const { fetch } = server([[sse(1, "ready"), event(5, 2)]]);
  const { seen, handle } = start(fetch, { hidden: () => hidden, onVisible: (run) => { visible = run; } });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(seen.events, []);
  hidden = false;
  visible();
  assert.deepEqual(seen.events, ["5"]);
  handle.stop();
});
