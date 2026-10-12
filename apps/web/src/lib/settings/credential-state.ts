/**
 * How one credential is shown: the word for where its value comes from, and what
 * the row offers. Display only; whether a value may be stored is the API's decision,
 * and the row says in advance what the API would answer.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { CredentialSource, CredentialStatus } from "@/lib/api/types";

type SourceDisplay = { label: string; tone: Tone };

const SOURCE_DISPLAY: Record<CredentialSource, SourceDisplay> = {
  not_set: { label: "Not set", tone: "neutral" },
  stored: { label: "Set here", tone: "success" },
  environment: { label: "Set by the environment", tone: "success" },
};

export function credentialSourceDisplay(source: CredentialSource): SourceDisplay {
  return SOURCE_DISPLAY[source];
}

export type CredentialOffer = {
  /** The field for a value: for a first one, for one that replaces the stored one, or none. */
  field: "save" | "replace" | null;
  /** A stored value exists, in use or not, and can be removed. */
  removable: boolean;
  /** A value is stored but the environment's is the one in use. */
  storedIsIgnored: boolean;
};

/** The API refuses to store a value for a credential the environment sets, so none is asked for. */
export function credentialOffer(credential: Pick<CredentialStatus, "source" | "stored">): CredentialOffer {
  const hasStored = credential.stored !== null;
  if (credential.source === "environment") {
    return { field: null, removable: hasStored, storedIsIgnored: hasStored };
  }
  return { field: hasStored ? "replace" : "save", removable: hasStored, storedIsIgnored: false };
}
