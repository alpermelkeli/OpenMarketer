"use client";

import { PlusIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Notice } from "@/components/common/notice";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateProject } from "@/lib/api/projects";
import type { CreateProjectRequest } from "@/lib/api/types";
import { createProjectErrors } from "@/lib/projects/create-project-errors";
import { withOpenedProject, withProject, withoutProject } from "@/lib/remembered/remembered";
import { useRemembered } from "@/lib/remembered/use-remembered";

import { NewProjectForm } from "./new-project-form";
import { OpenProjectForm } from "./open-project-form";
import { ProjectList } from "./project-list";
import { ProjectsEmptyState } from "./projects-empty-state";

/** The projects screen: what this browser remembers, and the ways to add to it. */
export function ProjectsScreen() {
  const router = useRouter();
  const { remembered, update } = useRemembered();
  const createProject = useCreateProject();
  const [creating, setCreating] = useState(false);

  function create(request: CreateProjectRequest) {
    createProject.mutate(request, {
      onSuccess: (project) => {
        update((current) => withProject(current, project));
        router.push(`/projects/${project.id}/analyses`);
      },
    });
  }

  function open(projectId: string) {
    update((current) => withOpenedProject(current, projectId));
    router.push(`/projects/${projectId}`);
  }

  const form = (onCancel?: () => void) => (
    <NewProjectForm
      errors={createProjectErrors(createProject.error)}
      pending={createProject.isPending}
      onSubmit={create}
      onCancel={onCancel}
    />
  );

  const isEmpty =
    remembered !== null && remembered.projects.length === 0 && remembered.openedProjectIds.length === 0;

  return (
    <div className="grid gap-10">
      <PageHeader
        title="Projects"
        description="A project is one product and the repository its code lives in."
        actions={
          !isEmpty &&
          !creating && (
            <Button size="lg" onClick={() => setCreating(true)}>
              <PlusIcon data-icon="inline-start" aria-hidden="true" />
              New project
            </Button>
          )
        }
      />

      {remembered === null && <Skeleton className="h-40" />}

      {isEmpty && <ProjectsEmptyState>{form()}</ProjectsEmptyState>}

      {remembered !== null && !isEmpty && (
        <>
          {creating && (
            <section aria-labelledby="new-project" className="max-w-prose animate-enter rounded-md border bg-card p-6">
              <h2 id="new-project" className="mb-5 font-heading text-lg">
                New project
              </h2>
              {form(() => {
                setCreating(false);
                createProject.reset();
              })}
            </section>
          )}
          <ProjectList
            projects={remembered.projects}
            openedProjectIds={remembered.openedProjectIds}
            onForget={(projectId) => update((current) => withoutProject(current, projectId))}
          />
        </>
      )}

      {remembered !== null && (
        <section aria-labelledby="open-by-id" className="grid gap-4 border-t pt-8">
          <div>
            <h2 id="open-by-id" className="font-heading text-lg">
              Open a project by ID
            </h2>
            <p className="mt-1 max-w-prose text-sm text-pretty text-muted-foreground">
              For a project made somewhere else, such as the command line or another browser.
            </p>
          </div>
          <OpenProjectForm onOpen={open} />
          <Notice title="Why this list may be incomplete" className="max-w-prose">
            The API cannot list projects yet, so this page shows only the projects created or opened in this
            browser. They are remembered in the browser&rsquo;s storage; &ldquo;Forget&rdquo; removes one from this
            list and leaves the project itself untouched.
          </Notice>
        </section>
      )}
    </div>
  );
}
