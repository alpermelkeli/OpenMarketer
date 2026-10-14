import type { Metadata } from "next";
import { Suspense } from "react";

import { ApprovalsScreen } from "@/components/proposals/approvals-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Approvals" };

type Props = PageProps<"/projects/[projectId]/approvals">;

export default function ApprovalsPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <ApprovalsOfProject params={params} />
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary.
async function ApprovalsOfProject({ params }: Pick<Props, "params">) {
  const { projectId } = await params;
  // The frame around this page already says when the address holds no project ID.
  return isProjectId(projectId) ? <ApprovalsScreen projectId={projectId} /> : null;
}
