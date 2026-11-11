"use client";

import Link from "next/link";
import { useId } from "react";

import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { Button } from "@/components/ui/button";
import { blockerExplanation, type AccountRow as Row } from "@/lib/accounts/account-rows";
import { accountProfileHref } from "@/lib/accounts/platforms";
import type { ConnectedAccount, SocialPlatform } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

import { AccountAvatar } from "./account-avatar";
import { PlatformMark } from "./platform-mark";

type AccountRowProps = {
  row: Row;
  /** A connection of this platform is being started, or the browser is on its way to the platform. */
  connecting: boolean;
  /** Another request of the screen is under way, so this row's buttons wait. */
  busy: boolean;
  /** Why the last connection could not be started, in words for the person. */
  problem: string | null;
  onConnect: () => void;
  onDisconnect: () => void;
};

/**
 * One platform: the project's account on it, or the way to connect one, and why that way is
 * closed when it is. The account's names come from the platform and are shown as text.
 */
export function AccountRow({ row, connecting, busy, problem, onConnect, onDisconnect }: AccountRowProps) {
  const id = useId();
  const { platform, account, blockers } = row;
  const name = platform.name;
  const blocked = blockers.length > 0;

  return (
    <li className="grid gap-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h3 className="flex items-center gap-2 font-heading text-lg break-words">
          <PlatformMark platform={platform.platform} />
          {name}
        </h3>
        {account === null ? (
          <StatusPill tone="neutral">Not connected</StatusPill>
        ) : (
          <StatusPill tone="success">Connected</StatusPill>
        )}
      </div>

      {/* Polite, so a screen reader hears the account appear or go. */}
      <div aria-live="polite" className="grid max-w-prose gap-3 text-sm text-pretty">
        {account === null ? (
          <p>No {name} account is connected to this project.</p>
        ) : (
          <ConnectedAccountSummary platform={platform.platform} platformName={name} account={account} />
        )}
      </div>

      {blocked && (
        <div id={`${id}-blocked`} className="grid max-w-prose gap-1 text-sm text-pretty text-muted-foreground">
          {blockers.map((blocker) => (
            <p key={blocker.kind} className="[&_code]:font-mono [&_code]:text-xs [&_code]:text-foreground">
              <InlineCodeText text={blockerExplanation(blocker, name)} />
            </p>
          ))}
          <p>
            <Link
              href="/settings/credentials"
              className="rounded-sm text-brand underline-offset-4 hover:underline"
            >
              Open Settings, Credentials
            </Link>
          </p>
        </div>
      )}

      <div className="grid max-w-prose gap-2">
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant={account === null ? "default" : "outline"}
            onClick={onConnect}
            disabled={blocked || busy || connecting}
            aria-describedby={blocked ? `${id}-blocked ${id}-hint` : `${id}-hint`}
          >
            {connecting ? `Opening ${name}…` : account === null ? `Connect ${name}` : "Connect another account"}
          </Button>
          {account !== null && (
            <Button type="button" variant="outline" onClick={onDisconnect} disabled={busy || connecting}>
              Disconnect
              <span className="sr-only"> @{account.username}</span>
            </Button>
          )}
        </div>
        <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
          {account === null
            ? `Connecting opens ${name}'s own page, where you sign in and agree; ${name} then sends you back here. A project has one ${name} account: connecting another later replaces it.`
            : `Connecting another account replaces @${account.username}: whichever account is signed in on ${name}'s page becomes this project's.`}
        </p>
      </div>

      {problem !== null && (
        <Notice tone="danger" title={`${name} could not be connected`} role="alert" className="max-w-prose">
          <p>
            <InlineCodeText text={problem} />
          </p>
        </Notice>
      )}
    </li>
  );
}

type ConnectedAccountSummaryProps = { platform: SocialPlatform; platformName: string; account: ConnectedAccount };

/** Who the connected account is, as the platform described it, and since when it is the project's. */
function ConnectedAccountSummary({ platform, platformName, account }: ConnectedAccountSummaryProps) {
  const profile = accountProfileHref(platform, account.username);
  return (
    <>
      <div className="flex min-w-0 items-center gap-3">
        <AccountAvatar account={account} />
        <div className="grid min-w-0">
          <span className="truncate">{account.display_name}</span>
          {profile === null ? (
            <span className="truncate font-mono text-xs text-muted-foreground">@{account.username}</span>
          ) : (
            <a
              href={profile}
              target="_blank"
              rel="noopener noreferrer"
              className="w-fit max-w-full truncate rounded-sm font-mono text-xs text-brand underline-offset-4 hover:underline"
            >
              @{account.username}
              <span className="sr-only"> on {platformName} (opens in a new tab)</span>
            </a>
          )}
        </div>
      </div>
      <p className="text-muted-foreground">
        Connected on{" "}
        <time dateTime={account.connected_at}>{formatDateTime(account.connected_at)}</time>
        . Its access token is stored encrypted and is never shown.
      </p>
    </>
  );
}
