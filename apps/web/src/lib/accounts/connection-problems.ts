/**
 * What to tell a person when connecting an account fails: on the accounts screen, when a
 * connection cannot be started or an account cannot be disconnected, and on the callback
 * page, when the platform's answer cannot be turned into a connection. Where the dashboard
 * knows the cause it says what to do; otherwise the API's own message is shown, as text.
 *
 * Sentences are the dashboard's own wording, with names between backticks.
 */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

const START_AGAIN = "Start again from the project's Accounts page.";

/** Why a connection to `platformName` could not be started. */
export function connectProblem(error: ApiError, platformName: string): string {
  switch (error.code) {
    case "platform_app_not_set":
      return `The installation's ${platformName} app is missing a credential. Set it in Settings, under Credentials, then connect again.`;
    case "encryption_key_missing":
      return "The API has no `ENCRYPTION_KEY`, and it stores an account's token only encrypted. Set one for the API and the worker, and restart both.";
    case "credential_unreadable":
      return `A stored credential of the ${platformName} app can no longer be read: it was stored under another \`ENCRYPTION_KEY\`. Store it again in Settings, under Credentials.`;
    case "authorization_refused":
      return `${platformName} refused to start the connection. This is most often a wrong API key or secret, or a callback URL the app does not list; Settings, under Credentials, says what the app needs.`;
    case "platform_unavailable":
      return `${platformName} could not be reached. Try again in a moment.`;
    default:
      return errorMessage(error);
  }
}

/** Why an account could not be disconnected. */
export function disconnectProblem(error: ApiError): string {
  if (error.code === "social_account_not_found") {
    return "This project has no account connected there any more. The list has been read again.";
  }
  return errorMessage(error);
}

/** Why the platform's answer could not be turned into a connection: a title and what to do. */
export type CallbackProblem = { title: string; message: string };

export function callbackProblem(error: ApiError): CallbackProblem {
  switch (error.code) {
    case "connection_not_found":
      return {
        title: "This connection is unknown or was already used",
        message: `The answer may have been opened twice, or the connection was finished or cancelled already. ${START_AGAIN}`,
      };
    case "connection_expired":
      return {
        title: "The connection took too long",
        message: `Too much time passed between starting the connection and agreeing to it. ${START_AGAIN}`,
      };
    case "connection_secret_unreadable":
      return {
        title: "The connection can no longer be finished",
        message: `The API's \`ENCRYPTION_KEY\` changed after the connection was started, so its secret cannot be read. ${START_AGAIN}`,
      };
    case "authorization_refused":
      return {
        title: "The platform refused the agreement",
        message: `It did not accept what it sent back as a grant. ${START_AGAIN}`,
      };
    case "platform_unavailable":
      return {
        title: "The platform could not be reached",
        message: `The connection was not finished. ${START_AGAIN}`,
      };
    case "platform_app_not_set":
    case "credential_unreadable":
      return {
        title: "The platform's app is missing a credential",
        message: `Set its credentials in Settings, under Credentials. ${START_AGAIN}`,
      };
    case "encryption_key_missing":
      return {
        title: "The account's token cannot be stored",
        message: "The API has no `ENCRYPTION_KEY`. Set one for the API and the worker, restart both, then connect again.",
      };
    default:
      return { title: "The connection was not finished", message: errorMessage(error) };
  }
}

/** The callback page was opened without a whole answer from the platform. */
export const INCOMPLETE_CALLBACK: CallbackProblem = {
  title: "This address holds no answer from the platform",
  message: `The platform sends the browser here after a person agrees or cancels; opened any other way, there is nothing to finish. ${START_AGAIN}`,
};

/** The callback page was opened for a platform the dashboard does not know. */
export const UNKNOWN_PLATFORM: CallbackProblem = {
  title: "Not a platform the dashboard knows",
  message: "The address names no platform an account can be connected on.",
};
