"""ExecutionBroker protocol and mandatory Capital-backed PaperBroker."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from .store import (
    CAPITAL_RESET_POSITION_CLEARED,
    ExecutionStore,
    ORPHANED_FILL_ROLLED_BACK,
)
from .types import OrderAction, OrderIntent, OrderState

logger = logging.getLogger(__name__)


@runtime_checkable
class ExecutionBroker(Protocol):
    def submit(self, intent: OrderIntent) -> dict[str, Any]: ...

    def cancel(self, order_intent_id: str, *, reason: str = "operator_cancel") -> dict[str, Any]: ...

    def reconcile(self) -> dict[str, Any]: ...

    def positions(self) -> list[dict[str, Any]]: ...


class PaperBroker:
    """Paper order lifecycle over the existing authoritative CapitalManager."""

    def __init__(
        self,
        *,
        capital_manager: Any,
        store: ExecutionStore,
        new_risk_authorizer: Optional[Callable[[], tuple[bool, str]]] = None,
    ) -> None:
        self.capital_manager = capital_manager
        self.store = store
        self.new_risk_authorizer = new_risk_authorizer or (lambda: (False, "paper entry policy unavailable"))

    @staticmethod
    def _positive(value: Any, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a positive finite number")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a positive finite number") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be a positive finite number")
        return number

    def _validate(self, intent: OrderIntent) -> None:
        if intent.action.opens_new_risk:
            self._positive(intent.size_usd, "size_usd")
            self._positive(intent.stop_loss_pct, "stop_loss_pct")
            self._positive(intent.take_profit_pct, "take_profit_pct")
            if not intent.signal_id:
                raise ValueError("signal_id is required for a paper entry")
            if intent.leverage < 1 or intent.leverage > 5:
                raise ValueError("leverage must be in [1,5]")
        else:
            if not intent.position_id:
                raise ValueError("position_id is required for reduce/close")
            position = self.capital_manager.get_position(intent.position_id)
            if not isinstance(position, Mapping):
                raise ValueError("position not found")
            if str(position.get("status") or "").upper() != "OPEN":
                raise ValueError("position is not open")
            if str(position.get("ticker") or "").upper() != intent.ticker:
                raise ValueError("intent ticker does not match position")
            if intent.action is OrderAction.REDUCE:
                fraction = self._positive(intent.reduce_fraction, "reduce_fraction")
                if fraction >= 1.0:
                    raise ValueError("reduce_fraction must be below 1")

    def submit(self, intent: OrderIntent) -> dict[str, Any]:
        existing = self.store.get_by_idempotency_key(intent.idempotency_key)
        if existing is not None:
            existing["idempotent_replay"] = True
            return existing

        reconciliation = self.reconcile()
        order, created = self.store.create_order(intent)
        if not created:
            order["idempotent_replay"] = True
            return order

        try:
            self._validate(intent)
            order = self.store.transition(intent.order_intent_id, OrderState.VALIDATED)
            if intent.action.opens_new_risk:
                allowed, reason = self.new_risk_authorizer()
                if not allowed:
                    return self.store.transition(
                        intent.order_intent_id,
                        OrderState.REJECTED,
                        detail={"policy": reason, "reconciliation": reconciliation},
                        error=str(reason or "new paper risk is blocked"),
                    )

            order = self.store.transition(intent.order_intent_id, OrderState.SUBMITTED)
            order = self.store.transition(intent.order_intent_id, OrderState.ACKNOWLEDGED)
            result = self._execute(intent)
            self._notify_telegram_outcome(intent, result)
            if intent.action is OrderAction.REDUCE:
                order = self.store.transition(
                    intent.order_intent_id,
                    OrderState.PARTIALLY_FILLED,
                    detail={"filled_fraction": intent.reduce_fraction},
                    result=result,
                )
            return self.store.transition(
                intent.order_intent_id,
                OrderState.FILLED,
                detail={"paper_fill": True},
                result=result,
            )
        except (KeyError, ValueError) as exc:
            current = self.store.get_order(intent.order_intent_id) or order
            if current.get("state") in {
                OrderState.PROPOSED.value,
                OrderState.VALIDATED.value,
                OrderState.SUBMITTED.value,
                OrderState.ACKNOWLEDGED.value,
            }:
                return self.store.transition(
                    intent.order_intent_id,
                    OrderState.REJECTED,
                    detail={"validation_or_execution_error": str(exc)},
                    error=str(exc),
                )
            return current
        except Exception as exc:
            current = self.store.get_order(intent.order_intent_id) or order
            if current.get("state") not in {
                OrderState.FILLED.value,
                OrderState.CANCELLED.value,
                OrderState.REJECTED.value,
                OrderState.FAILED.value,
            }:
                return self.store.transition(
                    intent.order_intent_id,
                    OrderState.FAILED,
                    detail={"exception_type": type(exc).__name__},
                    error=str(exc),
                )
            return current

    def _execution_context(self, intent: OrderIntent) -> dict[str, Any]:
        metadata = dict(intent.metadata)
        context = dict(metadata.get("decision_context") or {})
        execution = dict(context.get("execution") or {})
        execution.update(
            {
                "broker": "PaperBroker",
                "decision_id": intent.decision_id,
                "order_intent_id": intent.order_intent_id,
                "idempotency_key": intent.idempotency_key,
            }
        )
        context["execution"] = execution
        return context

    def _execute(self, intent: OrderIntent) -> dict[str, Any]:
        metadata = dict(intent.metadata)
        if intent.action.opens_new_risk:
            position = self.capital_manager.open_position(
                ticker=intent.ticker,
                direction="LONG" if intent.action is OrderAction.OPEN_LONG else "SHORT",
                size_usd=float(intent.size_usd or 0.0),
                stop_loss_pct=float(intent.stop_loss_pct or 0.0),
                take_profit_pct=float(intent.take_profit_pct or 0.0),
                signal_id=intent.signal_id,
                note=str(metadata.get("note") or "paper"),
                leverage=intent.leverage,
                decision_context=self._execution_context(intent),
                strategy_type=str(metadata.get("strategy_type") or ""),
                entry_reason=str(metadata.get("entry_reason") or intent.reason),
                expected_entry_price=metadata.get("expected_entry_price"),
                max_entry_deviation_pct=metadata.get("max_entry_deviation_pct"),
            )
        elif intent.action is OrderAction.REDUCE:
            position = self.capital_manager.reduce_position(
                position_id=intent.position_id,
                fraction=float(intent.reduce_fraction or 0.0),
                exit_price=float(metadata.get("exit_price") or 0.0),
                reason=intent.reason or "risk_reduction",
                exit_execution_data=metadata.get("exit_execution_data"),
                trigger_price=metadata.get("trigger_price"),
            )
        else:
            position = self.capital_manager.close_position(
                position_id=intent.position_id,
                exit_price=float(metadata.get("exit_price") or 0.0),
                reason=intent.reason or "paper_close",
                enforce_exit_reason=bool(metadata.get("enforce_exit_reason", False)),
                exit_execution_data=metadata.get("exit_execution_data"),
                trigger_price=metadata.get("trigger_price"),
            )
        return {
            "position_id": str(position.get("position_id") or ""),
            "position_status": str(position.get("status") or ""),
            "position": dict(position),
        }

    def _notify_telegram_outcome(self, intent: OrderIntent, result: dict[str, Any]) -> None:
        """Owner-facing Telegram delivery for paper fills (never blocks/raises)."""
        try:
            from core.telegram_service import notify_telegram

            position = dict(result.get("position") or {})
            direction = str(position.get("direction") or ("LONG" if intent.action is OrderAction.OPEN_LONG else "SHORT")).upper()
            ticker = intent.ticker

            def _num(key: str, *fallbacks: str) -> Optional[float]:
                for key_ in (key, *fallbacks):
                    value = position.get(key_)
                    if value is not None:
                        try:
                            return float(value)
                        except (TypeError, ValueError):
                            continue
                return None

            def _fmt(value: Optional[float]) -> str:
                return f"${value:,.2f}" if value is not None else "n/a"

            if intent.action.opens_new_risk:
                size = _num("size_usd") or (float(intent.size_usd) if intent.size_usd else None)
                entry = _num("entry_price", "avg_entry_price", "open_price")
                notify_telegram(
                    "signals",
                    f"📈 ENTRY {direction} {ticker} (paper)",
                    [
                        f"Size: {_fmt(size)}",
                        f"Entry: {_fmt(entry)}",
                        f"SL {_fmt(_num('stop_loss_pct'))} · TP {_fmt(_num('take_profit_pct'))}"
                        if (_num("stop_loss_pct") or _num("take_profit_pct"))
                        else f"Signal: {intent.signal_id or 'n/a'}",
                        str(intent.reason or "").strip()[:180],
                    ],
                )
            else:
                pnl = _num("net_pnl", "realized_pnl", "pnl")
                exit_price = _num("exit_price", "avg_exit_price")
                emoji = "✅" if (pnl or 0) >= 0 else "❌"
                notify_telegram(
                    "capital",
                    f"{emoji} EXIT {direction} {ticker} (paper)",
                    [
                        f"P&L: {_fmt(pnl)}",
                        f"Exit: {_fmt(exit_price)}",
                        f"Reason: {str(intent.reason or 'paper_close')[:120]}",
                    ],
                )
        except Exception:
            logger.debug("Telegram outcome notify skipped", exc_info=True)

    def cancel(self, order_intent_id: str, *, reason: str = "operator_cancel") -> dict[str, Any]:
        return self.store.transition(
            order_intent_id,
            OrderState.CANCELLED,
            detail={"reason": str(reason or "operator_cancel")},
        )

    def positions(self) -> list[dict[str, Any]]:
        values = self.capital_manager.get_positions() or []
        return [dict(value) for value in values if isinstance(value, Mapping)]

    def rollback_orphaned_fill(self, order_intent_id: str, *, reason: str) -> dict[str, Any]:
        """Resolve a FILLED open whose Capital mutation never persisted.

        The original FILLED record remains immutable.  A same-state audit event
        documents the compensating rollback, after verifying there is no
        authoritative Capital position to close or preserve.
        """
        order = self.store.get_order(order_intent_id)
        if not isinstance(order, Mapping):
            raise KeyError("order intent not found")
        if str(order.get("state") or "") != OrderState.FILLED.value:
            raise ValueError("order is not filled")
        if not str(order.get("action") or "").startswith("OPEN_"):
            raise ValueError("order is not an open")
        position_id = str((order.get("result") or {}).get("position_id") or "")
        if not position_id:
            raise ValueError("filled order has no position id")
        if any(
            str(position.get("position_id") or "") == position_id
            for position in self.positions()
        ):
            raise ValueError("Capital position still exists; rollback is not permitted")
        # A recorded cash debit without a position is a cash discrepancy, not an
        # orphaned fill: an audit-only rollback would hide money that left Capital.
        try:
            history = self.capital_manager.get_history() or []
        except Exception:
            history = []
        for transaction in history:
            if not isinstance(transaction, Mapping):
                continue
            metadata = transaction.get("metadata") if isinstance(transaction.get("metadata"), Mapping) else {}
            if str(metadata.get("position_id") or "") == position_id:
                raise ValueError(
                    "Capital history records a transaction for this position; "
                    "rollback is not permitted because Capital cash was affected"
                )
        resolution = self.store.record_reconciliation_resolution(
            str(order.get("order_intent_id") or ""),
            resolution=ORPHANED_FILL_ROLLED_BACK,
            reason=reason,
        )
        return {"resolution": resolution, "reconciliation": self.reconcile()}

    @staticmethod
    def _timestamp(value: Any) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _latest_capital_reset(self) -> tuple[Optional[datetime], set[str], str]:
        """Return the latest reset that explicitly or historically cleared positions."""
        try:
            history = self.capital_manager.get_history() or []
        except Exception:
            return None, set(), ""
        for transaction in reversed(history):
            if not isinstance(transaction, Mapping):
                continue
            if str(transaction.get("type") or "").upper() != "RESET":
                continue
            metadata = transaction.get("metadata") if isinstance(transaction.get("metadata"), Mapping) else {}
            # Legacy RESET records predate this metadata and used clear_positions=True.
            if metadata.get("clear_positions") is False:
                continue
            reset_at = self._timestamp(transaction.get("timestamp"))
            if reset_at is None:
                continue
            removed_ids = {
                str(value or "").strip()
                for value in (metadata.get("removed_position_ids") or [])
                if str(value or "").strip()
            }
            return reset_at, removed_ids, str(transaction.get("timestamp") or "")
        return None, set(), ""

    def _resolve_capital_reset_fill(
        self,
        order: Mapping[str, Any],
        *,
        position_id: str,
        reset_at: Optional[datetime],
        removed_position_ids: set[str],
        reset_timestamp: str,
    ) -> bool:
        """Audit a missing filled position that an explicit Capital reset cleared."""
        if reset_at is None or not str(order.get("action") or "").startswith("OPEN_"):
            return False
        created_at = self._timestamp(order.get("created_at"))
        if created_at is None or created_at > reset_at:
            return False
        if removed_position_ids and position_id not in removed_position_ids:
            return False
        self.store.record_reconciliation_resolution(
            str(order.get("order_intent_id") or ""),
            resolution=CAPITAL_RESET_POSITION_CLEARED,
            reason=f"Capital reset at {reset_timestamp} administratively cleared the filled paper position",
        )
        return True

    def reconcile(self) -> dict[str, Any]:
        conflicts: list[dict[str, Any]] = []
        try:
            positions = self.positions()
            by_id = {str(item.get("position_id") or ""): item for item in positions}
            orders = self.store.list_orders(limit=1000)
            by_order_id = {str(item.get("order_intent_id") or ""): item for item in orders}
            resolved_missing_positions = self.store.reconciliation_resolutions()
            reset_at, reset_position_ids, reset_timestamp = self._latest_capital_reset()
            filled_closes = {
                str((order.get("result") or {}).get("position_id") or "")
                for order in orders
                if str(order.get("state") or "") == OrderState.FILLED.value
                and str(order.get("action") or "") == OrderAction.CLOSE.value
            }

            for order in orders:
                state = str(order.get("state") or "")
                action = str(order.get("action") or "")
                result = order.get("result") if isinstance(order.get("result"), Mapping) else {}
                position_id = str(result.get("position_id") or "")
                intent = order.get("intent") if isinstance(order.get("intent"), Mapping) else {}

                if state in {OrderState.SUBMITTED.value, OrderState.ACKNOWLEDGED.value}:
                    recovered = None
                    if action.startswith("OPEN_"):
                        signal_id = str(intent.get("signal_id") or "")
                        recovered = next(
                            (
                                position
                                for position in positions
                                if signal_id
                                and signal_id
                                in {
                                    str(position.get("signal_id") or ""),
                                    str(position.get("source_signal_id") or ""),
                                    *{
                                        str(value or "")
                                        for value in (position.get("source_signal_ids") or [])
                                    },
                                }
                            ),
                            None,
                        )
                    else:
                        pending_position_id = str(intent.get("position_id") or "")
                        candidate = by_id.get(pending_position_id)
                        if action == OrderAction.CLOSE.value:
                            if candidate is not None and str(candidate.get("status") or "").upper() == "CLOSED":
                                recovered = candidate
                        elif action == OrderAction.REDUCE.value and candidate is not None:
                            metadata = intent.get("metadata") if isinstance(intent.get("metadata"), Mapping) else {}
                            before = metadata.get("position_before") if isinstance(metadata.get("position_before"), Mapping) else {}
                            try:
                                before_size = float(before.get("usd_size") or 0.0)
                                current_size = float(candidate.get("usd_size") or 0.0)
                            except (TypeError, ValueError):
                                before_size = current_size = 0.0
                            if before_size > 0 and 0 <= current_size < before_size:
                                recovered = candidate
                    if recovered is not None:
                        if action == OrderAction.REDUCE.value:
                            self.store.transition(
                                str(order["order_intent_id"]),
                                OrderState.PARTIALLY_FILLED,
                                detail={"recovered_by_reconciliation": True},
                                result={
                                    "position_id": str(recovered.get("position_id") or ""),
                                    "position_status": str(recovered.get("status") or ""),
                                    "position": dict(recovered),
                                },
                            )
                        self.store.transition(
                            str(order["order_intent_id"]),
                            OrderState.FILLED,
                            detail={"recovered_by_reconciliation": True},
                            result={
                                "position_id": str(recovered.get("position_id") or ""),
                                "position_status": str(recovered.get("status") or ""),
                                "position": dict(recovered),
                            },
                        )
                        continue
                    conflicts.append(
                        {
                            "code": "inflight_order_unresolved",
                            "order_intent_id": order.get("order_intent_id"),
                            "action": action,
                        }
                    )
                    continue

                if state != OrderState.FILLED.value:
                    continue
                position = by_id.get(position_id)
                if not position_id:
                    conflicts.append(
                        {
                            "code": "filled_order_position_missing",
                            "order_intent_id": order.get("order_intent_id"),
                            "position_id": position_id,
                        }
                    )
                    continue
                if position is None:
                    if action == OrderAction.CLOSE.value or position_id in filled_closes:
                        continue
                    if (
                        action.startswith("OPEN_")
                        and str(order.get("order_intent_id") or "") in resolved_missing_positions
                    ):
                        continue
                    if self._resolve_capital_reset_fill(
                        order,
                        position_id=position_id,
                        reset_at=reset_at,
                        removed_position_ids=reset_position_ids,
                        reset_timestamp=reset_timestamp,
                    ):
                        continue
                    conflicts.append(
                        {
                            "code": "filled_order_position_missing",
                            "order_intent_id": order.get("order_intent_id"),
                            "position_id": position_id,
                        }
                    )
                    continue
                if action == OrderAction.CLOSE.value and str(position.get("status") or "").upper() != "CLOSED":
                    conflicts.append(
                        {
                            "code": "filled_close_position_still_open",
                            "order_intent_id": order.get("order_intent_id"),
                            "position_id": position_id,
                        }
                    )

            for position in positions:
                context = position.get("decision_context") if isinstance(position.get("decision_context"), Mapping) else {}
                execution = context.get("execution") if isinstance(context.get("execution"), Mapping) else {}
                order_id = str(execution.get("order_intent_id") or "")
                if order_id and order_id not in by_order_id:
                    # The scan above is bounded to the newest 1000 orders, while
                    # Capital retains closed positions far longer. An order that
                    # merely aged out of that window is not missing: confirm
                    # against the ledger itself before raising a conflict.
                    if self.store.get_order(order_id) is not None:
                        continue
                    conflicts.append(
                        {
                            "code": "capital_position_order_missing",
                            "position_id": position.get("position_id"),
                            "order_intent_id": order_id,
                        }
                    )
        except Exception as exc:
            conflicts.append({"code": "reconciliation_error", "error": str(exc)})

        status = "EXECUTION_STATE_CONFLICT" if conflicts else "HEALTHY"
        result = self.store.set_reconciliation(status, conflicts)
        details = self.describe_conflicts(result.get("conflicts") or [])
        result["conflict_details"] = details
        result["summary"] = self.summarize_conflicts(details)
        return result

    def describe_conflicts(self, conflicts: Any) -> list[dict[str, Any]]:
        """Explain each stored conflict: which records disagree and how to resolve it.

        Read-only. The persisted ``conflicts`` list keeps its compact shape; this
        adds the owner-facing explanation on top, so a conflict latched by an
        earlier process is explained the same way.
        """
        described: list[dict[str, Any]] = []
        for raw in conflicts or []:
            if not isinstance(raw, Mapping):
                continue
            code = str(raw.get("code") or "")
            order_id = str(raw.get("order_intent_id") or "")
            position_id = str(raw.get("position_id") or "")
            order: Mapping[str, Any] = {}
            if order_id:
                try:
                    order = self.store.get_order(order_id) or {}
                except Exception:
                    order = {}
            ticker = str(order.get("ticker") or "")
            action = str(order.get("action") or raw.get("action") or "")
            created_at = str(order.get("created_at") or "")
            label = " ".join(part for part in (action, ticker) if part) or "order"
            when = f" on {created_at[:19].replace('T', ' ')} UTC" if created_at else ""
            repair_action = ""
            if code == "filled_order_position_missing":
                if position_id:
                    message = (
                        f"Order ledger records {label} ({order_id}) as FILLED{when} with Capital "
                        f"position {position_id}, but Capital has no such position (open or closed) "
                        "and no CLOSE order exists for it."
                    )
                else:
                    message = (
                        f"Order ledger records {label} ({order_id}) as FILLED{when} "
                        "without a Capital position id."
                    )
                if action.startswith("OPEN_") and position_id:
                    resolution = (
                        "Check the Capital transaction history for an OPEN_POSITION debit of this fill. "
                        "If there is none, the fill never became durable in Capital: record an orphaned-fill "
                        "rollback for this order (an audit event only; Capital cash and positions are not changed)."
                    )
                    repair_action = "ROLLBACK_ORPHANED_FILL"
                else:
                    resolution = "Inspect the order and the Capital position history manually; no automatic repair applies."
            elif code == "filled_close_position_still_open":
                message = (
                    f"Order ledger records {label} ({order_id}) as FILLED{when}, "
                    f"but Capital position {position_id} is still OPEN."
                )
                resolution = (
                    "Close the position again from the Capital tab during market hours, then re-run reconciliation."
                )
            elif code == "inflight_order_unresolved":
                message = (
                    f"{label} ({order_id}) was submitted{when} but never reached a final state, "
                    "and no matching Capital change was found."
                )
                resolution = (
                    "Verify in the Capital tab whether the position change happened, "
                    "then cancel the order or re-run reconciliation."
                )
            elif code == "capital_position_order_missing":
                message = (
                    f"Capital position {position_id} refers to order {order_id}, "
                    "which does not exist in the order ledger."
                )
                resolution = (
                    "The order ledger (execution.db) was replaced or is incomplete; restore it from a backup."
                )
            elif code == "reconciliation_error":
                message = f"Reconciliation could not run: {raw.get('error') or 'unknown error'}"
                resolution = "Fix the reported error and re-run reconciliation."
            else:
                message = f"Unrecognised reconciliation conflict {code or 'UNKNOWN'}."
                resolution = "Inspect the paper order ledger and the Capital state manually."
            described.append(
                {
                    "code": code,
                    "order_intent_id": order_id,
                    "position_id": position_id,
                    "ticker": ticker,
                    "action": action,
                    "order_created_at": created_at,
                    "message": message,
                    "resolution": resolution,
                    "repair_action": repair_action,
                }
            )
        return described

    @staticmethod
    def summarize_conflicts(details: Any) -> str:
        rows = [item for item in (details or []) if isinstance(item, Mapping)]
        if not rows:
            return ""
        first = str(rows[0].get("message") or "")
        extra = len(rows) - 1
        return first if extra <= 0 else f"{first} (+{extra} more conflict{'s' if extra != 1 else ''})"


__all__ = ["ExecutionBroker", "PaperBroker"]
