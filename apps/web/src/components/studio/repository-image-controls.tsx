"use client";

import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { errorMessage } from "@/lib/api/error-message";
import { useRemoveRepositoryImage, useSetRepositoryImageKind } from "@/lib/api/repository-images";
import type { RepositoryImage, RepositoryImageKind } from "@/lib/api/types";
import { REPOSITORY_IMAGE_KIND_OPTIONS, repositoryImageRemoval } from "@/lib/studio/repository-images";

import { RemoveRepositoryImageDialog } from "./remove-repository-image-dialog";

type RepositoryImageControlsProps = { projectId: string; image: RepositoryImage };

/**
 * What a person can change about one image: its kind, saved as soon as it is picked, and whether
 * the studio may use it at all, after a step that says what removing it does.
 */
export function RepositoryImageControls({ projectId, image }: RepositoryImageControlsProps) {
  const id = useId();
  const setKind = useSetRepositoryImageKind(projectId);
  const remove = useRemoveRepositoryImage(projectId);
  const [confirming, setConfirming] = useState(false);
  const shownKind = setKind.isPending ? setKind.variables.kind : image.kind;

  function chooseKind(next: RepositoryImageKind | null) {
    if (next === null || next === image.kind || setKind.isPending) return;
    setKind.mutate({ imageId: image.id, kind: next });
  }

  function openRemoval() {
    remove.reset();
    setConfirming(true);
  }

  return (
    <div className="grid gap-2">
      <div className="flex items-end gap-2">
        <div className="grid min-w-0 flex-1 gap-1">
          <Label id={`${id}-kind`} className="text-xs text-muted-foreground">
            Kind
          </Label>
          <Select
            items={REPOSITORY_IMAGE_KIND_OPTIONS}
            value={shownKind}
            onValueChange={(next) => chooseKind(next as RepositoryImageKind | null)}
            disabled={setKind.isPending}
          >
            <SelectTrigger
              size="sm"
              aria-labelledby={`${id}-kind`}
              aria-describedby={`${id}-kind-state`}
              className="w-full"
            >
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
        <Button type="button" variant="ghost" size="sm" onClick={openRemoval}>
          Remove
        </Button>
      </div>
      <div id={`${id}-kind-state`} aria-live="polite" className="text-xs empty:hidden">
        {setKind.isPending && <p className="text-muted-foreground">Saving the kind…</p>}
        {setKind.error !== null && (
          <p role="alert" className="text-pretty text-destructive">
            The kind was not saved. {errorMessage(setKind.error)}
          </p>
        )}
      </div>
      <RemoveRepositoryImageDialog
        open={confirming}
        onOpenChange={setConfirming}
        path={image.path}
        removal={repositoryImageRemoval(image)}
        pending={remove.isPending || remove.isSuccess}
        problem={remove.error === null ? null : errorMessage(remove.error)}
        onRemove={() => remove.mutate(image.id)}
      />
    </div>
  );
}
