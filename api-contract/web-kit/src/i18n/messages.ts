import type { FieldErrorCode, ProblemCode } from "../api/client";

/**
 * Translations of the API errors, typed by the generated schema. A locale written as
 *
 *   export default { problems: { ... }, field_errors: { ... } } satisfies Messages;
 *
 * fails `tsc` on every code it misses and on every code the API no longer has. Placeholders `{param}` are
 * the members of the problem body (see the component schema of the code). pydantic's own field codes
 * (missing, string_too_short, ...) are optional: without a translation the body's English `detail` is shown.
 */
export type Messages = {
  problems: Record<ProblemCode, string>;
  field_errors: Record<FieldErrorCode, string>;
  builtin_field_errors?: Record<string, string>;
  /**
   * Errors the API did not answer (no application/problem+json: nginx, a proxy) and requests that got no answer.
   * Placeholders: {status}, {request_id}. by_status overrides http_error for a status (413, 502, 503, 504, ...).
   */
  infrastructure: {
    http_error: string;
    network_error: string;
    by_status?: Partial<Record<number, string>>;
  };
};
