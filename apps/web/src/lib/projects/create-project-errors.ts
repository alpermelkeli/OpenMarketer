/** Where on the "new project" form each failure of `createProject` is shown. */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

export type CreateProjectErrors = {
  name: readonly string[];
  repositoryUrl: readonly string[];
  /** Failures that are about neither field. */
  form: readonly string[];
  /** The project that already has this repository, when that is why creating failed. */
  existingProjectId: string | null;
};

export const NO_ERRORS: CreateProjectErrors = { name: [], repositoryUrl: [], form: [], existingProjectId: null };

export function createProjectErrors(error: ApiError | null): CreateProjectErrors {
  if (error === null) return NO_ERRORS;

  switch (error.code) {
    case "invalid_repository_url":
      return { ...NO_ERRORS, repositoryUrl: [error.message] };
    case "project_already_exists":
      return {
        ...NO_ERRORS,
        repositoryUrl: ["A project for this repository already exists."],
        existingProjectId: error.existingProjectId,
      };
    case "request_invalid": {
      const { name = [], repository_url = [], ...rest } = error.fieldErrors;
      return { ...NO_ERRORS, name, repositoryUrl: repository_url, form: Object.values(rest).flat() };
    }
    default:
      return { ...NO_ERRORS, form: [errorMessage(error)] };
  }
}
