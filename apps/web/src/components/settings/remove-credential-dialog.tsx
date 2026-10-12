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

type RemoveCredentialDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** What the credential is called: "GitHub token". */
  label: string;
  /** What no longer works without the stored value, in the dashboard's own words. */
  withoutIt: string;
  /** The environment variable whose value is in use instead of the stored one, if that is so. */
  overriddenBy: string | null;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onRemove: () => void;
};

/** The step that deletes a stored credential. It says what stops working, and that the value is gone for good. */
export function RemoveCredentialDialog({
  open,
  onOpenChange,
  label,
  withoutIt,
  overriddenBy,
  pending,
  problem,
  onRemove,
}: RemoveCredentialDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title">Remove the stored {label}?</AlertDialogTitle>
          <AlertDialogDescription className="[&_code]:font-mono [&_code]:text-xs [&_code]:text-foreground">
            {overriddenBy === null ? (
              withoutIt
            ) : (
              <>
                The stored value is not in use: <code>{overriddenBy}</code> in the API&rsquo;s environment sets this
                credential, and that is not touched. Nothing changes now; if the variable is unset later, no value
                will be left.
              </>
            )}{" "}
            The stored value is deleted and cannot be recovered. To use it again you enter it again.
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
            {pending ? "Removing…" : "Remove the stored value"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
