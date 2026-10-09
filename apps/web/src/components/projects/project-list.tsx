import Link from "next/link";

import { RepositoryLink } from "@/components/common/repository-link";
import { StatusPill } from "@/components/common/status-pill";
import type { Project } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { projectStates } from "@/lib/projects/project-summary";

type ProjectListProps = {
  projects: readonly Project[];
};

/** The workspace's projects, one hairline row each, with where each one stands. */
export function ProjectList({ projects }: ProjectListProps) {
  return (
    <ul className="divide-y border-t">
      {projects.map((project) => (
        <li key={project.id} className="flex flex-wrap items-center gap-x-6 gap-y-3 py-4">
          <div className="min-w-0 flex-1 basis-72">
            <Link
              href={`/projects/${project.id}`}
              className="block w-fit max-w-full truncate rounded-sm font-heading text-lg hover:underline"
            >
              {project.name}
            </Link>
            <RepositoryLink repositoryUrl={project.repository_url} className="mt-1 text-xs" />
            <p className="mt-1 text-xs text-muted-foreground">Created {formatDateTime(project.created_at)}</p>
          </div>
          <ul aria-label={`State of ${project.name}`} className="flex flex-wrap items-center gap-1.5">
            {projectStates(project).map((state) => (
              <li key={state.label}>
                <StatusPill tone={state.tone} live={state.tone === "progress"}>
                  {state.label}
                </StatusPill>
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}
