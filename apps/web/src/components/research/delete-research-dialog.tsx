"use client";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { shortId } from "@/lib/format";

type DeleteResearchDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  runId: string;
  /** Whether the run is queued or running; the API refuses the deletion then. */
  unfinished: boolean;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onDelete: () => void;
};

/** The step that deletes one research run. It says that its report and pages go too, and that nothing comes back. */
export function DeleteResearchDialog({
  open,
  onOpenChange,
  runId,
  unfinished,
  pending,
  problem,
  onDelete,
}: DeleteResearchDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title break-words">
            Delete research run {shortId(runId)}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            The run is deleted with its report and the pages it fetched. Nothing can be recovered.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {unfinished && (
          <p className="rounded-md border border-warning/45 bg-warning/8 px-3 py-2 text-sm text-pretty">
            This run has not finished. It can be deleted when it has.
          </p>
        )}
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onDelete} disabled={pending || unfinished}>
            {pending ? "Deleting…" : "Delete the run"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
