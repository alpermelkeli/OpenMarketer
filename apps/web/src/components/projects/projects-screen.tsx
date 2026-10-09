"use client";

import { PlusIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { LoadMore } from "@/components/common/load-more";
import { PageHeader } from "@/components/common/page-header";
import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateProject, useProjects } from "@/lib/api/projects";
import type { CreateProjectRequest } from "@/lib/api/types";
import { createProjectErrors } from "@/lib/projects/create-project-errors";

import { NewProjectForm } from "./new-project-form";
import { ProjectList } from "./project-list";
import { ProjectsEmptyState } from "./projects-empty-state";

/** The projects screen: every project of the workspace, and the form that adds one. */
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

  const isEmpty = projects.data !== undefined && projects.data.length === 0;

  return (
    <div className="grid gap-10">
      <PageHeader
        title="Projects"
        description="A project is one product and the repository its code lives in."
        actions={
          projects.data !== undefined &&
          !isEmpty &&
          !creating && (
            <Button size="lg" onClick={() => setCreating(true)}>
              <PlusIcon data-icon="inline-start" aria-hidden="true" />
              New project
            </Button>
          )
        }
      />

      {projects.error !== null && (
        <RequestProblem
          title="The projects could not be read"
          error={projects.error}
          onRetry={() => void projects.refetch()}
        />
      )}

      {projects.data === undefined && projects.error === null && (
        <div className="grid gap-3" aria-busy="true" aria-label="Loading projects">
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </div>
      )}

      {isEmpty && <ProjectsEmptyState>{form()}</ProjectsEmptyState>}

      {projects.data !== undefined && !isEmpty && (
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
          <div>
            <ProjectList projects={projects.data} />
            <LoadMore
              noun="projects"
              hasMore={projects.hasNextPage}
              loading={projects.isFetchingNextPage}
              onLoadMore={() => void projects.fetchNextPage()}
            />
          </div>
        </>
      )}
    </div>
  );
}
