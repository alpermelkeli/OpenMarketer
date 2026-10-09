import { Suspense, type ReactNode } from "react";

import { SidebarNav } from "./sidebar-nav";
import { Wordmark } from "./wordmark";

type AppShellProps = { children: ReactNode };

/** The frame around every screen: the sidebar (a top bar on narrow screens) and the main column. */
export function AppShell({ children }: AppShellProps) {
  return (
    <div className="flex min-h-dvh flex-col lg:flex-row">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:text-primary-foreground"
      >
        Skip to content
      </a>
      <aside className="flex shrink-0 flex-col border-b bg-surface lg:sticky lg:top-0 lg:h-dvh lg:w-sidebar lg:border-r lg:border-b-0">
        <div className="flex h-14 items-center px-gutter">
          <Wordmark />
        </div>
        <Suspense>
          <SidebarNav />
        </Suspense>
        <p className="mt-auto hidden border-t px-gutter py-4 text-xs text-muted-foreground lg:block">
          Local mode. There is no login: whoever is at this machine is the reviewer.
        </p>
      </aside>
      <main id="main" className="min-w-0 flex-1">
        <div className="mx-auto max-w-content px-gutter pt-10 pb-24 lg:px-gutter-wide lg:pt-14">{children}</div>
      </main>
    </div>
  );
}
