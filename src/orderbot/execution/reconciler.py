from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import sqlite3

from .journal import Journal, Position, lots_by_token, project_orders
from .lifecycle import LifecycleError, OrderState, observed_transitions
from .paper_venue import PaperVenue


@dataclass(frozen=True, slots=True)
class Reconciliation:
    ok: bool
    reason: str
    positions: tuple[Position, ...] = ()


class Reconciler:
    def __init__(self, journal: Journal, venue: PaperVenue, alert: Callable[[dict], None]):
        self.journal, self.venue, self.alert = journal, venue, alert

    def reconcile(self, at: datetime, *, recover_unsent: bool = False) -> Reconciliation:
        try:
            book = self.venue.snapshot()
            ledger = self.journal.snapshot()
            return self._reconcile_books(book, ledger, at, recover_unsent)
        except (LifecycleError, OSError, sqlite3.Error, KeyError, ValueError):
            return self._failed(at, "book_unavailable_or_inconsistent")

    def _reconcile_books(self, book, ledger, at, recover_unsent) -> Reconciliation:
        remote = {item.intent.tag: item for item in book.orders}
        known = {item.intent.tag: item for item in ledger.orders}
        if len(remote) != len(book.orders) or remote.keys() - known.keys():
            return self._failed(at, "unrecognized_remote_order")
        unsent, updates = [], []
        projected = dict(known)
        for tag, previous in known.items():
            current = remote.get(tag)
            if current is None:
                if previous.state is OrderState.INTENT:
                    if recover_unsent:
                        unsent.append(tag)
                    continue
                if previous.state is OrderState.CANCELLED and previous.reason == "unsent_intent":
                    continue
                return self._failed(at, "missing_remote_order")
            try:
                changes = observed_transitions(previous, current)
            except LifecycleError:
                return self._failed(at, "order_mismatch")
            if changes:
                updates.append(current)
                projected[tag] = current
        positions, _ = project_orders(projected.values())
        if lots_by_token(positions) != book.positions:
            return self._failed(at, "position_mismatch")
        self.journal.sync(updates, at, revision=ledger.revision, cancel_unsent=unsent)
        if any(item.state is OrderState.REJECTED and item.intent.at.date() == at.date() for item in remote.values()):
            self.journal.halt(at, "order_rejected")
        if any(position.opened_at.date() < at.date() for position in positions):
            self.journal.halt(at, "overnight_position")
            return self._failed(at, "overnight_position")
        return Reconciliation(True, "reconciled", positions)

    def _failed(self, at: datetime, reason: str) -> Reconciliation:
        self.journal.halt(at, reason, reconciliation_failure=True)
        self.alert({"event": "reconciliation_failed", "at": at.isoformat(), "reason": reason})
        return Reconciliation(False, reason)
