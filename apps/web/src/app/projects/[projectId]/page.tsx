import { redirect } from "next/navigation";
import { Suspense, type ReactNode } from "react";

type Props = PageProps<"/projects/[projectId]">;

/** A project opens on its profile: reviewing it is what the dashboard is for. */
export default function ProjectPage({ params }: Props) {
  return (
    <Suspense>
      <ToProfile params={params} />
    </Suspense>
  );
}

async function ToProfile({ params }: Pick<Props, "params">): Promise<ReactNode> {
  const { projectId } = await params;
  redirect(`/projects/${encodeURIComponent(projectId)}/profile`);
}
