"use client";

import { ArrowRightIcon, CheckIcon, XIcon } from "lucide-react";
import Link from "next/link";

import { StatusPill } from "@/components/common/status-pill";
import { Button } from "@/components/ui/button";
import type { Proposal } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import {
  actionLabel,
  effectLabel,
  estimateText,
  payloadText,
  proposalStatusDisplay,
  proposedPostText,
  timeLeftToDecide,
} from "@/lib/proposals/proposal-display";
import { proposalAnchor, writingHref } from "@/lib/projects/project-navigation";

type ProposalCardProps = {
  proposal: Proposal;
  now: number;
  /** Asks to approve (`true`) or reject (`false`); absent for a proposal that is decided. */
  onDecide?: (approved: boolean) => void;
};

/** One action an agent asked for: what it would do, exactly what it asks, and where the decision stands. */
export function ProposalCard({ proposal, now, onDecide }: ProposalCardProps) {
  const status = proposalStatusDisplay(proposal.status);
  const waiting = proposal.status === "pending";
  const label = actionLabel(proposal.action_name);
  const estimate = estimateText(proposal.estimate_usd);
  const post = proposedPostText(proposal);

  return (
    <article
      id={proposalAnchor(proposal.id)}
      aria-label={`Proposal ${shortId(proposal.id)}`}
      className={
        waiting
          ? "grid gap-4 rounded-md border border-dashed border-warning/60 bg-card p-5"
          : "grid gap-4 rounded-md border bg-card p-5"
      }
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <StatusPill tone={status.tone}>{status.label}</StatusPill>
        {label !== null && <p className="text-sm">{label}</p>}
        <p className={label === null ? "font-mono text-sm" : "font-mono text-xs text-muted-foreground"}>
          {proposal.action_name}
        </p>
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">{effectLabel(proposal.effect)}</p>
        {estimate !== null && <p className="font-mono text-sm tabular-nums">{estimate}</p>}
        <p className="ml-auto text-sm text-muted-foreground">Asked {formatDateTime(proposal.created_at)}</p>
      </div>

      {post !== null && (
        <div className="grid gap-2">
          <p className="text-2xs tracking-widest text-muted-foreground uppercase">The post, exactly as it would be sent</p>
          {/* Written by a model: text only, never HTML or Markdown. */}
          <blockquote className="max-w-prose border-l-2 border-warning/60 pl-4 text-base text-pretty break-words whitespace-pre-wrap">
            {post}
          </blockquote>
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
            <span>The writer sends nothing to X yet: an approved post is recorded as a dry run.</span>
            <Link
              href={writingHref(proposal.project_id, proposal.run_id)}
              className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
            >
              Its claims, checks and alternatives
              <ArrowRightIcon aria-hidden="true" className="size-3.5" />
            </Link>
          </p>
        </div>
      )}

      <div className="grid gap-1.5">
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">What the agent asks</p>
        {/* Written by an agent from a model's output: text only, never HTML or Markdown. */}
        <pre className="max-h-80 overflow-auto rounded-sm border bg-surface p-3 font-mono text-xs break-words whitespace-pre-wrap">
          {payloadText(proposal.payload)}
        </pre>
      </div>

      {waiting && onDecide !== undefined && (
        <div className="flex flex-wrap items-center gap-3">
          <p className="mr-auto text-sm text-pretty text-muted-foreground">
            Nothing runs until you decide. Decide {timeLeftToDecide(proposal.expires_at, now)}; after{" "}
            {formatDateTime(proposal.expires_at)} it expires and the action does not run.
          </p>
          <Button variant="outline" onClick={() => onDecide(false)}>
            <XIcon data-icon="inline-start" aria-hidden="true" />
            Reject
          </Button>
          <Button onClick={() => onDecide(true)}>
            <CheckIcon data-icon="inline-start" aria-hidden="true" />
            Approve
          </Button>
        </div>
      )}

      {proposal.status === "approved" && proposal.decided_at !== null && (
        <p className="text-sm text-muted-foreground">
          Approved {formatDateTime(proposal.decided_at)}. The run carries it out; its result is the run&rsquo;s.
        </p>
      )}
      {proposal.status === "rejected" && proposal.decided_at !== null && (
        <p className="text-sm text-muted-foreground">Rejected {formatDateTime(proposal.decided_at)}. It did not run.</p>
      )}
      {proposal.status === "expired" && (
        <p className="text-sm text-muted-foreground">
          Nobody decided before its deadline, or its run ended first. It did not run.
        </p>
      )}

      <p className="font-mono text-2xs text-muted-foreground">
        Proposal {proposal.id} · {proposal.run_kind} {shortId(proposal.run_id)}
      </p>
    </article>
  );
}
