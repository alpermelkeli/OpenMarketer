import Link from "next/link";

import { Button } from "@/components/ui/button";

type ProfileEmptyStateProps = { projectId: string };

/** A project that has no profile version yet: the next step is an analysis. */
export function ProfileEmptyState({ projectId }: ProfileEmptyStateProps) {
  return (
    <div className="grid animate-enter justify-items-start gap-4 py-6">
      <div>
        <h2 className="font-heading text-title">No profile yet</h2>
        <p className="mt-2 max-w-prose text-pretty text-muted-foreground">
          The API has no draft and no approved profile for this project. An analysis reads the repository and
          drafts one for you to review.
        </p>
      </div>
      <Button size="lg" nativeButton={false} render={<Link href={`/projects/${projectId}/analyses`} />}>
        Go to analyses
      </Button>
    </div>
  );
}
