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
import { effectNote } from "@/lib/proposals/proposal-display";
import type { Proposal } from "@/lib/api/types";

type DecideDialogProps = {
  proposal: Proposal;
  /** The decision being confirmed: `true` to approve, `false` to reject, `null` while closed. */
  approving: boolean | null;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  /** Why the last attempt failed, in words for the person deciding. */
  problem: string | null;
  onConfirm: () => void;
};

/** The deliberate step before an action an agent asked for runs, or is refused. Says what follows. */
export function DecideDialog({ proposal, approving, onOpenChange, pending, problem, onConfirm }: DecideDialogProps) {
  const approve = approving === true;
  const what = <span className="font-mono">{proposal.action_name}</span>;
  return (
    <AlertDialog open={approving !== null} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title">
            {approve ? "Approve this action?" : "Reject this action?"}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {approve ? (
              <>
                The agent&rsquo;s run will carry out {what} exactly as shown. {effectNote(proposal.effect)} Once it has
                run, it cannot be taken back from here.
              </>
            ) : (
              <>
                {what} will not run. The agent is told that a person rejected it, and may propose something else.
              </>
            )}{" "}
            A decision is recorded as yours and cannot be changed.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant={approve ? "default" : "destructive"} onClick={onConfirm} disabled={pending}>
            {pending ? (approve ? "Approving…" : "Rejecting…") : approve ? "Approve and run" : "Reject"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
