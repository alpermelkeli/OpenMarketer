/** What to tell the reviewer when saving an edit or approving a version fails. */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

/** One line per problem. A validation failure names the part of the profile it is about. */
export function saveEditProblems(error: ApiError): string[] {
  if (error.code === "request_invalid") {
    const lines = Object.entries(error.fieldErrors).flatMap(([field, messages]) =>
      messages.map((message) => `${field.replace(/^profile\./, "")}: ${message}`),
    );
    if (lines.length > 0) return lines;
  }
  if (error.code === "profile_not_found") {
    return ["The version you were editing no longer exists on the server. Reload the page."];
  }
  return [errorMessage(error)];
}

export function approveProblem(error: ApiError): string {
  switch (error.code) {
    case "profile_already_approved":
      return "This version has already been approved, perhaps in another tab. Close this dialog to see the current state.";
    case "profile_not_found":
      return "The API no longer has this version. Close this dialog and reload the page.";
    default:
      return errorMessage(error);
  }
}
