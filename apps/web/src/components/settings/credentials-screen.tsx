"use client";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentials } from "@/lib/api/credentials";

import { CredentialRow } from "./credential-row";

/** The credentials section: where each of the installation's credentials comes from, and the way to set one. */
export function CredentialsScreen() {
  const credentials = useCredentials();
  const list = credentials.data;

  return (
    <section aria-labelledby="credentials" className="grid gap-6">
      <div>
        <h2 id="credentials" className="font-heading text-title">
          Credentials
        </h2>
        <p className="mt-2 max-w-prose text-pretty text-muted-foreground">
          The keys and tokens this installation works with. A value saved here is stored encrypted and is never shown
          again, on this screen or anywhere else. A value set in the environment takes precedence over a stored one.
        </p>
      </div>

      {credentials.error !== null && (
        <RequestProblem
          title="The credentials could not be read"
          error={credentials.error}
          onRetry={() => void credentials.refetch()}
        />
      )}

      {list === undefined && credentials.error === null && (
        <div className="grid gap-3" aria-busy="true" aria-label="Loading credentials">
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
        </div>
      )}

      {list?.can_store === false && (
        <Notice tone="warning" title="A value cannot be stored yet">
          The API has no <code>ENCRYPTION_KEY</code>, and it stores a credential only encrypted. Generate a key with{" "}
          <code className="whitespace-nowrap">openssl rand -base64 32</code>, set it as <code>ENCRYPTION_KEY</code> for the API and for the worker,
          and restart both. Credentials set in the environment work without it.
        </Notice>
      )}

      {list?.credentials.length === 0 && (
        <p className="max-w-prose text-sm text-pretty text-muted-foreground">The API lists no credentials.</p>
      )}

      {list !== undefined && list.credentials.length > 0 && (
        <ul className="divide-y border-y">
          {list.credentials.map((credential) => (
            <CredentialRow key={credential.name} credential={credential} canStore={list.can_store} />
          ))}
        </ul>
      )}
    </section>
  );
}
