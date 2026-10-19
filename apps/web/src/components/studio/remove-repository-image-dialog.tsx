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
import type { RepositoryImageRemoval } from "@/lib/studio/repository-images";

type RemoveRepositoryImageDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The image's path or name, shown as text. */
  path: string;
  removal: RepositoryImageRemoval;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onRemove: () => void;
};

/** The step that removes one repository image. It says first whether the image is deleted or only hidden. */
export function RemoveRepositoryImageDialog({
  open,
  onOpenChange,
  path,
  removal,
  pending,
  problem,
  onRemove,
}: RemoveRepositoryImageDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title">{removal.title}</AlertDialogTitle>
          <AlertDialogDescription>
            <span className="block truncate font-mono text-xs" title={path}>
              {path}
            </span>
            <span className="mt-2 block text-pretty">{removal.description}</span>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onRemove} disabled={pending}>
            {pending ? removal.pending : removal.action}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
