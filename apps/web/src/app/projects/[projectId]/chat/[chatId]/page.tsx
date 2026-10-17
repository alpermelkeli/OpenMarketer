import type { Metadata } from "next";
import { Suspense } from "react";

import { ChatThreadScreen } from "@/components/chat/chat-thread-screen";
import { Notice } from "@/components/common/notice";
import { Skeleton } from "@/components/ui/skeleton";
import { isChatId, isProjectId } from "@/lib/projects/project-id";

export const metadata: Metadata = { title: "Chat" };

type Props = PageProps<"/projects/[projectId]/chat/[chatId]">;

export default function ChatThreadPage({ params }: Props) {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <ChatOfProject params={params} />
    </Suspense>
  );
}

// The IDs are known only at request time, so they are read inside the boundary.
async function ChatOfProject({ params }: Pick<Props, "params">) {
  const { projectId, chatId } = await params;
  // The frame around this page already says when the address holds no project ID.
  if (!isProjectId(projectId)) return null;
  if (!isChatId(chatId)) {
    return (
      <Notice tone="danger" title="No such chat">
        <p>This address does not name a chat.</p>
      </Notice>
    );
  }
  return <ChatThreadScreen projectId={projectId} chatId={chatId} />;
}
