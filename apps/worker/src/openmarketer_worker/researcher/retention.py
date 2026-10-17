"""Removing the pages a research run fetched once they have expired, while the worker serves.

The bytes of a fetched page are working material: a report keeps the quotes it
relied on, and the pages are removed after ``RESEARCH_PAGE_RETENTION_DAYS``.
This module keeps the schedule; which pages may go is core's rule
(``db/research_pages.expired_pages``). Every round takes a bounded batch of the
oldest expired pages, removes each one's object first and then the rows whose
objects are gone, so an object that cannot be removed is tried again next round
while its row still names it. A round that fails is logged and tried again: it
never stops the worker or a run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.research_pages import delete_pages, expired_pages
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.research.page_store import PageStore, PageStoreUnavailable
from openmarketer_worker import PeriodicTask

logger = logging.getLogger(__name__)

# A round is one statement that usually finds nothing; a day bounds how long an expired
# page's bytes stay without a query every few seconds.
RETENTION_EVERY_SECONDS: float = 24 * 60 * 60
EXPIRY_BATCH = 500


def page_retention_task(
    sessions: sessionmaker[Session],
    store: PageStore,
    *,
    retention: timedelta,
    every_seconds: float = RETENTION_EVERY_SECONDS,
) -> PeriodicTask:
    """The schedule that removes expired pages, as a task the process can start."""

    async def keep_removing() -> None:
        while True:
            await asyncio.to_thread(remove_expired_pages, sessions, store, retention=retention)
            await asyncio.sleep(every_seconds)

    return keep_removing


def remove_expired_pages(
    sessions: sessionmaker[Session], store: PageStore, *, retention: timedelta
) -> None:
    """One round: the oldest expired pages' objects, then the rows that are safe to remove."""
    try:
        with transaction(sessions) as session:
            pages = expired_pages(session, older_than=retention, limit=EXPIRY_BATCH)
    except DatabaseError as e:
        logger.warning("expired pages were not read, trying again later: %s", e)
        return
    gone = []
    for page in pages:
        try:
            store.remove(page.storage_key)
        except PageStoreUnavailable as e:
            logger.warning("a fetched page was not removed, trying again later: %s", e)
            continue
        gone.append(page.id)
    if not gone:
        return
    try:
        with transaction(sessions) as session:
            removed = delete_pages(session, tuple(gone))
    except DatabaseError as e:
        logger.warning("the rows of expired pages were not removed, trying again later: %s", e)
        return
    logger.info("removed %d expired pages of research runs", removed)
