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
  chat_not_found: "The project has no chat with this ID.",
  studio_run_used_by_post:
    "A post of this project goes with this run's image, so the run is kept as long as the post is. It is deleted only with the project.",
  research_used_by_post:
    "A post of this project drew on this run's findings, so the run is kept as long as the post is. It is deleted only with the project.",
  model_key_not_set:
    "No key of the model provider is set, so the orchestrator cannot answer. Store one in Settings, under Credentials.",
};

export function errorMessage(error: ApiError): string {
  return ADVICE[error.code] ?? error.message;
}
