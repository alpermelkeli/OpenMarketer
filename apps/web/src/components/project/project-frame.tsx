"use client";

import { ArrowUpRightIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { Notice } from "@/components/common/notice";
import { PageHeader } from "@/components/common/page-header";
import { shortId } from "@/lib/format";
import { httpsUrlOrNull } from "@/lib/profile/evidence-link";
import { findProject, isProjectId } from "@/lib/remembered/remembered";
import { useRemembered } from "@/lib/remembered/use-remembered";
import { cn } from "@/lib/utils";

type ProjectFrameProps = { projectId: string; children: ReactNode };

/** What every screen of one project shares: its name, its repository and the tabs. */
export function ProjectFrame({ projectId, children }: ProjectFrameProps) {
  const pathname = usePathname();
  const { remembered } = useRemembered();

  if (!isProjectId(projectId)) {
    return (
      <div className="grid gap-8">
        <PageHeader title="Not a project" eyebrow={<Link href="/">Projects</Link>} />
        <Notice tone="warning">The address does not contain a project ID.</Notice>
      </div>
    );
  }

  const project = remembered === null ? null : findProject(remembered, projectId);
  const repositoryHref = project === null ? null : httpsUrlOrNull(project.repository_url);
  const tabs = [
    { href: `/projects/${projectId}/profile`, label: "Profile" },
    { href: `/projects/${projectId}/analyses`, label: "Analyses" },
  ];

  return (
    <div className="grid gap-8">
      <PageHeader
        eyebrow={
          <Link href="/" className="rounded-sm hover:text-foreground">
            Projects
          </Link>
        }
        title={project?.name ?? <span className="font-mono text-title">{shortId(projectId)}</span>}
        description={
          project === null ? (
            remembered !== null && "Opened by ID. The API cannot tell the dashboard this project's name or repository yet."
          ) : repositoryHref === null ? (
            <span className="font-mono text-sm break-all">{project.repository_url}</span>
          ) : (
            <a
              href={repositoryHref}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex max-w-full items-center gap-1 rounded-sm font-mono text-sm hover:text-foreground"
            >
              <span className="truncate">{project.repository_url}</span>
              <ArrowUpRightIcon aria-hidden="true" className="size-3.5 shrink-0" />
              <span className="sr-only">(opens the repository in a new tab)</span>
            </a>
          )
        }
      />
      <nav aria-label="Project" className="-mb-px flex gap-6 border-b">
        {tabs.map((tab) => {
          const current = pathname.startsWith(tab.href);
          return (
            <Link
              key={tab.href}
              href={tab.href}
              aria-current={current ? "page" : undefined}
              className={cn(
                "-mb-px border-b py-2.5 text-sm transition-colors",
                current
                  ? "border-foreground text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
      {children}
    </div>
  );
}
