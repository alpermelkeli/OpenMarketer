"use client";

import { Trash2Icon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api/error-message";
import { useDeleteProject } from "@/lib/api/projects";
import type { Project } from "@/lib/api/types";

import { DeleteProjectDialog } from "./delete-project-dialog";

/** A small button that asks first and then deletes the project; once it is gone, the projects are shown. */
export function DeleteProjectButton({ project }: { project: Project }) {
  const router = useRouter();
  const deleteProject = useDeleteProject(project.id);
  const [open, setOpen] = useState(false);

  function openDialog() {
    deleteProject.reset();
    setOpen(true);
  }

  return (
    <>
      <Button type="button" variant="ghost" size="icon-sm" onClick={openDialog} title="Delete the project">
        <Trash2Icon aria-hidden="true" />
        <span className="sr-only">Delete the project</span>
      </Button>
      <DeleteProjectDialog
        open={open}
        onOpenChange={setOpen}
        name={project.name}
        analysisInProgress={project.unfinished_run_id !== null}
        pending={deleteProject.isPending || deleteProject.isSuccess}
        problem={deleteProject.error === null ? null : errorMessage(deleteProject.error)}
        onDelete={() =>
          deleteProject.mutate(undefined, {
            onSuccess: () => router.replace("/"),
          })
        }
      />
    </>
  );
}
