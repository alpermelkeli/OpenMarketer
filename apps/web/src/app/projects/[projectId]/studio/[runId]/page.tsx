import type { Metadata } from "next";
import { Suspense } from "react";

import { Notice } from "@/components/common/notice";
import { StudioRunScreen } from "@/components/studio/studio-run-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId, isStudioRunId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Studio" };

type Props = PageProps<"/projects/[projectId]/studio/[runId]">;

export default function StudioRunPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <StudioRunOfProject params={params} />
    </Suspense>
  );
}

// The IDs are known only at request time, so they are read inside the boundary.
async function StudioRunOfProject({ params }: Pick<Props, "params">) {
  const { projectId, runId } = await params;
  // The frame around this page already says when the address holds no project ID.
  if (!isProjectId(projectId)) return null;
  if (!isStudioRunId(runId)) {
    return (
      <Notice tone="danger" title="No such studio run">
        <p>This address does not name a studio run.</p>
      </Notice>
    );
  }
  return <StudioRunScreen projectId={projectId} runId={runId} />;
}
