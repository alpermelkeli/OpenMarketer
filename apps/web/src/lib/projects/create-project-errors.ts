/** Where on the "new project" form each failure of `createProject` is shown. */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

export type CreateProjectErrors = {
  name: readonly string[];
  repositoryUrl: readonly string[];
  /** Failures that are about neither field. */
  form: readonly string[];
};

export const NO_ERRORS: CreateProjectErrors = { name: [], repositoryUrl: [], form: [] };

export function createProjectErrors(error: ApiError | null): CreateProjectErrors {
  if (error === null) return NO_ERRORS;

  switch (error.code) {
    case "invalid_repository_url":
      return { ...NO_ERRORS, repositoryUrl: [error.message] };
    case "project_already_exists":
      return {
        ...NO_ERRORS,
        repositoryUrl: [
          "A project for this repository already exists. The API cannot tell the dashboard which one yet; if you know its ID, open it by ID below.",
        ],
      };
    case "request_invalid": {
      const { name = [], repository_url = [], ...rest } = error.fieldErrors;
      return { name, repositoryUrl: repository_url, form: Object.values(rest).flat() };
    }
    default:
      return { ...NO_ERRORS, form: [errorMessage(error)] };
  }
}
