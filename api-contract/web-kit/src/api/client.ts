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
  const { data, error } = await api.PUT("/api/v1/posts/{post_id}", {
    params: { path: { post_id: postId } },
    body: { code },
  });
  if (error) {
    switch (error.code) {
      case "validation_error":
        return { fields: fieldMessages(error.errors, t) };
      case "post_code_taken":
        return { fields: { code: problemText(error, t) } };
      default:
        return { message: problemText(error, t) };
    }
  }
  return { post: data };
}
