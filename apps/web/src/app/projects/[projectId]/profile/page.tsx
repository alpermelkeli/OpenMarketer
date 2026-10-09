import type { Metadata } from "next";
import { Suspense } from "react";

import { ProfileReviewScreen } from "@/components/profile/profile-review-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId } from "@/lib/remembered/remembered";

export const metadata: Metadata = { title: "Profile" };

type Props = PageProps<"/projects/[projectId]/profile">;

export default function ProfilePage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-96" />}>
      <ProfileOfProject params={params} />
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary.
async function ProfileOfProject({ params }: Pick<Props, "params">) {
  const { projectId } = await params;
  // The frame around this page already says when the address holds no project ID.
  return isProjectId(projectId) ? <ProfileReviewScreen projectId={projectId} /> : null;
}
