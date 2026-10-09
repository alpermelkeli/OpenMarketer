import { Suspense } from "react";

import { ProjectFrame } from "@/components/project/project-frame";
import { Skeleton } from "@/components/ui/skeleton";

export default function ProjectLayout({ children, params }: LayoutProps<"/projects/[projectId]">) {
  return (
    <Suspense fallback={<Skeleton className="h-64" />}>
      <ProjectFrameForRoute params={params}>{children}</ProjectFrameForRoute>
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary.
async function ProjectFrameForRoute({ children, params }: LayoutProps<"/projects/[projectId]">) {
  const { projectId } = await params;
  return <ProjectFrame projectId={projectId}>{children}</ProjectFrame>;
}
