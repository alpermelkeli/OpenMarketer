import type { ReactNode } from "react";

import { PageHeader } from "@/components/common/page-header";

import { SettingsMenu } from "./settings-menu";

type SettingsFrameProps = { children: ReactNode };

/** What every settings section shares: the heading, and the menu of sections beside (or above) the one open. */
export function SettingsFrame({ children }: SettingsFrameProps) {
  return (
    <div className="grid gap-8">
      <PageHeader
        title="Settings"
        description="How this installation is set up. What is set here holds for every project."
      />
      <div className="flex flex-col gap-8 border-t pt-8 lg:flex-row lg:gap-10">
        <SettingsMenu />
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
