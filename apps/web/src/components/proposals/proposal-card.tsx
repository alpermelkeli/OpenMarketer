"use client";

import { CheckIcon, XIcon } from "lucide-react";

import { StatusPill } from "@/components/common/status-pill";
import { Button } from "@/components/ui/button";
import type { Proposal } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import {
  effectLabel,
  payloadText,
  proposalStatusDisplay,
  timeLeftToDecide,
} from "@/lib/proposals/proposal-display";

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

  return (
    <article
      aria-label={`Proposal ${shortId(proposal.id)}`}
      className={
        waiting
          ? "grid gap-4 rounded-md border border-dashed border-warning/60 bg-card p-5"
          : "grid gap-4 rounded-md border bg-card p-5"
      }
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <StatusPill tone={status.tone}>{status.label}</StatusPill>
        <p className="font-mono text-sm">{proposal.action_name}</p>
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">{effectLabel(proposal.effect)}</p>
        <p className="ml-auto text-sm text-muted-foreground">Asked {formatDateTime(proposal.created_at)}</p>
      </div>

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
