/** What to tell the person when an analysis could not be started. */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

export type StartAnalysisProblem = { title: string; text: string };

export function startAnalysisProblem(error: ApiError): StartAnalysisProblem {
  switch (error.code) {
    case "analysis_already_running":
      return {
        title: "An analysis is already running for this project",
        text: "A project has one unfinished analysis at a time, so the model is not paid twice. If it was started somewhere else it is not listed here: the API cannot list a project's runs yet. Try again when it has finished.",
      };
    case "analysis_not_started":
      return {
        title: "The analysis could not be handed to a worker",
        text: "The API could not reach Temporal, so the run was stored as failed and nothing was analysed. Start the dev stack with `make up` and a worker with `make worker`, then start a new analysis.",
      };
    case "invalid_repository_url":
      return { title: "This project's repository cannot be analysed", text: error.message };
    default:
      return { title: "The analysis was not started", text: errorMessage(error) };
  }
}
