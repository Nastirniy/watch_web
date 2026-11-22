// Real-time events (API.md, "Real-time events"): a starter, copy it into your client. No dependency beyond the
// generated types. Events are hints to refetch, never data: the app works the same without them.
import type { components } from "./schema";

export type StreamMessage = components["schemas"]["StreamMessage"];
export type DomainEvent = Extract<StreamMessage, { id: string; refetch_within_ms: number }>;
/** Send it with every request the user did not cause: refetches events ask for, pollers, the refresh for a stream */
export const BACKGROUND = { "X-Activity": "background" } as const;

export type Refetch = (run: () => void) => void;
export interface EventsOptions {
  url: string;                                        // the stream's path, e.g. "/api/v1/events/stream"
  token: () => string | null;                         // the current access token
  refresh: () => Promise<boolean>;                    // refresh with BACKGROUND; false: the session is over
  onEvent: (event: DomainEvent, refetch: Refetch) => void;
  onReload: (refetch: Refetch) => void;               // refetch everything on screen
  onStop?: (why: string) => void;
  // for tests and special hosts
  fetch?: typeof fetch;
  random?: () => number;
  protocol?: () => string | undefined;
  hidden?: () => boolean;
  onVisible?: (run: () => void) => void;
}
export interface SseBlock { id?: string; event?: string; data?: string; retry?: number }

/** WHATWG server-sent events: blocks end at an empty line, a chunk may end in the middle of a line */
export class SseParser {
  private rest = "";
  private block: SseBlock = {};
  private data: string[] = [];
  push(text: string): SseBlock[] {
    const out: SseBlock[] = [];
    const lines = (this.rest + text).split(/\r\n|\r|\n/);
    this.rest = lines.pop() ?? "";
    for (const line of lines) {
      if (line === "") {
        if (this.data.length) this.block.data = this.data.join("\n");
        if (Object.keys(this.block).length) out.push(this.block);
        this.block = {};
        this.data = [];
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const name = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (name === "data") this.data.push(value);
      else if (name === "event") this.block.event = value;
      else if (name === "id" && !value.includes("\0")) this.block.id = value;
      else if (name === "retry" && /^\d+$/.test(value)) this.block.retry = Number(value);
    }
    return out;
  }
}

const FIRST_ANSWER_MS = 120_000;   // nginx delays connects under load
const SILENCE_MS = 65_000;         // three heartbeats and a little
const SEEN_MAX = 2000;

export function startEvents(o: EventsOptions): { stop: () => void } {
  const doFetch = o.fetch ?? fetch;
  const random = o.random ?? Math.random;
  const hidden = o.hidden ?? (() => document.hidden);
  const protocol = o.protocol ?? (() => (performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined)?.nextHopProtocol);
  let position: string | null = null;      // per tab, per login, in memory: never in localStorage
  let retry = 3000;
  let failures = 0;
  let stopped = false;
  let controller: AbortController | null = null;
  const seen = new Set<string>();
  const stale = new Set<() => void>();

  const stop = (why: string) => { if (!stopped) { stopped = true; controller?.abort(); o.onStop?.(why); } };
  const spread = (within: number): Refetch => (run) => {
    if (hidden()) { stale.add(run); return; }
    setTimeout(run, random() * within);
  };
  o.onVisible?.(() => { const runs = [...stale]; stale.clear(); runs.forEach((run) => run()); });
  // On HTTP/1.1 a browser holds 6 connections per server: at 6 tabs every request of the app would hang
  if (protocol() !== "h2") { stop("not_http2"); return { stop: () => stop("stopped") }; }

  const later = (ms: number) => { if (!stopped) setTimeout(connect, ms); };
  const backoff = () => { failures += 1; later(retry + random() * (Math.min(60_000, retry * 2 ** failures) - retry)); };

  function handle(block: SseBlock): "go" | "end" {
    if (block.id !== undefined) position = block.id;             // the heartbeat's too
    if (block.retry !== undefined) retry = block.retry;
    if (!block.data) return "go";
    const message = JSON.parse(block.data) as StreamMessage;
    switch (message.type) {
      case "ready": case "heartbeat": return "go";
      case "reload": o.onReload(spread(message.refetch_within_ms)); return "go";
      case "close":
        if (message.data.reason === "session_ended") stop("session_ended");
        else if (message.data.reason === "token_expired") o.refresh().then((ok) => (ok ? later(0) : stop("session_ended")));
        else later(retry);                                       // restart: the retry it carried
        return "end";
      default:
        if (seen.has(message.id)) return "go";                   // at least once: a repeat is skipped
        seen.add(message.id);
        if (seen.size > SEEN_MAX) seen.delete(seen.values().next().value as string);
        o.onEvent(message, spread(message.refetch_within_ms));
        return "go";
    }
  }

  async function connect(): Promise<void> {
    if (stopped) return;
    controller = new AbortController();
    const current = controller;
    let watchdog = setTimeout(() => current.abort(), FIRST_ANSWER_MS);
    const headers: Record<string, string> = { Accept: "text/event-stream", Authorization: `Bearer ${o.token() ?? ""}` };
    if (position !== null) headers["Last-Event-ID"] = position;
    try {
      const response = await doFetch(o.url, { headers, signal: current.signal, cache: "no-store" });
      clearTimeout(watchdog);
      if (response.status === 204) { stop("events_off"); return; }
      if (response.status === 401) { stop("unauthorized"); return; }   // never reconnect on a 401
      if (response.status !== 200 || !response.body) { backoff(); return; }
      failures = 0;
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      const parser = new SseParser();
      for (;;) {
        clearTimeout(watchdog);
        watchdog = setTimeout(() => current.abort(), SILENCE_MS);
        const { value, done } = await reader.read();
        if (done) break;
        for (const block of parser.push(value)) if (handle(block) === "end") { clearTimeout(watchdog); current.abort(); return; }
      }
      clearTimeout(watchdog);
      later(retry);                                              // the stream may end at any time
    } catch {
      clearTimeout(watchdog);
      if (!stopped) backoff();                                   // network error or watchdog
    }
  }

  void connect();
  return { stop: () => stop("stopped") };
}
