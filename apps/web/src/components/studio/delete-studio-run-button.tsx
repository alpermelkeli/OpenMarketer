"use client";

import { Trash2Icon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api/error-message";
import { useDeleteStudioRun } from "@/lib/api/studio";
import type { StudioRun } from "@/lib/api/types";
import { isStudioRunFinished } from "@/lib/studio/status";

import { DeleteStudioRunDialog } from "./delete-studio-run-dialog";

/** A small button that asks first and then deletes a finished studio run with its asset. */
export function DeleteStudioRunButton({ run, onDeleted }: { run: StudioRun; onDeleted: () => void }) {
  const deleteRun = useDeleteStudioRun(run.project_id);
  const [open, setOpen] = useState(false);
  const finished = isStudioRunFinished(run);

  function openDialog() {
    deleteRun.reset();
    setOpen(true);
  }

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        onClick={openDialog}
        disabled={!finished}
        title={finished ? "Delete the studio run" : "The run can be deleted when it has ended"}
      >
        <Trash2Icon aria-hidden="true" />
        <span className="sr-only">Delete the studio run</span>
      </Button>
      <DeleteStudioRunDialog
        open={open}
        onOpenChange={setOpen}
        runId={run.id}
        pending={deleteRun.isPending || deleteRun.isSuccess}
        problem={deleteRun.error === null ? null : errorMessage(deleteRun.error)}
        onDelete={() => deleteRun.mutate(run, { onSuccess: onDeleted })}
      />
    </>
  );
}
