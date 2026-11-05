"use client";

import { PlusIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateProject, useProjects } from "@/lib/api/projects";
import type { CreateProjectRequest } from "@/lib/api/types";
import { createProjectErrors } from "@/lib/projects/create-project-errors";

import { NewProjectForm } from "./new-project-form";
import { ProjectsIntro } from "./projects-intro";

/** The root screen: the landing that says what OpenMarketer does and starts a project. */
export function ProjectsScreen() {
  const router = useRouter();
  const projects = useProjects();
  const createProject = useCreateProject();
  const [creating, setCreating] = useState(false);

  function create(request: CreateProjectRequest) {
    createProject.mutate(request, {
      onSuccess: (project) => router.push(`/projects/${project.id}/analyses`),
    });
  }

  const form = (onCancel?: () => void) => (
    <NewProjectForm
      errors={createProjectErrors(createProject.error)}
      pending={createProject.isPending}
      onSubmit={create}
      onCancel={onCancel}
    />
  );

  const loading = projects.data === undefined && projects.error === null;
  const empty = projects.data !== undefined && projects.data.length === 0;

  return (
    <div className="grid gap-12">
      <section className="grid animate-enter gap-12 md:grid-cols-[1fr_minmax(0,22rem)] md:gap-16">
        <ProjectsIntro empty={empty} />
        {loading ? (
          <Skeleton className="h-72" />
        ) : empty ? (
          <Panel id="first-project" title="Your first project">
            {form()}
          </Panel>
        ) : creating ? (
          <Panel id="new-project" title="New project">
            {form(() => {
              setCreating(false);
              createProject.reset();
            })}
          </Panel>
        ) : (
          <Panel id="new-project" title="New project">
            <p className="max-w-prose text-pretty text-sm text-muted-foreground">
              A project is one product and the repository its code lives in.
            </p>
            <Button size="lg" className="mt-5" onClick={() => setCreating(true)}>
              <PlusIcon data-icon="inline-start" aria-hidden="true" />
              New project
            </Button>
          </Panel>
        )}
      </section>

      {projects.error !== null && (
        <RequestProblem
          title="The projects could not be read"
          error={projects.error}
          onRetry={() => void projects.refetch()}
        />
      )}
    </div>
  );
}

type PanelProps = { id: string; title: string; children: ReactNode };

/** One of the landing's two cards: the form that starts a project, or the way to it. */
function Panel({ id, title, children }: PanelProps) {
  return (
    <section aria-labelledby={id} className="self-start rounded-md border bg-card p-6">
      <h2 id={id} className="mb-5 font-heading text-lg">
        {title}
      </h2>
      {children}
    </section>
  );
}
