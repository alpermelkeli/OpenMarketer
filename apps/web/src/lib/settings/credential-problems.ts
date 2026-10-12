/** Where in a credential's row each failure of `setCredential` is shown. */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

export type SetCredentialProblems = {
  /** What the API said about the value, shown beside the field. Never the value itself. */
  value: readonly string[];
  /** Failures that are not about the value: a conflict, an unknown name, the API being down. */
  row: readonly string[];
};

export const NO_PROBLEMS: SetCredentialProblems = { value: [], row: [] };

export function setCredentialProblems(error: ApiError | null): SetCredentialProblems {
  if (error === null) return NO_PROBLEMS;
  if (error.code === "request_invalid") {
    const { value = [], ...rest } = error.fieldErrors;
    const other = Object.values(rest).flat();
    // A 422 that names nothing still has to say that the value was not stored.
    return value.length + other.length > 0 ? { value, row: other } : { value: [], row: [errorMessage(error)] };
  }
  return { value: [], row: [errorMessage(error)] };
}
