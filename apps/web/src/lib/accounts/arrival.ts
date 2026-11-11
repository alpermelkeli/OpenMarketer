/**
 * How the callback page hands its outcome to the accounts screen: one parameter in the
 * accounts screen's address (`?connected=x` or `?declined=x`), which the screen reads once
 * and removes. Nothing about the connection is in it but its outcome and the platform's slug,
 * and nothing is kept in the browser.
 */

import type { ConnectionOutcome, SocialPlatform } from "@/lib/api/types";
import { projectPageHref } from "@/lib/projects/project-navigation";

import { isSocialPlatform } from "./platforms";

export type Arrival = { outcome: ConnectionOutcome; platform: SocialPlatform };

const OUTCOMES: readonly ConnectionOutcome[] = ["connected", "declined"];

/** The project's accounts screen, saying how the connection just ended. */
export function arrivalHref(projectId: string, arrival: Arrival): string {
  return `${projectPageHref(projectId, "accounts")}?${arrival.outcome}=${encodeURIComponent(arrival.platform)}`;
}

/** The outcome an address of the accounts screen carries, or null for none or one the dashboard did not write. */
export function arrivalOf(query: URLSearchParams): Arrival | null {
  const found: Arrival[] = [];
  for (const outcome of OUTCOMES) {
    for (const platform of query.getAll(outcome)) {
      if (isSocialPlatform(platform)) found.push({ outcome, platform });
    }
  }
  return found.length === 1 ? found[0] : null;
}
