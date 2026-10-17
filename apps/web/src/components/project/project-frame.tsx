"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { Notice } from "@/components/common/notice";
import { PageHeader } from "@/components/common/page-header";
import { RequestProblem } from "@/components/common/request-problem";
import { useShellProject } from "@/lib/api/projects";
import { isProjectId } from "@/lib/projects/project-id";
import { PROJECT_PAGES, projectPageOfPath } from "@/lib/projects/project-navigation";

type ProjectFrameProps = { projectId: string; children: ReactNode };

const backToProjects = (
  <Link href="/" className="rounded-sm hover:text-foreground">
    Projects
  </Link>
);

/**
 * What every screen of one project shares: the page's title, or why the project cannot be shown. The
 * sidebar shows which project this is and carries its menu; the chat has a heading of its own.
 */
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
  // Read for its errors only, under the sidebar's key: no extra request, and no screen's cache filled early.
  const project = useShellProject(projectId);
  const page = projectPageOfPath(pathname);

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

  if (page === "chat" || page === null) return children;

  return (
    <div className="grid gap-8">
      <PageHeader title={PROJECT_PAGES.find((entry) => entry.page === page)?.label} />
      {children}
    </div>
  );
}
