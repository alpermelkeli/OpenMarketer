"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { SETTINGS_SECTIONS, isSectionOpen } from "@/lib/settings/sections";
import { cn } from "@/lib/utils";

/** The sections of the settings: a column beside the open section, a row above it on narrow screens. */
export function SettingsMenu() {
  const pathname = usePathname();

  return (
    // The padding and the negative margin leave room for a link's focus ring inside the scrolling row.
    <nav
      aria-label="Settings"
      className="-m-1 flex shrink-0 gap-1 overflow-x-auto p-1 lg:w-settings-menu lg:flex-col lg:self-start lg:overflow-visible"
    >
      {SETTINGS_SECTIONS.map((section) => {
        const current = isSectionOpen(section, pathname);
        return (
          <Link
            key={section.href}
            href={section.href}
            aria-current={current ? "page" : undefined}
            className={cn(
              "flex h-8 shrink-0 items-center rounded-md px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
              current && "bg-accent text-foreground",
            )}
          >
            {section.label}
          </Link>
        );
      })}
    </nav>
  );
}
