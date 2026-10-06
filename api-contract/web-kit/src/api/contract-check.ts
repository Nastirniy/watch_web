// Compile-time checks of the API contract. Not imported by the app: `tsc --noEmit` checks it.
// Each block fails the build when the API changes in a way the web client must react to.
import type { components, paths } from "./schema";
import type { Problem, ProblemCode } from "./client";

type Schemas = components["schemas"];

// 1. Every 401 code is handled. A new 401 code in the API -> "Function lacks ending return statement".
type Unauthorized = paths["/api/v1/posts/{post_id}"]["put"]["responses"][401]["content"]["application/problem+json"];
export function onUnauthorized(problem: Unauthorized): "refresh" | "login" {
  switch (problem.code) {
    case "invalid_token":            // only the access token expired: the session lives on
      return "refresh";
    case "session_expired":          // the session itself ended: a refresh answers session_expired again
    case "credentials_required":
    case "token_certificate_mismatch":
    case "account_not_found":
    case "account_deactivated":
    case "account_deleted":
      return "login";
  }
}

// 2. Params are typed per code. A renamed or retyped param in the API -> type error here and at every use.
export function uploadOffset(problem: Schemas["UploadOffsetMismatchProblem"]): number {
  return problem.offset;
}

// 3. The generated types are real, not `any`: each line below MUST be an error.
//    If one stops being an error, tsc fails with "Unused '@ts-expect-error' directive".
// @ts-expect-error: not a code of the API
export const notACode: ProblemCode = "not_a_code";
// @ts-expect-error: status is a constant of the problem type
export const wrongStatus: Schemas["PostNotFoundProblem"]["status"] = 400;
export function wrongParam(problem: Problem): unknown {
  if (problem.code === "post_not_found") {
    // @ts-expect-error: post_not_found has no name
    return problem.name;
  }
  return undefined;
}
