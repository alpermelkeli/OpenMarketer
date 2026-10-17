"use client";

import { SettingsIcon } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type NavLinkProps = {
  href: string;
  current: boolean;
  children: ReactNode;
  className?: string;
};

/** One entry of the sidebar: an icon and a word, marked when it is the page that is open. */
export function NavLink({ href, current, children, className }: NavLinkProps) {
  return (
    <Link
      href={href}
      aria-current={current ? "page" : undefined}
      className={cn(
        "flex h-8 max-w-56 shrink-0 items-center gap-2.5 rounded-md px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground lg:max-w-none [&_svg]:size-4 [&_svg]:shrink-0",
        current && "bg-accent text-foreground",
        className,
      )}
    >
      {children}
    </Link>
  );
}

/** The workspace's settings, at the foot of the sidebar wherever it is. */
export function SettingsLink({ current }: { current: boolean }) {
  return (
    <NavLink href="/settings" current={current}>
      <SettingsIcon aria-hidden="true" />
      Settings
    </NavLink>
  );
}
