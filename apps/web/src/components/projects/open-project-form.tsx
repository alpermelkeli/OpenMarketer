"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { isProjectId } from "@/lib/remembered/remembered";

type OpenProjectFormProps = {
  onOpen: (projectId: string) => void;
};

/** Open a project that was created elsewhere (the CLI, another browser) by its ID. */
export function OpenProjectForm({ onOpen }: OpenProjectFormProps) {
  const id = useId();
  const [value, setValue] = useState("");
  const [rejected, setRejected] = useState(false);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const projectId = value.trim().toLowerCase();
    if (!isProjectId(projectId)) {
      setRejected(true);
      return;
    }
    onOpen(projectId);
  }

  return (
    <form onSubmit={submit} className="grid gap-2" noValidate>
      <Label htmlFor={`${id}-id`}>Project ID</Label>
      <div className="flex flex-wrap gap-2">
        <Input
          id={`${id}-id`}
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
            setRejected(false);
          }}
          placeholder="00000000-0000-0000-0000-000000000000"
          autoComplete="off"
          spellCheck={false}
          className="max-w-96 min-w-0 flex-1 font-mono"
          aria-invalid={rejected}
          aria-describedby={`${id}-errors`}
        />
        <Button type="submit" variant="outline">
          Open
        </Button>
      </div>
      <FieldErrors
        id={`${id}-errors`}
        messages={rejected ? ["A project ID is 32 hexadecimal digits in five groups, like 1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b."] : []}
      />
    </form>
  );
}
