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

type DeleteStudioRunDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  runId: string;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onDelete: () => void;
};

/** The step that deletes one studio run. It says that its image goes too, accepted or not, and that nothing comes back. */
export function DeleteStudioRunDialog({ open, onOpenChange, runId, pending, problem, onDelete }: DeleteStudioRunDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title break-words">
            Delete studio run {shortId(runId)}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            The run is deleted with its asset and the image of every version it made, even an accepted one. Nothing
            can be recovered. A copy you downloaded is not touched.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onDelete} disabled={pending}>
            {pending ? "Deleting…" : "Delete the run"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
