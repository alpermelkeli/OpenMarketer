"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { accountRows, authorizeDestination, type AccountRow as Row } from "@/lib/accounts/account-rows";
import { arrivalOf, type Arrival } from "@/lib/accounts/arrival";
import { connectProblem, disconnectProblem } from "@/lib/accounts/connection-problems";
import { useConnectSocialAccount, useDisconnectSocialAccount, useSocialAccounts } from "@/lib/api/social-accounts";
import type { SocialAccounts, SocialPlatform } from "@/lib/api/types";

import { AccountRow } from "./account-row";
import { DisconnectAccountDialog } from "./disconnect-account-dialog";

type AccountsScreenProps = { projectId: string };

/** A destination the API gave that the dashboard will not send the browser to. */
const UNSAFE_DESTINATION = "The API answered with a page that is not an https address, so the dashboard did not open it.";

/**
 * The project's accounts on social platforms: one row per platform the API lists, with the
 * account connected there or the way to connect one. Connecting sends the browser to the
 * platform, which sends it back to the callback page; that page brings it here with the outcome.
 */
export function AccountsScreen({ projectId }: AccountsScreenProps) {
  const accounts = useSocialAccounts(projectId);
  const connect = useConnectSocialAccount(projectId);
  const disconnect = useDisconnectSocialAccount(projectId);
  const searchParams = useSearchParams();
  // Read once: the parameter is taken out of the address below, and the notice stays until the person leaves.
  const [arrival] = useState(() => arrivalOf(searchParams));
  const [unsafeDestination, setUnsafeDestination] = useState<SocialPlatform | null>(null);
  const [disconnecting, setDisconnecting] = useState<Row | null>(null);

  useEffect(() => {
    // The native call keeps this screen mounted; a reload or a link then shows no stale notice.
    if (arrival !== null) window.history.replaceState(null, "", window.location.pathname);
  }, [arrival]);

  const list = accounts.data;
  const rows = list === undefined ? undefined : accountRows(list);
  // On its way to the platform: the browser keeps this page until the platform's has loaded.
  const leavingFor = connect.isPending || (connect.isSuccess && unsafeDestination === null) ? connect.variables : null;
  const busy = connect.isPending || disconnect.isPending;

  function startConnecting(platform: SocialPlatform) {
    setUnsafeDestination(null);
    connect.mutate(platform, {
      onSuccess: ({ authorize_url }) => {
        const destination = authorizeDestination(authorize_url);
        if (destination === null) setUnsafeDestination(platform);
        else window.location.assign(destination);
      },
    });
  }

  function problemOf(row: Row): string | null {
    if (unsafeDestination === row.platform.platform) return UNSAFE_DESTINATION;
    if (connect.error === null || connect.variables !== row.platform.platform) return null;
    return connectProblem(connect.error, row.platform.name);
  }

  function openDisconnect(row: Row) {
    disconnect.reset();
    setDisconnecting(row);
  }

  return (
    <div className="grid gap-8">
      <section aria-labelledby="about-accounts" className="max-w-prose">
        <h2 id="about-accounts" className="font-heading text-title">
          Where this project posts from
        </h2>
        <p className="mt-2 text-pretty text-muted-foreground">
          Connect the project&rsquo;s account on each platform. The platform asks you to sign in and agree on its own
          page; this installation then keeps the account&rsquo;s access token, encrypted, and never shows it. Nothing
          is posted yet: an approved post is still recorded as a dry run and sent nowhere.
        </p>
      </section>

      {arrival !== null && <ArrivalNotice arrival={arrival} list={list} />}

      {accounts.error !== null && (
        <RequestProblem
          title="The accounts could not be read"
          error={accounts.error}
          onRetry={() => void accounts.refetch()}
        />
      )}

      {rows === undefined && accounts.error === null && (
        <div aria-busy="true" aria-label="Loading accounts">
          <Skeleton className="h-36" />
        </div>
      )}

      {rows?.length === 0 && (
        <p className="max-w-prose text-sm text-pretty text-muted-foreground">
          The API lists no platform an account can be connected on.
        </p>
      )}

      {rows !== undefined && rows.length > 0 && (
        <ul className="divide-y border-y">
          {rows.map((row) => (
            <AccountRow
              key={row.platform.platform}
              row={row}
              connecting={leavingFor === row.platform.platform}
              busy={busy}
              problem={problemOf(row)}
              onConnect={() => startConnecting(row.platform.platform)}
              onDisconnect={() => openDisconnect(row)}
            />
          ))}
        </ul>
      )}

      {disconnecting !== null && disconnecting.account !== null && (
        <DisconnectAccountDialog
          open
          onOpenChange={(isOpen) => !isOpen && !disconnect.isPending && setDisconnecting(null)}
          platformName={disconnecting.platform.name}
          username={disconnecting.account.username}
          pending={disconnect.isPending}
          problem={disconnect.error === null ? null : disconnectProblem(disconnect.error)}
          onDisconnect={() =>
            disconnect.mutate(disconnecting.platform.platform, { onSuccess: () => setDisconnecting(null) })
          }
        />
      )}
    </div>
  );
}

type ArrivalNoticeProps = { arrival: Arrival; list: SocialAccounts | undefined };

/** How the connection the person just came back from ended, said once. */
function ArrivalNotice({ arrival, list }: ArrivalNoticeProps) {
  const name = list?.platforms.find((platform) => platform.platform === arrival.platform)?.name;
  if (arrival.outcome === "declined") {
    return (
      <Notice tone="neutral" title="The connection was cancelled" role="status" className="max-w-prose">
        You cancelled on {name === undefined ? "the platform" : name}&rsquo;s page, so nothing was stored and the
        project&rsquo;s accounts are as they were.
      </Notice>
    );
  }
  const account = list?.accounts.find((connected) => connected.platform === arrival.platform);
  return (
    <Notice tone="success" title="The account is connected" role="status" className="max-w-prose">
      {account === undefined || name === undefined
        ? "The account you agreed with is now this project's."
        : `@${account.username} is now this project's ${name} account.`}
    </Notice>
  );
}
