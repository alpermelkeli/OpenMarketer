"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { AssetRequest, Platform } from "@/lib/api/types";
import { fieldProblems, type FormProblems } from "@/lib/settings/form-problems";
import { EMPTY_STUDIO_REQUEST, assetRequestOf, type StudioRequestForm } from "@/lib/studio/request-form";
import { PLATFORM_OPTIONS } from "@/lib/studio/status";

type NewImageFormProps = {
  problems: FormProblems;
  pending: boolean;
  onSubmit: (request: AssetRequest) => void;
};

/** What the studio is asked to make: a goal, the exact words, a platform and notes on style. */
export function NewImageForm({ problems, pending, onSubmit }: NewImageFormProps) {
  const id = useId();
  const [form, setForm] = useState<StudioRequestForm>(EMPTY_STUDIO_REQUEST);
  const goalProblems = fieldProblems(problems, "goal");
  const copyProblems = fieldProblems(problems, "exact_copy");
  const platformProblems = fieldProblems(problems, "platform");
  const styleProblems = fieldProblems(problems, "style_notes");

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    onSubmit(assetRequestOf(form));
  }

  return (
    <form onSubmit={submit} className="grid max-w-prose gap-5" autoComplete="off" noValidate>
      <div className="grid gap-2">
        <Label htmlFor={`${id}-goal`}>What the image is for</Label>
        <Textarea
          id={`${id}-goal`}
          value={form.goal}
          onChange={(event) => setForm({ ...form, goal: event.target.value })}
          placeholder="Announce the 2.0 release to developers who already use the app"
          maxLength={500}
          rows={2}
          disabled={pending}
          aria-invalid={goalProblems.length > 0}
          aria-describedby={`${id}-goal-errors`}
        />
        <FieldErrors id={`${id}-goal-errors`} messages={goalProblems} />
      </div>

      <div className="grid gap-2">
        <Label htmlFor={`${id}-copy`}>Exact words, one line each</Label>
        <Textarea
          id={`${id}-copy`}
          value={form.copy}
          onChange={(event) => setForm({ ...form, copy: event.target.value })}
          placeholder={"Version 2.0 is here\nTry it free"}
          rows={3}
          disabled={pending}
          aria-invalid={copyProblems.length > 0}
          aria-describedby={`${id}-copy-hint ${id}-copy-errors`}
        />
        <p id={`${id}-copy-hint`} className="text-xs text-pretty text-muted-foreground">
          Up to six lines. The image must show each exactly as written, and code checks that it does. Leave it empty
          for an image without words.
        </p>
        <FieldErrors id={`${id}-copy-errors`} messages={copyProblems} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label id={`${id}-platform-label`}>Platform</Label>
          <Select
            items={PLATFORM_OPTIONS}
            value={form.platform}
            onValueChange={(next) => {
              if (next !== null) setForm({ ...form, platform: next as Platform });
            }}
            disabled={pending}
          >
            <SelectTrigger
              aria-labelledby={`${id}-platform-label`}
              aria-invalid={platformProblems.length > 0}
              aria-describedby={`${id}-platform-errors`}
              className="w-full"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PLATFORM_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldErrors id={`${id}-platform-errors`} messages={platformProblems} />
        </div>
        <div className="grid gap-2">
          <Label htmlFor={`${id}-style`}>Notes on style (optional)</Label>
          <Input
            id={`${id}-style`}
            value={form.styleNotes}
            onChange={(event) => setForm({ ...form, styleNotes: event.target.value })}
            placeholder="Calm, lots of space, the brand's blue"
            maxLength={500}
            disabled={pending}
            aria-invalid={styleProblems.length > 0}
            aria-describedby={`${id}-style-errors`}
          />
          <FieldErrors id={`${id}-style-errors`} messages={styleProblems} />
        </div>
      </div>

      <FieldErrors id={`${id}-form-errors`} messages={problems.form} />
      <div className="grid gap-2">
        <p className="text-sm text-pretty text-muted-foreground">
          The run makes, checks and edits candidates until one passes or it reaches its limits, then stores the best
          as a draft for you to accept or reject. Generating an image costs money: within the creative budget&rsquo;s
          automatic limit it runs at once, above it the run asks for your approval first.
        </p>
        <div>
          <Button type="submit" disabled={pending}>
            {pending ? "Starting…" : "Start the run"}
          </Button>
        </div>
      </div>
    </form>
  );
}
