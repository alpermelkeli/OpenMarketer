"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { Notice } from "@/components/common/notice";
import { PageHeader } from "@/components/common/page-header";
import { RepositoryLink } from "@/components/common/repository-link";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { useProject } from "@/lib/api/projects";
import { useProposals } from "@/lib/api/proposals";
import { isProjectId } from "@/lib/projects/project-id";
import { cn } from "@/lib/utils";

type ProjectFrameProps = { projectId: string; children: ReactNode };

const backToProjects = (
  <Link href="/" className="rounded-sm hover:text-foreground">
    Projects
  </Link>
);

/** What every screen of one project shares: its name, its repository and the tabs. */
export function ProjectFrame({ projectId, children }: ProjectFrameProps) {
  if (!isProjectId(projectId)) {
    return (
      <div className="grid gap-8">
        <PageHeader title="Not a project" eyebrow={backToProjects} />
        <Notice tone="warning">The address does not contain a project ID.</Notice>
      </div>
    );
  }
  return <KnownProjectFrame projectId={projectId}>{children}</KnownProjectFrame>;
}

function KnownProjectFrame({ projectId, children }: ProjectFrameProps) {
  const pathname = usePathname();
  const project = useProject(projectId);
  const proposals = useProposals(projectId);
  // Of the proposals loaded so far: enough to say that something waits, which is what the tab is for.
  const waiting = proposals.data?.filter((proposal) => proposal.status === "pending").length ?? 0;

  if (project.error !== null) {
    const missing = project.error.code === "project_not_found";
    return (
      <div className="grid gap-8">
        <PageHeader title={missing ? "No such project" : "Project"} eyebrow={backToProjects} />
        {missing ? (
          <Notice tone="warning">
            The API has no project with this ID. It may belong to another database, or the address may be mistyped.
          </Notice>
        ) : (
          <RequestProblem
            title="The project could not be read"
            error={project.error}
            onRetry={() => void project.refetch()}
          />
        )}
      </div>
    );
  }

  const tabs = [
    { href: `/projects/${projectId}/profile`, label: "Profile", waiting: 0 },
    { href: `/projects/${projectId}/analyses`, label: "Analyses", waiting: 0 },
    { href: `/projects/${projectId}/approvals`, label: "Approvals", waiting },
  ];

  return (
    <div className="grid gap-8">
      <PageHeader
        eyebrow={backToProjects}
        title={project.data?.name ?? <Skeleton className="h-9 w-56" />}
        description={project.data && <RepositoryLink repositoryUrl={project.data.repository_url} className="text-sm" />}
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
              {tab.waiting > 0 && (
                <span className="ml-1.5 rounded-full border border-warning/50 px-1.5 font-mono text-2xs text-warning tabular-nums">
                  {tab.waiting}
                  <span className="sr-only"> waiting for your decision</span>
                </span>
              )}
            </Link>
          );
        })}
      </nav>
      {children}
    </div>
  );
}
