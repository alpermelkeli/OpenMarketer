/**
 * What to tell a person about a failed request when the screen has nothing more
 * specific to say. Where the dashboard knows how to fix the cause it says so;
 * otherwise the API's own message is shown, as text.
 */

import type { ApiError, ApiErrorCode } from "./api-error";

const ADVICE: Partial<Record<ApiErrorCode, string>> = {
  api_unreachable: "The OpenMarketer API is not answering. Start it with `make api`, then try again.",
  database_unavailable:
    "The API cannot reach its database. Start the dev stack with `make up` and apply migrations with `make migrate`.",
  proxy_request_refused:
    "The dashboard refused this request because it did not come from this page on this machine. Open the dashboard at http://localhost:3000.",
  not_local_request:
    "The API refused the request: without a login it only serves requests made on the machine it runs on.",
  internal_error: "The API ran into an error it did not expect. Its log has the details.",
  project_not_found: "The API has no project with this ID.",
};

export function errorMessage(error: ApiError): string {
  return ADVICE[error.code] ?? error.message;
}
