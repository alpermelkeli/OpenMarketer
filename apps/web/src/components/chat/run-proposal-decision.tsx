"use client";

import { CheckIcon, XIcon } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { useDecideProposal } from "@/lib/api/proposals";
import type { ActionEffect } from "@/lib/api/types";
import type { RunProposal } from "@/lib/chat/events";
import { proposalText } from "@/lib/chat/research-progress";
import { proposalHref } from "@/lib/projects/project-navigation";
import { decideProblem, decisionLabels, effectLabel } from "@/lib/proposals/proposal-display";

// The effects a card names beside the action: what reaches beyond the run.
const NAMED_EFFECTS: readonly ActionEffect[] = ["publishes", "spends", "confirms"];

type RunProposalDecisionProps = {
  projectId: string;
  proposal: RunProposal;
  /** What exactly is decided, when the card can show it in place: a task's plan to confirm. */
  children?: ReactNode;
};

/**
 * The action a research or studio run, or a task, asked to run that the policy gate held for a
 * person, right in the run's card: what it would do, the buttons to decide it, and the way to the
 * proposal on the project's approvals screen for what exactly it asks. A task's plan to confirm is
 * named as one, and the plan itself is shown here when the card has it. The decision is the
 * person's own request to the API; the card then shows where it stands, as the chat's events say.
 * Nothing here decides anything itself.
 */
export function RunProposalDecision({ projectId, proposal, children }: RunProposalDecisionProps) {
  const decide = useDecideProposal(projectId);
  const waiting = proposal.status === "waiting";
  const labels = decisionLabels(proposal.effect);
  const effect = NAMED_EFFECTS.find((named) => named === proposal.effect) ?? null;

  return (
    <div className="grid gap-2 rounded-sm border border-dashed border-warning/60 bg-surface p-3">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-2xs tracking-widest text-muted-foreground uppercase">
          Approval
        </span>
        <span className="font-mono text-xs">{proposal.actionName}</span>
        {effect !== null && (
          <span className="text-2xs tracking-widest text-muted-foreground uppercase">
            {effectLabel(effect)}
          </span>
        )}
      </p>
      <p className="text-pretty">{proposalText(proposal)}</p>
      {children}
      {waiting && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Button
            variant="outline"
            size="sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ proposalId: proposal.id, approved: false })}
          >
            <XIcon data-icon="inline-start" aria-hidden="true" />
            {labels.reject}
          </Button>
          <Button
            size="sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ proposalId: proposal.id, approved: true })}
          >
            <CheckIcon data-icon="inline-start" aria-hidden="true" />
            {labels.approve}
          </Button>
          <Link
            href={proposalHref(projectId, proposal.id)}
            className="rounded-sm text-xs text-muted-foreground underline-offset-4 hover:underline"
          >
            What exactly it asks
          </Link>
        </div>
      )}
      {decide.error !== null && waiting && (
        <Notice tone="danger" role="status">
          <p className="text-pretty">{decideProblem(decide.error)}</p>
        </Notice>
      )}
    </div>
  );
}
