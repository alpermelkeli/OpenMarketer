import type { Metadata } from "next";
import { Suspense } from "react";

import { Notice } from "@/components/common/notice";
import { ResearchRunScreen } from "@/components/research/research-run-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId, isResearchRunId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Research" };

type Props = PageProps<"/projects/[projectId]/research/[runId]">;

export default function ResearchRunPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <ResearchRunOfProject params={params} />
    </Suspense>
  );
}

// The IDs are known only at request time, so they are read inside the boundary.
async function ResearchRunOfProject({ params }: Pick<Props, "params">) {
  const { projectId, runId } = await params;
  // The frame around this page already says when the address holds no project ID.
  if (!isProjectId(projectId)) return null;
  if (!isResearchRunId(runId)) {
    return (
      <Notice tone="danger" title="No such research run">
        <p>This address does not name a research run.</p>
      </Notice>
    );
  }
  return <ResearchRunScreen projectId={projectId} runId={runId} />;
}
