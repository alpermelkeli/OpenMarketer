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

type RejectAssetDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onReject: () => void;
};

/** The step that rejects a draft. It says that a rejection is final and that the image stays in the library. */
export function RejectAssetDialog({ open, onOpenChange, pending, problem, onReject }: RejectAssetDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title">Reject this image?</AlertDialogTitle>
          <AlertDialogDescription>
            A rejected image is kept in the library, marked as rejected, and cannot be accepted afterwards. To try
            again, ask for a new image. Delete the run to remove it.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onReject} disabled={pending}>
            {pending ? "Rejecting…" : "Reject the image"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
