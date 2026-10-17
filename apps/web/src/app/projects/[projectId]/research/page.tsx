import type { Metadata } from "next";
import { Suspense } from "react";

import { ResearchScreen } from "@/components/research/research-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Research" };

type Props = PageProps<"/projects/[projectId]/research">;

export default function ResearchPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <ResearchOfProject params={params} />
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary.
async function ResearchOfProject({ params }: Pick<Props, "params">) {
  const { projectId } = await params;
  // The frame around this page already says when the address holds no project ID.
  return isProjectId(projectId) ? <ResearchScreen projectId={projectId} /> : null;
}
