import type { Metadata } from "next";
import { Suspense } from "react";

import { Notice } from "@/components/common/notice";
import { Skeleton } from "@/components/ui/skeleton";
import { WritingRunScreen } from "@/components/writing/writing-run-screen";
import { isProjectId, isWritingRunId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Writing" };

type Props = PageProps<"/projects/[projectId]/writing/[runId]">;

export default function WritingRunPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <WritingRunOfProject params={params} />
    </Suspense>
  );
}

// The IDs are known only at request time, so they are read inside the boundary.
async function WritingRunOfProject({ params }: Pick<Props, "params">) {
  const { projectId, runId } = await params;
  // The frame around this page already says when the address holds no project ID.
  if (!isProjectId(projectId)) return null;
  if (!isWritingRunId(runId)) {
    return (
      <Notice tone="danger" title="No such writing run">
        <p>This address does not name a writing run.</p>
      </Notice>
    );
  }
  return <WritingRunScreen projectId={projectId} runId={runId} />;
}
