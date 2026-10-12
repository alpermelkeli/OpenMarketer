"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useRemoveCredential, useSetCredential } from "@/lib/api/credentials";
import { errorMessage } from "@/lib/api/error-message";
import { OneTimeValue } from "@/lib/api/one-time-value";
import type { CredentialStatus } from "@/lib/api/types";
import { formatDateTime, formatTimeAgo } from "@/lib/format";
import { credentialLabel } from "@/lib/settings/credential-labels";
import { setCredentialProblems } from "@/lib/settings/credential-problems";
import { credentialOffer, credentialSourceDisplay } from "@/lib/settings/credential-state";
import { useNow } from "@/lib/use-now";

import { RemoveCredentialDialog } from "./remove-credential-dialog";

type CredentialRowProps = {
  credential: CredentialStatus;
  /** Whether the API can store a value at all: it cannot without its encryption key. */
  canStore: boolean;
};

/**
 * One credential: what it is for, where its value comes from, and the field that
 * stores a new one. The value typed is held here only until it has been sent; the
 * API never returns one, so a stored value is described and never shown.
 */
export function CredentialRow({ credential, canStore }: CredentialRowProps) {
  const id = useId();
  // Only a stored value has a time to count from.
  const now = useNow(credential.stored !== null);
  const setCredential = useSetCredential(credential.name);
  const removeCredential = useRemoveCredential(credential.name);
  const [value, setValue] = useState("");
  const [removeOpen, setRemoveOpen] = useState(false);

  const label = credentialLabel(credential);
  const source = credentialSourceDisplay(credential.source);
  const offer = credentialOffer(credential);
  const problems = setCredentialProblems(setCredential.error);
  const replacing = offer.field === "replace";
  const pending = setCredential.isPending;

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (value.trim() === "" || !canStore || pending) return;
    setCredential.mutate(new OneTimeValue(value), { onSuccess: () => setValue("") });
  }

  function openRemoval() {
    removeCredential.reset();
    setRemoveOpen(true);
  }

  const removeButton = offer.removable && (
    <Button type="button" variant="outline" onClick={openRemoval} disabled={pending}>
      Remove
      <span className="sr-only"> the stored {label.label}</span>
    </Button>
  );

  return (
    <li className="grid gap-4 py-6 [&_code]:font-mono [&_code]:text-xs">
      <div className="grid gap-1.5">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <h3 className="font-heading text-lg break-words">{label.label}</h3>
          <StatusPill tone={source.tone}>{source.label}</StatusPill>
        </div>
        <p className="max-w-prose text-sm text-pretty text-muted-foreground">
          <InlineCodeText text={label.purpose} />
        </p>
      </div>

      {/* Polite, so a screen reader hears the state change after a save or a removal. */}
      <div aria-live="polite" className="grid max-w-prose gap-1 text-sm text-pretty">
        {credential.source === "not_set" && (
          <p>
            No value is stored, and <code>{credential.environment_variable}</code> is not set in the API&rsquo;s
            environment.
          </p>
        )}
        {credential.source === "stored" && credential.stored !== null && (
          <p>
            Stored here <StoredTime setAt={credential.stored.set_at} now={now} />. The value itself cannot be shown.
          </p>
        )}
        {credential.source === "environment" && (
          <p>
            The value comes from <code>{credential.environment_variable}</code> in the API&rsquo;s environment. To
            store one here instead, unset the variable there and for the worker, and restart both.
          </p>
        )}
        {offer.storedIsIgnored && credential.stored !== null && (
          <p className="text-muted-foreground">
            A value was also stored here <StoredTime setAt={credential.stored.set_at} now={now} />. It is kept but
            ignored for as long as the variable is set.
          </p>
        )}
      </div>

      {offer.field === null && removeButton && <div>{removeButton}</div>}

      {offer.field !== null && (
        <form onSubmit={save} className="grid max-w-prose gap-2" autoComplete="off" noValidate>
          <Label htmlFor={`${id}-value`}>
            {replacing ? "New value" : "Value"}
            <span className="sr-only"> of the {label.label}</span>
          </Label>
          <div className="flex flex-wrap gap-2">
            {/* Nameless and marked for password managers to leave alone: it is not a login. */}
            <Input
              id={`${id}-value`}
              type="password"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              data-1p-ignore
              data-lpignore="true"
              data-bwignore
              data-form-type="other"
              disabled={!canStore || pending}
              className="min-w-48 flex-1 font-mono"
              aria-invalid={problems.value.length > 0}
              aria-describedby={`${id}-hint ${id}-errors`}
            />
            <Button type="submit" disabled={!canStore || pending || value.trim() === ""}>
              {pending ? "Saving…" : replacing ? "Replace" : "Save"}
            </Button>
            {removeButton}
          </div>
          <p id={`${id}-hint`} className="text-xs text-muted-foreground">
            {!canStore
              ? "Nothing can be stored until the API has an encryption key; see above."
              : replacing
                ? "Replacing overwrites the stored value. The old one cannot be brought back."
                : "Saving sends the value to the API once, which stores it encrypted. It is not kept in this browser."}
          </p>
          <FieldErrors id={`${id}-errors`} messages={problems.value} />
        </form>
      )}

      {problems.row.length > 0 && (
        <Notice tone="danger" title="The value was not stored" role="alert" className="max-w-prose">
          {problems.row.map((problem) => (
            <p key={problem}>
              <InlineCodeText text={problem} />
            </p>
          ))}
        </Notice>
      )}

      <RemoveCredentialDialog
        open={removeOpen}
        onOpenChange={setRemoveOpen}
        label={label.label}
        withoutIt={label.withoutIt}
        overriddenBy={offer.storedIsIgnored ? credential.environment_variable : null}
        pending={removeCredential.isPending}
        problem={removeCredential.error === null ? null : errorMessage(removeCredential.error)}
        onRemove={() => removeCredential.mutate(undefined, { onSuccess: () => setRemoveOpen(false) })}
      />
    </li>
  );
}

type StoredTimeProps = { setAt: string; now: number };

/** "5 minutes ago", with the exact time for whoever asks for it. */
function StoredTime({ setAt, now }: StoredTimeProps) {
  return (
    <time dateTime={setAt} title={formatDateTime(setAt)}>
      {formatTimeAgo(setAt, now)}
    </time>
  );
}
