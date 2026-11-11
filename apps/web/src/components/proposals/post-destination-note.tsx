import { ArrowRightIcon, SendIcon } from "lucide-react";
import Link from "next/link";

import type { PostDestination } from "@/lib/api/types";
import { projectPageHref } from "@/lib/projects/project-navigation";
import { postDestinationDisplay } from "@/lib/proposals/proposal-display";
import { cn } from "@/lib/utils";

type PostDestinationNoteProps = {
  projectId: string;
  destination: PostDestination | null;
  /** In the notifications' short list: the sentence alone, without the way to the accounts. */
  compact?: boolean;
  className?: string;
};

/**
 * Where a writer's post goes once approved, said before the person decides: the account it is
 * published from with what that is reckoned to cost, or that it is a dry run, with the way to the
 * project's accounts. What the server read from the proposal; nothing here decides it.
 */
export function PostDestinationNote({ projectId, destination, compact = false, className }: PostDestinationNoteProps) {
  const display = postDestinationDisplay(destination);
  if (display === null) {
    return (
      <p className={cn("text-sm text-muted-foreground", className)}>
        Where it would be published could not be read from the proposal.
      </p>
    );
  }
  if (display.live) {
    return (
      <p className={cn("flex items-center gap-1.5 text-sm", className)}>
        <SendIcon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
        <span>{display.text}</span>
      </p>
    );
  }
  return (
    <p className={cn("flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground", className)}>
      <span>{display.text}</span>
      {!compact && (
        <Link
          href={projectPageHref(projectId, "accounts")}
          className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
        >
          Connect an X account for the next post
          <ArrowRightIcon aria-hidden="true" className="size-3.5" />
        </Link>
      )}
    </p>
  );
}
