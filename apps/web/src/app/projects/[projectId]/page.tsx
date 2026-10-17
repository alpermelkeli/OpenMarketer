import { redirect } from "next/navigation";
import { Suspense, type ReactNode } from "react";

type Props = PageProps<"/projects/[projectId]">;

/** A project opens on a new chat: asking for something is where the work on a project starts. */
export default function ProjectPage({ params }: Props) {
  return (
    <Suspense>
      <ToChat params={params} />
    </Suspense>
  );
}

async function ToChat({ params }: Pick<Props, "params">): Promise<ReactNode> {
  const { projectId } = await params;
  redirect(`/projects/${encodeURIComponent(projectId)}/chat`);
}
