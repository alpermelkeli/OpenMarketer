"use client";

import { Trash2Icon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useDeleteChat } from "@/lib/api/chats";
import { errorMessage } from "@/lib/api/error-message";
import { projectPageHref } from "@/lib/projects/project-navigation";

import { DeleteChatDialog } from "./delete-chat-dialog";

/** A small button that asks first and then deletes the chat; once it is gone, a new one is shown. */
export function DeleteChatButton({
  projectId,
  chatId,
  title,
  answering,
}: {
  projectId: string;
  chatId: string;
  title: string;
  /** Whether a turn is being answered in this chat; deleting then would only fail the turn. */
  answering: boolean;
}) {
  const router = useRouter();
  const deleteChat = useDeleteChat(projectId);
  const [open, setOpen] = useState(false);

  function openDialog() {
    deleteChat.reset();
    setOpen(true);
  }

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        onClick={openDialog}
        disabled={answering}
        title={answering ? "The chat can be deleted when the answer has been stored" : "Delete the chat"}
      >
        <Trash2Icon aria-hidden="true" />
        <span className="sr-only">Delete the chat</span>
      </Button>
      <DeleteChatDialog
        open={open}
        onOpenChange={setOpen}
        title={title}
        answering={answering}
        pending={deleteChat.isPending || deleteChat.isSuccess}
        problem={deleteChat.error === null ? null : errorMessage(deleteChat.error)}
        onDelete={() =>
          deleteChat.mutate(chatId, {
            onSuccess: () => router.replace(projectPageHref(projectId, "chat")),
          })
        }
      />
    </>
  );
}
