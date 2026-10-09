/**
 * One error type for everything a request to the API can answer with: the API's
 * `Problem`, FastAPI's request validation (422), a refusal of the dashboard's own
 * proxy, and a response that is none of these.
 */

import { PROXY_ERROR_CODES, type ProxyErrorCode } from "./proxy-problem";
import type { ErrorCode } from "./types";

export type ApiErrorCode = ErrorCode | ProxyErrorCode | "request_invalid" | "unexpected_response";

/** Validation messages by the request field they are about (`name`, `repository_url`). */
export type FieldErrors = Readonly<Record<string, readonly string[]>>;

// Written as a record so that a code added to the contract fails the type check here.
const API_ERROR_CODES: Record<ErrorCode, true> = {
  invalid_repository_url: true,
  not_local_request: true,
  project_not_found: true,
  analysis_not_found: true,
  profile_not_found: true,
  project_already_exists: true,
  analysis_already_running: true,
  profile_already_approved: true,
  internal_error: true,
  analysis_not_started: true,
  database_unavailable: true,
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: ApiErrorCode,
    message: string,
    readonly fieldErrors: FieldErrors = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn the body of a failed response into an `ApiError`. The body is untrusted JSON. */
export function apiErrorFrom(status: number, body: unknown): ApiError {
  if (isRecord(body)) {
    const { code, message, detail } = body;
    if (typeof code === "string" && typeof message === "string" && isKnownCode(code)) {
      return new ApiError(status, code, message);
    }
    if (Array.isArray(detail)) {
      return new ApiError(status, "request_invalid", "The request was not valid.", fieldErrorsOf(detail));
    }
  }
  return new ApiError(status, "unexpected_response", `The API answered ${status} without an explanation.`);
}

function isKnownCode(code: string): code is ErrorCode | ProxyErrorCode {
  return code in API_ERROR_CODES || (PROXY_ERROR_CODES as readonly string[]).includes(code);
}

function fieldErrorsOf(detail: readonly unknown[]): FieldErrors {
  const errors: Record<string, string[]> = {};
  for (const item of detail) {
    if (!isRecord(item) || typeof item.msg !== "string" || !Array.isArray(item.loc)) continue;
    // `loc` is ["body", "name"] for a field and ["body"] for the body as a whole.
    const path = item.loc.filter((part) => part !== "body").map(String);
    const field = path.length > 0 ? path.join(".") : "body";
    (errors[field] ??= []).push(item.msg);
  }
  return errors;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
