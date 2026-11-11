import { ArrowRightIcon, ArrowUpRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import type { ContentItem } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { projectPageHref } from "@/lib/projects/project-navigation";
import { formatCost } from "@/lib/studio/status";
import { publicationOf, publishingFailureDisplay, unconfirmedPublishing } from "@/lib/writing/publication";

type PostPublicationProps = { post: ContentItem };

/**
 * What became of a post sent to the project's X account: where it is once published, why X refused
 * it with what to do, or a send that was never confirmed, which is a warning and not a failure.
 * Nothing for a post that was never sent.
 */
export function PostPublication({ post }: PostPublicationProps) {
  const publication = publicationOf(post);
  const failure = publishingFailureDisplay(post);
  const unconfirmed = unconfirmedPublishing(post);

  if (publication !== null) {
    return (
      <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        {publication.href !== null && (
          <a
            href={publication.href}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
          >
            View on X
            <ArrowUpRightIcon aria-hidden="true" className="size-3.5" />
            <span className="sr-only">(opens in a new tab)</span>
          </a>
        )}
        {publication.username !== null && <span className="font-mono">@{publication.username}</span>}
        {publication.publishedAt !== null && (
          <span className="text-muted-foreground">Published {formatDateTime(publication.publishedAt)}</span>
        )}
        {publication.costUsd !== null && (
          <span className="text-muted-foreground">
            Cost <span className="font-mono tabular-nums">{formatCost(publication.costUsd)}</span>
          </span>
        )}
      </p>
    );
  }

  if (unconfirmed !== null) {
    return (
      <Notice tone="warning" title="Not confirmed" role="status">
        <p>{unconfirmed.text}</p>
        {unconfirmed.profileHref !== null && (
          <p className="mt-2">
            <a
              href={unconfirmed.profileHref}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
            >
              The account&rsquo;s posts on X
              <ArrowUpRightIcon aria-hidden="true" className="size-3.5" />
              <span className="sr-only">(opens in a new tab)</span>
            </a>
          </p>
        )}
      </Notice>
    );
  }

  if (failure !== null) {
    return (
      <Notice tone="danger" title={failure.title} role="status">
        <p>{failure.advice}</p>
        {failure.reconnect && (
          <p className="mt-2">
            <Link
              href={projectPageHref(post.project_id, "accounts")}
              className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
            >
              Reconnect the X account
              <ArrowRightIcon aria-hidden="true" className="size-3.5" />
            </Link>
          </p>
        )}
      </Notice>
    );
  }

  return null;
}
