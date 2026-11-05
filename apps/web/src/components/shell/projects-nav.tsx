"use client";

import { ChevronRightIcon, LayoutGridIcon } from "lucide-react";
import { useState } from "react";

import { NavLink, SettingsLink } from "@/components/shell/nav-link";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Skeleton } from "@/components/ui/skeleton";
import { useShellProjects } from "@/lib/api/projects";
import { cn } from "@/lib/utils";

type ProjectsNavProps = { pathname: string };

/**
 * The sidebar outside a project: the way to the projects screen, every project of the workspace
 * folded under that entry, and the settings. Inside a project the sidebar is that project's own
 * menu (`ProjectNav`).
 */
export function ProjectsNav({ pathname }: ProjectsNavProps) {
  const [projectsOpen, setProjectsOpen] = useState(true);

  return (
    <nav
      aria-label="Main"
      className="flex gap-1 overflow-x-auto px-3 pb-3 lg:min-h-0 lg:flex-1 lg:flex-col lg:overflow-visible lg:pt-2"
    >
      <Collapsible
        open={projectsOpen}
        onOpenChange={setProjectsOpen}
        className="flex min-w-0 shrink-0 gap-1 lg:min-h-0 lg:flex-1 lg:flex-col"
      >
        <div className="flex shrink-0 items-center gap-0.5">
          <NavLink href="/" current={pathname === "/"}>
            <LayoutGridIcon aria-hidden="true" />
            Projects
          </NavLink>
          <CollapsibleTrigger
            aria-label={projectsOpen ? "Collapse projects" : "Expand projects"}
            className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground lg:size-7"
          >
            <ChevronRightIcon
              aria-hidden="true"
              className={cn("size-3.5 transition-transform", projectsOpen && "rotate-90")}
            />
          </CollapsibleTrigger>
        </div>
        <CollapsibleContent className="flex gap-1 lg:block lg:min-h-0 lg:flex-1 lg:overflow-y-auto">
          <ProjectsList />
        </CollapsibleContent>
      </Collapsible>
      <div className="ml-auto lg:mt-auto lg:ml-0">
        <SettingsLink current={pathname.startsWith("/settings")} />
      </div>
    </nav>
  );
}

/**
 * Every project of the workspace, newest first, a page at a time: a chip beside the Projects entry
 * on a narrow screen, a row under it otherwise.
 */
function ProjectsList() {
  const projects = useShellProjects();

  if (projects.data === undefined) {
    return projects.error !== null ? (
      <p className="shrink-0 px-2.5 py-1.5 text-xs text-muted-foreground">The projects could not be read.</p>
    ) : (
      <Skeleton className="mx-2.5 h-8 w-40 shrink-0 lg:my-1 lg:w-auto" />
    );
  }
  if (projects.data.length === 0) {
    return <p className="shrink-0 px-2.5 py-1.5 text-xs text-muted-foreground">No projects yet.</p>;
  }

  return (
    <ul
      aria-label="Projects"
      className="flex gap-1 lg:grid lg:grid-cols-[minmax(0,1fr)] lg:gap-0.5 lg:py-1"
    >
      {projects.data.map((project) => (
        <li key={project.id} className="shrink-0 lg:min-w-0">
          <NavLink href={`/projects/${project.id}`} current={false}>
            <span className="min-w-0 truncate" title={project.name}>
              {project.name}
            </span>
          </NavLink>
        </li>
      ))}
      {projects.hasNextPage && (
        <li className="shrink-0">
          <button
            type="button"
            onClick={() => void projects.fetchNextPage()}
            disabled={projects.isFetchingNextPage}
            className="flex h-8 items-center rounded-md px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50 lg:w-full"
          >
            {projects.isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        </li>
      )}
    </ul>
  );
}
