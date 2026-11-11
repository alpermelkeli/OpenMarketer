import type { Metadata } from "next";
import { Suspense } from "react";

import { AccountsScreen } from "@/components/accounts/accounts-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Accounts" };

type Props = PageProps<"/projects/[projectId]/accounts">;

export default function AccountsPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <AccountsOfProject params={params} />
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary; so is the
// outcome of a connection the screen reads from its address.
async function AccountsOfProject({ params }: Pick<Props, "params">) {
  const { projectId } = await params;
  // The frame around this page already says when the address holds no project ID.
  return isProjectId(projectId) ? <AccountsScreen projectId={projectId} /> : null;
}
