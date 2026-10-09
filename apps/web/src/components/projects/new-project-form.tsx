"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { CreateProjectRequest } from "@/lib/api/types";
import type { CreateProjectErrors } from "@/lib/projects/create-project-errors";

type NewProjectFormProps = {
  errors: CreateProjectErrors;
  pending: boolean;
  onSubmit: (project: CreateProjectRequest) => void;
  onCancel?: () => void;
};

/** Name and repository URL of a new project. What is acceptable is the API's to say. */
export function NewProjectForm({ errors, pending, onSubmit, onCancel }: NewProjectFormProps) {
  const id = useId();
  const [name, setName] = useState("");
  const [repositoryUrl, setRepositoryUrl] = useState("");

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit({ name: name.trim(), repository_url: repositoryUrl.trim() });
  }

  return (
    <form onSubmit={submit} className="grid gap-5" noValidate>
      <div className="grid gap-2">
        <Label htmlFor={`${id}-name`}>Name</Label>
        <Input
          id={`${id}-name`}
          name="name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Memoria"
          autoComplete="off"
          maxLength={200}
          required
          aria-invalid={errors.name.length > 0}
          aria-describedby={`${id}-name-errors`}
        />
        <FieldErrors id={`${id}-name-errors`} messages={errors.name} />
      </div>
      <div className="grid gap-2">
        <Label htmlFor={`${id}-url`}>Repository URL</Label>
        <Input
          id={`${id}-url`}
          name="repository_url"
          type="url"
          inputMode="url"
          value={repositoryUrl}
          onChange={(event) => setRepositoryUrl(event.target.value)}
          placeholder="https://github.com/owner/name"
          autoComplete="off"
          spellCheck={false}
          maxLength={2000}
          required
          className="font-mono"
          aria-invalid={errors.repositoryUrl.length > 0}
          aria-describedby={`${id}-url-hint ${id}-url-errors`}
        />
        <p id={`${id}-url-hint`} className="text-xs text-muted-foreground">
          An https:// address. Nothing is cloned until you start an analysis.
        </p>
        <FieldErrors id={`${id}-url-errors`} messages={errors.repositoryUrl} />
      </div>
      <FieldErrors id={`${id}-form-errors`} messages={errors.form} />
      <div className="flex gap-2">
        <Button type="submit" size="lg" disabled={pending}>
          {pending ? "Creating…" : "Create project"}
        </Button>
        {onCancel && (
          <Button type="button" size="lg" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}
