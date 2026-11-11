/**
 * What the dashboard knows of each social platform beyond what the API says: how its callback
 * names the connection, and where a public profile is. Display only: which platforms exist,
 * their names and whether one is ready are the API's to say.
 *
 * Written as records over the generated `SocialPlatform`, so a platform the API adds fails
 * the type check here until the dashboard knows how to read its callback.
 */

import type { SocialPlatform } from "@/lib/api/types";

/** What a platform's callback brought, before it is sent on: the connection it names, and the person's grant. */
export type CallbackRequest = { state: string; grant: string | null };

type CallbackReader = (query: URLSearchParams) => CallbackRequest | null;

/**
 * X (OAuth 1.0a): `oauth_token` with `oauth_verifier` when the person agreed, `denied` with
 * the same token when they cancelled. A cancellation wins over anything else in the address.
 */
function readXCallback(query: URLSearchParams): CallbackRequest | null {
  const denied = single(query, "denied");
  if (denied !== null) return { state: denied, grant: null };
  const token = single(query, "oauth_token");
  const verifier = single(query, "oauth_verifier");
  return token !== null && verifier !== null ? { state: token, grant: verifier } : null;
}

const CALLBACK_READERS: Record<SocialPlatform, CallbackReader> = { x: readXCallback };

const PROFILE_ORIGINS: Record<SocialPlatform, string> = { x: "https://x.com" };

/** Whether a piece of an address names a platform the dashboard knows. */
export function isSocialPlatform(value: string): value is SocialPlatform {
  return Object.hasOwn(CALLBACK_READERS, value);
}

/** What to send the API for a platform's callback, or null when the address does not hold a whole answer. */
export function callbackRequest(platform: SocialPlatform, query: URLSearchParams): CallbackRequest | null {
  return CALLBACK_READERS[platform](query);
}

/** The account's public profile: the platform's own origin and the username as one encoded segment. */
export function accountProfileHref(platform: SocialPlatform, username: string): string | null {
  if (username === "") return null;
  return `${PROFILE_ORIGINS[platform]}/${encodeURIComponent(username)}`;
}

// A parameter given once and not empty; anything else is not an answer the platform wrote.
function single(query: URLSearchParams, name: string): string | null {
  const values = query.getAll(name);
  return values.length === 1 && values[0] !== "" ? values[0] : null;
}
