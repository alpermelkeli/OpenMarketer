/**
 * The accounts screen's rows: one per platform the API lists, with the project's account on it
 * if there is one, and what stops a connection from starting. Display only: the API decides
 * whether a platform is ready and refuses a connection that cannot be made.
 */

import type { ConnectablePlatform, ConnectedAccount, SocialAccounts } from "@/lib/api/types";
import { credentialLabel } from "@/lib/settings/credential-labels";

/** Why a connection cannot be started now. */
export type ConnectBlocker =
  /** The API has no `ENCRYPTION_KEY`, so it cannot store the account's token. */
  | { kind: "encryption_key_missing" }
  /** Credentials of the installation's app on the platform are not set; their names in words. */
  | { kind: "app_not_set"; missing: readonly string[] };

export type AccountRow = {
  platform: ConnectablePlatform;
  account: ConnectedAccount | null;
  /** Empty when a connection can be started. */
  blockers: readonly ConnectBlocker[];
};

export function accountRows(list: SocialAccounts): AccountRow[] {
  return list.platforms.map((platform) => ({
    platform,
    account: list.accounts.find((account) => account.platform === platform.platform) ?? null,
    blockers: connectBlockers(platform, list.can_connect),
  }));
}

function connectBlockers(platform: ConnectablePlatform, canConnect: boolean): ConnectBlocker[] {
  const blockers: ConnectBlocker[] = [];
  if (!canConnect) blockers.push({ kind: "encryption_key_missing" });
  if (!platform.ready) {
    blockers.push({
      kind: "app_not_set",
      missing: platform.missing_credentials.map((credential) => credentialLabel(credential).label),
    });
  }
  return blockers;
}

/** Why the connect button of a platform is disabled, in a sentence of the dashboard's own, names between backticks. */
export function blockerExplanation(blocker: ConnectBlocker, platformName: string): string {
  if (blocker.kind === "encryption_key_missing") {
    return "The API has no `ENCRYPTION_KEY`, and it stores an account's token only encrypted. Set one for the API and the worker and restart both; Settings, under Credentials, says how.";
  }
  const missing = missingCredentialsInWords(blocker.missing);
  const verb = blocker.missing.length === 1 ? "is" : "are";
  return `${capitalized(missing)} of the installation's ${platformName} app ${verb} not set. Set ${blocker.missing.length === 1 ? "it" : "them"} in Settings, under Credentials, where what the app needs is described too.`;
}

function capitalized(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** "the X API key and the X API secret", or "some credentials" when the API named none. */
export function missingCredentialsInWords(missing: readonly string[]): string {
  if (missing.length === 0) return "some credentials";
  const named = missing.map((label) => `the ${label}`);
  return named.length === 1 ? named[0] : `${named.slice(0, -1).join(", ")} and ${named[named.length - 1]}`;
}

/** Up to two letters for an account without a picture: from its display name, else its username. */
export function accountInitials(account: Pick<ConnectedAccount, "display_name" | "username">): string {
  // Words with a letter or a digit in them: an emoji or a dash alone gives no initial.
  const letters = account.display_name
    .split(/\s+/)
    .map(firstLetter)
    .filter((letter) => letter !== "");
  const initials =
    letters.length > 1 ? [letters[0], letters[letters.length - 1]] : [letters[0] ?? firstLetter(account.username)];
  return initials.join("").toUpperCase() || "?";
}

function firstLetter(word: string): string {
  return /[\p{L}\p{N}]/u.exec(word)?.[0] ?? "";
}

/** The account's picture as the platform gave it, when it is an https address; anything else is not shown. */
export function accountPicture(account: Pick<ConnectedAccount, "picture_url">): string | null {
  return isHttps(account.picture_url) ? account.picture_url : null;
}

/** Where to send the browser to agree to a connection: the API's answer, when it is an https address. */
export function authorizeDestination(authorizeUrl: string): string | null {
  return isHttps(authorizeUrl) ? authorizeUrl : null;
}

function isHttps(value: string | null): value is string {
  if (value === null) return false;
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}
