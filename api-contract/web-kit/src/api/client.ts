import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

type Schemas = components["schemas"];

/** Every problem type of the API: its component schemas <Code>Problem, discriminated by `code` */
export type Problem = { [K in keyof Schemas]: K extends `${string}Problem` ? Schemas[K] : never }[keyof Schemas];
/** Every problem code: the keys of the "problems" translations */
export type ProblemCode = Problem["code"];
/** Codes of the API for invalid values: the keys of the "field_errors" translations */
export type FieldErrorCode = Schemas["FieldErrorCode"];
export type FieldError = Schemas["FieldError"];

// Origin of the API, e.g. from your build config (Vite: import.meta.env.VITE_API_URL)
const API_URL = "https://api.example";

export const api = createClient<paths>({ baseUrl: API_URL });

/** Text of a problem for the user: the translation of its code with its params, or the English detail. */
export function problemText(problem: Problem, t: (key: string, params: Record<string, unknown>) => string | undefined): string {
  return t(`problems.${problem.code}`, problem) ?? problem.detail;
}

/**
 * An error response by who answered it (API.md, "Errors"). The API answers problems (application/problem+json):
 * handle their code. Anything else with an error status comes from the infrastructure in front of the API (nginx:
 * client certificate, request size, the API not reachable or too slow): handle it by status only, never parse it.
 * requestId (X-Request-ID) is on both: show it so a report can be matched.
 */
export type ApiFailure<P> =
  | { kind: "problem"; problem: P; requestId: string | null }
  | { kind: "infrastructure"; status: number; requestId: string | null };

export function apiFailure<P>(response: Response, error: P): ApiFailure<P> {
  const requestId = response.headers.get("x-request-id");
  return (response.headers.get("content-type") ?? "").startsWith("application/problem+json")
    ? { kind: "problem", problem: error, requestId }
    : { kind: "infrastructure", status: response.status, requestId };
}

/** Text of an error the infrastructure answered: the translation for its status, else the generic one */
export function infrastructureText(
  failure: { status: number; requestId: string | null },
  t: (key: string, params: Record<string, unknown>) => string | undefined,
): string {
  const params = { status: failure.status, request_id: failure.requestId ?? "-" };
  return t(`infrastructure.by_status.${failure.status}`, params) ?? t("infrastructure.http_error", params) ?? `HTTP ${failure.status}`;
}

/** Messages of a validation_error by form field: "#/username" -> "username" */
export function fieldMessages(
  errors: FieldError[],
  t: (key: string, params: Record<string, unknown>) => string | undefined,
): Record<string, string> {
  const byField: Record<string, string> = {};
  for (const error of errors) {
    const field = error.pointer ? error.pointer.replace(/^#\//, "").replace(/\//g, ".") : error.parameter ?? "";
    byField[field] = t(`field_errors.${error.code}`, error) ?? error.detail;
  }
  return byField;
}

export async function renamePost(postId: number, code: string, t: (key: string, params: Record<string, unknown>) => string | undefined) {
  const { data, error, response } = await api.PUT("/api/v1/posts/{post_id}", {
    params: { path: { post_id: postId } },
    body: { code },
  });
  if (error) {
    const failure = apiFailure(response, error);
    if (failure.kind === "infrastructure") {
      return { message: infrastructureText(failure, t) };
    }
    const problem = failure.problem;
    switch (problem.code) {
      case "validation_error":
        return { fields: fieldMessages(problem.errors, t) };
      case "post_code_taken":
        return { fields: { code: problemText(problem, t) } };
      default:
        return { message: problemText(problem, t) };
    }
  }
  return { post: data };
}

/** Answers of the storyboard route that mean "no track to show" (see media.storyboard_status) */
const NO_STORYBOARD_TRACK: readonly number[] = [
  202, // pending or processing: ask again after Retry-After
  409, // storyboard_failed
  422, // storyboard_unsupported
];

/**
 * Seek-preview track of a video for <track kind="metadata">. The route needs the bearer token, a <track src> cannot
 * send it: fetch the WebVTT and hand the player a blob URL (the sheet URLs inside are absolute, so they resolve from
 * it). null when there is no track to show: 202 (not generated yet), 409 storyboard_failed, 422
 * storyboard_unsupported. Any other answer (401, 403, 404, 5xx, the infrastructure) is an ApiFailure, handled like
 * the failure of any other request. The sheet URLs are signed and expire, so ask again (and revoke the old blob URL)
 * when the page lives longer than the Cache-Control max-age.
 */
export async function storyboardTrack(mediaId: number, token: string): Promise<string | null | ApiFailure<Problem>> {
  const response = await fetch(`${API_URL}/api/v1/media/${mediaId}/storyboard`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (response.status === 200) {
    const text = await response.text();
    return URL.createObjectURL(new Blob([text], { type: "text/vtt" }));
  }
  if (NO_STORYBOARD_TRACK.includes(response.status)) {
    return null;
  }
  // Only the API's problems are parsed; an infrastructure answer is handled by its status (apiFailure)
  const isProblem = (response.headers.get("content-type") ?? "").startsWith("application/problem+json");
  const problem = isProblem ? ((await response.json()) as Problem) : undefined;
  return apiFailure(response, problem as Problem);
}
