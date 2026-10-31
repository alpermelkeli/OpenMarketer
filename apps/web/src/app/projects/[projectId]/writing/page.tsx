import type { Metadata } from "next";
import { Suspense } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { WritingScreen } from "@/components/writing/writing-screen";
import { isProjectId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Writing" };

type Props = PageProps<"/projects/[projectId]/writing">;

export default function WritingPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <WritingOfProject params={params} />
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary.
async function WritingOfProject({ params }: Pick<Props, "params">) {
  const { projectId } = await params;
  // The frame around this page already says when the address holds no project ID.
  return isProjectId(projectId) ? <WritingScreen projectId={projectId} /> : null;
}
