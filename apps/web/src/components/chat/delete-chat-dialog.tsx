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

type DeleteChatDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** Whether a turn is being answered in this chat; the API would then store nothing of it. */
  answering: boolean;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onDelete: () => void;
};

/** The step that deletes one chat. It says that its messages go too and that nothing comes back. */
export function DeleteChatDialog({
  open,
  onOpenChange,
  title,
  answering,
  pending,
  problem,
  onDelete,
}: DeleteChatDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title break-words">Delete {title}?</AlertDialogTitle>
          <AlertDialogDescription>
            The chat is deleted with every message it holds. Nothing can be recovered. A research run it asked for is
            not touched.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {answering && (
          <p className="rounded-md border border-warning/45 bg-warning/8 px-3 py-2 text-sm text-pretty">
            The orchestrator is answering a message in this chat. The chat can be deleted when the answer has been
            stored.
          </p>
        )}
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onDelete} disabled={pending || answering}>
            {pending ? "Deleting…" : "Delete the chat"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
