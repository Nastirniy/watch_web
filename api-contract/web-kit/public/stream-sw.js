// Starter service worker: gives <video> and <audio> elements the Bearer token (WEB_TOOLING.md, section 10).
//
// A media element cannot send an Authorization header, but it sends its Range requests itself. For
// GET /api/v1/media/<id>/stream this worker sends the request again with the same Range header plus the token
// and returns that answer (206 passes through untouched). Serve this file from the root of the web client's
// origin (Vite: public/), so its scope covers /api/.
//
// The token is held in memory only. The page sends it with postMessage({type: "token", token}) after every
// login and refresh (sendStreamToken in src/api/streamWorker.ts); the worker asks the pages for one with
// {type: "need-token", expired} after a restart (the browser stops idle workers) or a 401.

const STREAM_PATH = /^\/api\/v1\/media\/[^/]+\/stream$/;
const TOKEN_WAIT_MS = 5000;

let token = null;
let waiting = [];

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "token") return;
  token = data.token || null;
  const pending = waiting;
  waiting = [];
  for (const resolve of pending) resolve(token);
});

// Ask the open pages for a token and wait for the answer (null after TOKEN_WAIT_MS).
async function askForToken(expired) {
  const answer = new Promise((resolve) => {
    waiting.push(resolve);
    setTimeout(() => resolve(null), TOKEN_WAIT_MS);
  });
  const pages = await self.clients.matchAll({ type: "window" });
  for (const page of pages) page.postMessage({ type: "need-token", expired });
  return answer;
}

function authorised(request, value) {
  const headers = new Headers();
  const range = request.headers.get("range");
  if (range) headers.set("range", range);
  headers.set("authorization", `Bearer ${value}`);
  return fetch(request.url, { method: request.method, headers, credentials: "same-origin", cache: "no-store" });
}

async function stream(request) {
  if (!token) await askForToken(false);
  if (!token) return fetch(request);           // no token: the API answers 401, the page handles it
  const used = token;
  const first = await authorised(request, used);
  if (first.status !== 401) return first;
  await askForToken(true);                      // the access token expired: one refresh, one retry
  if (!token || token === used) return first;
  return authorised(request, token);
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" && request.method !== "HEAD") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !STREAM_PATH.test(url.pathname)) return;
  // A request that carries its own credentials is none of this worker's business
  if (request.headers.has("authorization")) return;
  event.respondWith(stream(request));
});
