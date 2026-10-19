/**
 * Page side of the starter service worker public/stream-sw.js (WEB_TOOLING.md, section 10): <video> and <audio>
 * elements stream GET /api/v1/media/{media_id}/stream with the Bearer token.
 *
 * Call registerStreamWorker once at start-up and sendStreamToken after every login and refresh (null at logout).
 */

type WorkerMessage = { type?: string; expired?: boolean };

/** Hands the current token to the controlling worker, if there is one yet */
export function sendStreamToken(token: string | null): void {
  navigator.serviceWorker?.controller?.postMessage({ type: "token", token });
}

/**
 * Registers the worker at the root scope and answers its requests for a token with getToken(expired): expired is
 * true after a 401, when the token it had no longer works (refresh it first). null where the browser has no
 * service workers (the player then needs another way to send the token, e.g. fetching the media itself).
 */
export async function registerStreamWorker(
  getToken: (expired: boolean) => Promise<string | null>,
  scriptUrl = "/stream-sw.js",
): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  navigator.serviceWorker.addEventListener("message", async (event: MessageEvent<WorkerMessage>) => {
    if (event.data?.type !== "need-token") return;
    const token = await getToken(event.data.expired === true);
    (event.source as ServiceWorker | null)?.postMessage({ type: "token", token });
  });
  const registration = await navigator.serviceWorker.register(scriptUrl, { scope: "/" });
  await navigator.serviceWorker.ready;
  return registration;
}
