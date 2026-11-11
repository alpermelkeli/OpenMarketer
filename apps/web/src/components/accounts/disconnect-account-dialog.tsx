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

type DisconnectAccountDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The platform's name as a person knows it: "X". */
  platformName: string;
  username: string;
  pending: boolean;
  /** Why the last attempt failed, in words for the person. */
  problem: string | null;
  onDisconnect: () => void;
};

/**
 * The step that disconnects an account. It says that the token is deleted here, and that the
 * platform keeps listing the app until the person revokes its access there.
 */
export function DisconnectAccountDialog({
  open,
  onOpenChange,
  platformName,
  username,
  pending,
  problem,
  onDisconnect,
}: DisconnectAccountDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title break-words">
            Disconnect @{username} from this project?
          </AlertDialogTitle>
          <AlertDialogDescription>
            The project forgets this {platformName} account, and the access token stored for it is deleted from this
            installation and cannot be recovered. {platformName} is not told: the app keeps its access there until you
            revoke it in {platformName}&rsquo;s own settings, under connected apps. To use the account again you
            connect it again.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onDisconnect} disabled={pending}>
            {pending ? "Disconnecting…" : "Disconnect the account"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
