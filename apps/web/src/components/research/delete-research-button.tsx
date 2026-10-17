"use client";

import { Trash2Icon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { isRunFinished } from "@/lib/analysis/run-status";
import { errorMessage } from "@/lib/api/error-message";
import { useDeleteResearch } from "@/lib/api/research";
import type { ResearchRun } from "@/lib/api/types";

import { DeleteResearchDialog } from "./delete-research-dialog";

/**
 * A small button that asks first and then deletes a finished research run, its report and the
 * pages it fetched. From the run's own screen, `onDeleted` takes the person back to the list.
 */
export function DeleteResearchButton({ run, onDeleted }: { run: ResearchRun; onDeleted?: () => void }) {
  const deleteResearch = useDeleteResearch(run.project_id);
  const [open, setOpen] = useState(false);
  const finished = isRunFinished(run);

  function openDialog() {
    deleteResearch.reset();
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
        title={finished ? "Delete the research run" : "The run can be deleted when it has finished"}
      >
        <Trash2Icon aria-hidden="true" />
        <span className="sr-only">Delete the research run</span>
      </Button>
      <DeleteResearchDialog
        open={open}
        onOpenChange={setOpen}
        runId={run.id}
        unfinished={!finished}
        pending={deleteResearch.isPending || deleteResearch.isSuccess}
        problem={deleteResearch.error === null ? null : errorMessage(deleteResearch.error)}
        onDelete={() => deleteResearch.mutate(run.id, { onSuccess: onDeleted })}
      />
    </>
  );
}
