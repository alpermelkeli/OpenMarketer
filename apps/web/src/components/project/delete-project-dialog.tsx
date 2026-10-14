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

type DeleteProjectDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  name: string;
  /** Whether an analysis of the project is queued or running; the API refuses the deletion then. */
  analysisInProgress: boolean;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onDelete: () => void;
};

/** The step that deletes a project. It says what goes with it, that approved versions do too, and that nothing comes back. */
export function DeleteProjectDialog({
  open,
  onOpenChange,
  name,
  analysisInProgress,
  pending,
  problem,
  onDelete,
}: DeleteProjectDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title break-words">Delete {name}?</AlertDialogTitle>
          <AlertDialogDescription>
            The project is deleted with its analyses, the evidence they found and every profile version, approved ones
            included. Nothing can be recovered. The repository itself is not touched, and it can be registered again
            as a new project with no history.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {analysisInProgress && (
          <p className="rounded-md border border-warning/45 bg-warning/8 px-3 py-2 text-sm text-pretty">
            An analysis of this project is in progress. The project can be deleted when it has finished.
          </p>
        )}
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onDelete} disabled={pending || analysisInProgress}>
            {pending ? "Deleting…" : "Delete the project"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
