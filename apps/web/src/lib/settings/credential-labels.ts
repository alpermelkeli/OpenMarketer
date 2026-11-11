/**
 * What each credential is called, what it is for, and what stops working without it.
 * Display only: which credentials exist, and their names, are the API's to say.
 *
 * Sentences are the dashboard's own wording, with commands and names between backticks.
 */

import type { CredentialStatus } from "@/lib/api/types";

export type CredentialLabel = {
  label: string;
  /** One sentence or two: what uses it, what access it needs and where it is sent. */
  purpose: string;
  /** What no longer works once its value is removed. */
  withoutIt: string;
  /** What has to be set up outside the dashboard for it to work, when there is something. */
  setup?: string;
};

// A map, not an object: a name such as "constructor" must not find something inherited.
const KNOWN = new Map<string, CredentialLabel>([
  [
    "openrouter_api_key",
    {
      label: "OpenRouter API key",
      purpose:
        "Used for every model call the analyzer makes, through OpenRouter. Without one an analysis fails, and with one it spends that account's credit.",
      withoutIt: "Analyses fail until a key is set again.",
    },
  ],
  [
    "github_token",
    {
      label: "GitHub token",
      purpose:
        "Lets an analysis clone private repositories on GitHub. Read-only access to repository contents is enough, and it is sent only to github.com.",
      withoutIt:
        "Private repositories on github.com can no longer be cloned, so their analyses fail. Public repositories are not affected.",
    },
  ],
  [
    "gitlab_token",
    {
      label: "GitLab token",
      purpose:
        "Lets an analysis clone private repositories on GitLab. Read-only repository access (`read_repository`) is enough, and it is sent only to gitlab.com, or to the host the installation's `GITLAB_HOST` names.",
      withoutIt:
        "Private repositories on that GitLab host can no longer be cloned, so their analyses fail. Public repositories are not affected.",
    },
  ],
  [
    "x_api_key",
    {
      label: "X API key",
      purpose:
        "The Consumer Key of your X app, listed under OAuth 1.0 Keys in the X developer portal. With the X API secret it lets a project connect its X account. It is sent only to X.",
      withoutIt: "No project can connect an X account until a key is set again.",
      setup:
        "In the X developer portal, give the app Read and write permission and register `http://127.0.0.1:3000/connections/x/callback` as its callback URL (if the API's `DASHBOARD_URL` is set, that address followed by `/connections/x/callback`). X sends the browser back to that address, so open the dashboard at http://127.0.0.1:3000 rather than at localhost.",
    },
  ],
  [
    "x_api_secret",
    {
      label: "X API secret",
      purpose:
        "The Consumer Secret of your X app, listed with its Consumer Key under OAuth 1.0 Keys. It signs the requests that connect a project's X account and is never sent itself.",
      withoutIt: "No project can connect an X account until a secret is set again.",
    },
  ],
]);

const ENVIRONMENT_VARIABLE = /^[A-Z][A-Z0-9_]*$/;

/**
 * The words for one credential. A credential the API added after this was written is
 * called by its name and described by its environment variable.
 */
export function credentialLabel(credential: Pick<CredentialStatus, "name" | "environment_variable">): CredentialLabel {
  const known = KNOWN.get(credential.name);
  if (known !== undefined) return known;

  // The variable's name goes between backticks, so only one that has the form of a name does.
  const variable = ENVIRONMENT_VARIABLE.test(credential.environment_variable)
    ? ` The environment variable \`${credential.environment_variable}\` sets it.`
    : "";
  return {
    label: credential.name,
    purpose: `This dashboard has no description of this credential yet.${variable}`,
    withoutIt: "Whatever uses this credential stops working until a value is set again.",
  };
}
