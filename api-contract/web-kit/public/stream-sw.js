// Starter service worker: gives <video> and <audio> elements the Bearer token (WEB_TOOLING.md, section 10).
//
// A media element cannot send an Authorization header, but it sends its Range requests itself. For
// GET /api/v1/media/<id>/stream this worker sends the request again with the same Range header plus the token
// and returns that answer (206 passes through untouched). Serve this file from the root of the web client's
// origin (Vite: public/), so its scope covers /api/.
//
// The token is held in memory only. The page sends it with postMessage({type: "token", token}) after every
// login and refresh (sendStreamToken in src/api/streamWorker.ts); the worker asks the page that plays the media
// for one with {type: "need-token", expired} after a restart (the browser stops idle workers) or a 401. Streams
// that need a token at the same time share one request: refresh tokens work once, a second refresh would fail.
//
// Only the requests of <video> and <audio> elements get the token: never a page load or an iframe of the URL
// (another site could embed it), never fetch() calls (they send their own Authorization header).

const STREAM_PATH = /^\/api\/v1\/media\/[^/]+\/stream$/;
const TOKEN_WAIT_MS = 5000;

const MEDIA_DESTINATIONS = new Set(["video", "audio"]);

let token = null;
let waiting = [];
let asking = null;

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

// Ask the page that plays the media for a token (every open page if it is gone) and wait for the answer (null
// after TOKEN_WAIT_MS). One question at a time: callers that come while it is open wait for the same answer.
function askForToken(clientId, expired) {
  if (asking) return asking;
  asking = (async () => {
    const answer = new Promise((resolve) => {
      waiting.push(resolve);
      setTimeout(() => resolve(null), TOKEN_WAIT_MS);
    });
    const page = clientId ? await self.clients.get(clientId) : null;
    const pages = page ? [page] : await self.clients.matchAll({ type: "window" });
    for (const each of pages) each.postMessage({ type: "need-token", expired });
    return answer;
  })().finally(() => {
    asking = null;
  });
  return asking;
}

function authorised(request, value) {
  const headers = new Headers();
  const range = request.headers.get("range");
  if (range) headers.set("range", range);
  headers.set("authorization", `Bearer ${value}`);
  return fetch(request.url, { method: request.method, headers, credentials: "same-origin", cache: "no-store" });
}

async function stream(request, clientId) {
  if (!token) await askForToken(clientId, false);
  if (!token) return fetch(request);           // no token: the API answers 401, the page handles it
  const used = token;
  const first = await authorised(request, used);
  if (first.status !== 401) return first;
  if (token === used) await askForToken(clientId, true);   // expired: one refresh for every stream, one retry
  if (!token || token === used) return first;
  return authorised(request, token);
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" && request.method !== "HEAD") return;
  if (request.mode === "navigate" || !MEDIA_DESTINATIONS.has(request.destination)) return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !STREAM_PATH.test(url.pathname)) return;
  // A request that carries its own credentials is none of this worker's business
  if (request.headers.has("authorization")) return;
  event.respondWith(stream(request, event.clientId));
});
