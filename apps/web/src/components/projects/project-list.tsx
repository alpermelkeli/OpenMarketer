import { ArrowUpRightIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import type { Project } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { httpsUrlOrNull } from "@/lib/profile/evidence-link";

type ProjectListProps = {
  projects: readonly Project[];
  /** Projects opened by ID: only the ID is known. */
  openedProjectIds: readonly string[];
  onForget: (projectId: string) => void;
};

/** The projects this browser remembers, one hairline row each. */
export function ProjectList({ projects, openedProjectIds, onForget }: ProjectListProps) {
  return (
    <ul className="divide-y border-t">
      {projects.map((project) => (
        <li key={project.id} className="flex flex-wrap items-center gap-x-6 gap-y-2 py-4">
          <div className="min-w-0 flex-1 basis-64">
            <Link href={`/projects/${project.id}`} className="block w-fit max-w-full truncate rounded-sm font-heading text-lg hover:underline">
              {project.name}
            </Link>
            <RepositoryLine repositoryUrl={project.repository_url} />
          </div>
          <p className="text-xs text-muted-foreground">Created {formatDateTime(project.created_at)}</p>
          <RowActions projectId={project.id} label={project.name} onForget={onForget} />
        </li>
      ))}
      {openedProjectIds.map((id) => (
        <li key={id} className="flex flex-wrap items-center gap-x-6 gap-y-2 py-4">
          <div className="min-w-0 flex-1 basis-64">
            <Link href={`/projects/${id}`} className="rounded-sm font-mono text-sm hover:underline">
              {id}
            </Link>
            <p className="mt-1 text-xs text-muted-foreground">
              Opened by ID. The API cannot tell the dashboard its name or repository yet.
            </p>
          </div>
          <RowActions projectId={id} label={`project ${shortId(id)}`} onForget={onForget} />
        </li>
      ))}
    </ul>
  );
}

function RepositoryLine({ repositoryUrl }: { repositoryUrl: string }) {
  const href = httpsUrlOrNull(repositoryUrl);
  if (href === null) {
    return <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{repositoryUrl}</p>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="mt-1 inline-flex max-w-full items-center gap-1 rounded-sm font-mono text-xs text-muted-foreground hover:text-foreground"
    >
      <span className="truncate">{repositoryUrl}</span>
      <ArrowUpRightIcon aria-hidden="true" className="size-3 shrink-0" />
      <span className="sr-only">(opens the repository in a new tab)</span>
    </a>
  );
}

type RowActionsProps = { projectId: string; label: string; onForget: (projectId: string) => void };

function RowActions({ projectId, label, onForget }: RowActionsProps) {
  return (
    <div className="flex items-center gap-1">
      <Button variant="ghost" size="sm" onClick={() => onForget(projectId)} aria-label={`Forget ${label} in this browser`}>
        Forget
      </Button>
      <Button variant="outline" size="sm" nativeButton={false} render={<Link href={`/projects/${projectId}`} />}>
        Open
      </Button>
    </div>
  );
}
