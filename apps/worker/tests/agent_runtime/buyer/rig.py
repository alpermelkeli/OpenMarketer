"""Where the buyer's tests start runs, decide proposals as the API would, and read the ledger."""

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client, WorkflowHandle

from buyer.definitions import KIND, BuyerRun
from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL, RunScope
from openmarketer_core.db.budget_ledger import LedgerReservation, reservation_of_action
from openmarketer_core.db.models import BudgetLedgerEntryRecord
from openmarketer_core.db.proposals import ActionProposal, decide_proposal, list_proposals
from openmarketer_core.db.session import transaction

PERSON = uuid.uuid4()


@dataclass
class Desk:
    """Where the tests start runs, decide proposals, and read what the ledger holds."""

    client: Client
    task_queue: str
    sessions: sessionmaker[Session]
    scope: RunScope

    async def start(self, run_id: uuid.UUID | None = None) -> tuple[BuyerRun, WorkflowHandle]:
        run = BuyerRun(
            workspace_id=self.scope.workspace_id,
            project_id=self.scope.project_id,
            run_id=run_id or uuid.uuid4(),
        )
        handle = await self.client.start_workflow(
            "Buyer", run, id=f"buyer-{run.run_id}", task_queue=self.task_queue
        )
        return run, handle

    def proposals(self) -> tuple[ActionProposal, ...]:
        with transaction(self.sessions) as session:
            return list_proposals(
                session,
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.project_id,
                limit=10,
            ).items

    async def pending_proposal(self, *, within: float = 20) -> ActionProposal:
        """The run's proposal, once the run has stored it."""

        async def stored() -> ActionProposal:
            while not (found := self.proposals()):
                await asyncio.sleep(0.1)
            return found[0]

        return await asyncio.wait_for(stored(), timeout=within)

    async def decide(
        self, handle: WorkflowHandle, proposal: ActionProposal, *, approved: bool
    ) -> None:
        """Record the decision, then tell the run: what the API does."""
        with transaction(self.sessions) as session:
            decide_proposal(
                session,
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.project_id,
                proposal_id=proposal.id,
                approved=approved,
                decided_by=PERSON,
            )
        await handle.signal(PROPOSAL_DECIDED_SIGNAL, proposal.id)

    def reservation(self, action_id: str) -> LedgerReservation | None:
        with transaction(self.sessions) as session:
            return reservation_of_action(
                session, workspace_id=self.scope.workspace_id, run_kind=KIND, action_id=action_id
            )

    def ledger_entries(self) -> int:
        """How many entries the workspace's ledger holds."""
        with transaction(self.sessions) as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(BudgetLedgerEntryRecord)
                    .where(BudgetLedgerEntryRecord.workspace_id == self.scope.workspace_id)
                )
                or 0
            )
