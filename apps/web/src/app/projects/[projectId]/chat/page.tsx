import type { Metadata } from "next";
import { Suspense } from "react";

import { NewChatScreen } from "@/components/chat/new-chat-screen";
import { Skeleton } from "@/components/ui/skeleton";
import { isProjectId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "New chat" };

type Props = PageProps<"/projects/[projectId]/chat">;

export default function ChatPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <ChatOfProject params={params} />
    </Suspense>
  );
}

// The project ID is known only at request time, so it is read inside the boundary.
async function ChatOfProject({ params }: Pick<Props, "params">) {
  const { projectId } = await params;
  // The frame around this page already says when the address holds no project ID.
  return isProjectId(projectId) ? <NewChatScreen projectId={projectId} /> : null;
}
