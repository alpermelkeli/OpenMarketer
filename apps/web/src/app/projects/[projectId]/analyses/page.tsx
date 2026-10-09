import type { Metadata } from "next";
import { Suspense } from "react";

import { AnalysesScreen } from "@/components/analyses/analyses-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId } from "@/lib/remembered/remembered";

export const metadata: Metadata = { title: "Analyses" };

type Props = PageProps<"/projects/[projectId]/analyses">;

export default function AnalysesPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <AnalysesOfProject params={params} />
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary.
async function AnalysesOfProject({ params }: Pick<Props, "params">) {
  const { projectId } = await params;
  // The frame around this page already says when the address holds no project ID.
  return isProjectId(projectId) ? <AnalysesScreen projectId={projectId} /> : null;
}
