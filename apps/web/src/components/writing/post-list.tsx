import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { StatusPill } from "@/components/common/status-pill";
import type { ContentItem } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { writingHref } from "@/lib/projects/project-navigation";
import { cn } from "@/lib/utils";
import { postStatusDisplay, writingPlatformLabel } from "@/lib/writing/status";

type PostListProps = { posts: readonly ContentItem[] };

/**
 * The posts the project's runs wrote, newest first, each with what became of it. A post that still
 * waits for a person is drawn as a draft. The texts were written by a model: shown as text only.
 */
export function PostList({ posts }: PostListProps) {
  return (
    <ul className="grid gap-3">
      {posts.map((post) => {
        const status = postStatusDisplay(post.status);
        return (
          <li
            key={post.id}
            className={cn(
              "grid gap-3 rounded-md border p-4",
              status.draft ? "border-dashed border-warning/60" : "bg-card",
            )}
          >
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <StatusPill tone={status.tone}>{status.label}</StatusPill>
              <p className="text-sm">{writingPlatformLabel(post.platform)}</p>
              <p className="text-sm text-muted-foreground">
                Written {formatDateTime(post.created_at)} from profile version{" "}
                <span className="font-mono tabular-nums">{post.profile_version}</span>
              </p>
            </div>
            <p className="max-w-prose text-pretty break-words whitespace-pre-wrap">{post.text}</p>
            <p>
              <Link
                href={writingHref(post.project_id, post.run_id)}
                className="inline-flex items-center gap-1 rounded-sm text-sm text-brand underline-offset-4 hover:underline"
              >
                How it was written
                <ArrowRightIcon aria-hidden="true" className="size-3.5" />
              </Link>
            </p>
          </li>
        );
      })}
    </ul>
  );
}
