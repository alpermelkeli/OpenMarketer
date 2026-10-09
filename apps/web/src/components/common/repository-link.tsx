import { ArrowUpRightIcon } from "lucide-react";

import { httpsUrlOrNull } from "@/lib/profile/evidence-link";
import { cn } from "@/lib/utils";

type RepositoryLinkProps = {
  /** As stored: typed by a person or written by the command line, and not necessarily a web address. */
  repositoryUrl: string;
  className?: string;
};

/** A project's repository: a link when it is an https address, plain text for anything else. */
export function RepositoryLink({ repositoryUrl, className }: RepositoryLinkProps) {
  const href = httpsUrlOrNull(repositoryUrl);
  if (href === null) {
    return <span className={cn("font-mono break-all text-muted-foreground", className)}>{repositoryUrl}</span>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={cn(
        "inline-flex max-w-full items-center gap-1 rounded-sm font-mono text-muted-foreground hover:text-foreground",
        className,
      )}
    >
      <span className="truncate">{repositoryUrl}</span>
      <ArrowUpRightIcon aria-hidden="true" className="size-3 shrink-0" />
      <span className="sr-only">(opens the repository in a new tab)</span>
    </a>
  );
}
