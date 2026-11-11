"use client";

import Image from "next/image";
import { useState } from "react";

import { accountInitials, accountPicture } from "@/lib/accounts/account-rows";
import type { ConnectedAccount } from "@/lib/api/types";

type AccountAvatarProps = { account: Pick<ConnectedAccount, "display_name" | "username" | "picture_url"> };

/**
 * The account's picture as the platform serves it, or its initials when it has none, the address
 * is not https, or the picture does not load. Decorative: the account's name is written beside it.
 */
export function AccountAvatar({ account }: AccountAvatarProps) {
  const picture = accountPicture(account);
  const [failed, setFailed] = useState(false);

  if (picture === null || failed) {
    return (
      <span
        aria-hidden="true"
        className="grid size-10 shrink-0 place-items-center rounded-full border bg-surface font-mono text-xs text-muted-foreground"
      >
        {accountInitials(account)}
      </span>
    );
  }
  // Unoptimized: the dashboard's server does not fetch the platform's images, the browser does.
  return (
    <Image
      src={picture}
      alt=""
      width={40}
      height={40}
      unoptimized
      onError={() => setFailed(true)}
      className="size-10 shrink-0 rounded-full border bg-surface object-cover"
    />
  );
}
