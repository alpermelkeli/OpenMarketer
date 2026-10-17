"use client";

import { SendIcon } from "lucide-react";
import { type KeyboardEvent, useId } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type ComposerProps = {
  value: string;
  onChange: (value: string) => void;
  onSend: (message: string) => void;
  /** While a message is being answered, another one waits: the chat answers one at a time. */
  busy: boolean;
  placeholder: string;
  /** Why nothing can be written now, such as a turn the orchestrator started itself; said in the field. */
  waiting?: string;
  className?: string;
};

/** Where a message to the orchestrator is written. Enter sends it; Shift+Enter starts a new line. */
export function Composer({ value, onChange, onSend, busy, placeholder, waiting, className }: ComposerProps) {
  const id = useId();
  const message = value.trim();
  const canSend = message !== "" && !busy && waiting === undefined;

  function send() {
    if (canSend) onSend(message);
  }

  function sendOnEnter(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    send();
  }

  return (
    <form
      className={cn("grid w-full gap-2", className)}
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <label htmlFor={id} className="sr-only">
        Message to the orchestrator
      </label>
      <div className="flex items-end gap-2 rounded-md border bg-card p-2 shadow-sm">
        <Textarea
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={sendOnEnter}
          placeholder={waiting ?? placeholder}
          disabled={waiting !== undefined}
          className="max-h-60 min-h-20 min-w-0 resize-none border-0 shadow-none focus-visible:ring-0"
        />
        <Button type="submit" size="icon" disabled={!canSend}>
          <SendIcon aria-hidden="true" />
          <span className="sr-only">Send</span>
        </Button>
      </div>
      {waiting !== undefined && value !== "" && <p className="text-xs text-muted-foreground">{waiting}</p>}
    </form>
  );
}
