"use client";

import { useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useUploadRepositoryImage } from "@/lib/api/repository-images";
import type { RepositoryImageKind } from "@/lib/api/types";
import {
  DEFAULT_UPLOAD_KIND,
  UPLOAD_IMAGE_TYPES,
  readFileAsBase64,
  uploadOf,
  uploadProblem,
  uploadRefusal,
} from "@/lib/studio/repository-image-upload";
import { REPOSITORY_IMAGE_KIND_OPTIONS } from "@/lib/studio/repository-images";

type AddRepositoryImageProps = { projectId: string };

/**
 * Adding an image to the project: a file, the kind it is, and a button. The file is checked in
 * the browser before anything is sent, read here into base64 and uploaded; what the API refuses is
 * shown in its own words.
 */
export function AddRepositoryImage({ projectId }: AddRepositoryImageProps) {
  const id = useId();
  const upload = useUploadRepositoryImage(projectId);
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<RepositoryImageKind>(DEFAULT_UPLOAD_KIND);
  const [reading, setReading] = useState(false);
  const [readProblem, setReadProblem] = useState<string | null>(null);
  // A new key empties the file input once its image is stored.
  const [inputKey, setInputKey] = useState(0);
  const refusal = file === null ? null : uploadRefusal(file);
  const busy = reading || upload.isPending;
  const problem = refusal ?? readProblem ?? (upload.error === null ? null : uploadProblem(upload.error));

  function chooseFile(next: File | null) {
    upload.reset();
    setReadProblem(null);
    setFile(next);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (file === null || refusal !== null || busy) return;
    setReading(true);
    setReadProblem(null);
    let dataBase64: string;
    try {
      dataBase64 = await readFileAsBase64(file);
    } catch {
      setReadProblem("This browser could not read the file. Choose it again, or another one.");
      return;
    } finally {
      setReading(false);
    }
    upload.mutate(uploadOf(file.name, kind, dataBase64), {
      onSuccess: () => {
        setFile(null);
        setKind(DEFAULT_UPLOAD_KIND);
        setInputKey((key) => key + 1);
      },
    });
  }

  return (
    <form onSubmit={submit} className="grid max-w-prose gap-4" noValidate aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="text-2xs tracking-widest text-muted-foreground uppercase">
        Add an image
      </h3>
      <div className="grid gap-4 sm:grid-cols-[1fr_12rem]">
        <div className="grid gap-2">
          <Label htmlFor={`${id}-file`}>Image file</Label>
          <Input
            key={inputKey}
            id={`${id}-file`}
            type="file"
            accept={UPLOAD_IMAGE_TYPES.join(",")}
            onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
            disabled={busy}
            aria-invalid={refusal !== null}
            aria-describedby={`${id}-hint ${id}-state`}
          />
        </div>
        <div className="grid gap-2">
          <Label id={`${id}-kind`}>Kind</Label>
          <Select
            items={REPOSITORY_IMAGE_KIND_OPTIONS}
            value={kind}
            onValueChange={(next) => {
              if (next !== null) setKind(next as RepositoryImageKind);
            }}
            disabled={busy}
          >
            <SelectTrigger aria-labelledby={`${id}-kind`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {REPOSITORY_IMAGE_KIND_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
        A PNG, JPEG or WebP image of up to 8 MiB. It is stored with the project, listed here as uploaded, and the
        studio may use it as a reference. A project holds up to 24 uploads.
      </p>
      <div id={`${id}-state`} aria-live="polite" className="text-sm empty:hidden">
        {problem !== null && (
          <p role="alert" className="text-pretty text-destructive">
            {problem}
          </p>
        )}
        {upload.isSuccess && file === null && <p className="text-muted-foreground">The image was added.</p>}
      </div>
      <div>
        <Button type="submit" disabled={file === null || refusal !== null || busy}>
          {reading ? "Reading the file…" : upload.isPending ? "Uploading…" : "Add the image"}
        </Button>
      </div>
    </form>
  );
}
