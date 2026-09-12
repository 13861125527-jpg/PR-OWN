"""Run 级共享 finalize 预留池（24 §3.3 / DP-24）。"""

from __future__ import annotations

import asyncio

from reposage.domain.models import BudgetReservation, GlobalBudget


class FinalizeBudgetCoordinator:
    """一个 AgenticReviewer run 内所有文件共享一份 finalize 池。

    不复制 GlobalBudget 账本：探索请求通过 ``protect_*`` 为剩余 finalize 留出门槛；
    grace 请求从本池竞争后再走 GlobalBudget.reserve。
    """

    def __init__(self, budget: GlobalBudget) -> None:
        self.budget = budget
        ratio = budget.reserved_finalize_ratio
        self.pool_tokens = int(budget.max_total_tokens * ratio)
        self.pool_cost = budget.max_cost_usd * ratio
        self._lock = asyncio.Lock()
        self._grace_reserved_tokens = 0
        self._grace_used_tokens = 0
        self._grace_reserved_cost = 0.0
        self._grace_used_cost = 0.0
        self._grace_open: dict[int, tuple[int, float]] = {}

    def remaining_finalize_tokens(self) -> int:
        return max(
            0, self.pool_tokens - self._grace_used_tokens - self._grace_reserved_tokens
        )

    def remaining_finalize_cost(self) -> float:
        return max(0.0, self.pool_cost - self._grace_used_cost - self._grace_reserved_cost)

    async def reserve(
        self,
        *,
        mode: str,
        input_tokens: int,
        max_output_tokens: int,
        est_cost_usd: float | None,
    ) -> BudgetReservation | None:
        need_tokens = input_tokens + max_output_tokens
        async with self._lock:
            if mode == "grace":
                if need_tokens > self.remaining_finalize_tokens():
                    return None
                if est_cost_usd is not None and est_cost_usd > self.remaining_finalize_cost():
                    return None
                reservation = await self.budget.reserve(
                    input_tokens=input_tokens,
                    max_output_tokens=max_output_tokens,
                    est_cost_usd=est_cost_usd,
                )
                if reservation is None:
                    return None
                cost = est_cost_usd or 0.0
                self._grace_reserved_tokens += need_tokens
                self._grace_reserved_cost += cost
                self._grace_open[id(reservation)] = (need_tokens, cost)
                return reservation
            protect_tokens = self.remaining_finalize_tokens()
            protect_cost = self.remaining_finalize_cost() if est_cost_usd is not None else 0.0
            return await self.budget.reserve(
                input_tokens=input_tokens,
                max_output_tokens=max_output_tokens,
                est_cost_usd=est_cost_usd,
                protect_tokens=protect_tokens,
                protect_cost=protect_cost,
            )

    async def settle(
        self,
        reservation: BudgetReservation,
        *,
        mode: str,
        actual_input: int,
        actual_output: int,
        actual_cost: float,
    ) -> None:
        async with self._lock:
            await self.budget.settle(
                reservation,
                actual_input=actual_input,
                actual_output=actual_output,
                actual_cost=actual_cost,
            )
            if mode != "grace":
                return
            held = self._grace_open.pop(id(reservation), None)
            if held is None:
                return
            held_tokens, held_cost = held
            self._grace_reserved_tokens = max(0, self._grace_reserved_tokens - held_tokens)
            self._grace_reserved_cost = max(0.0, self._grace_reserved_cost - held_cost)
            self._grace_used_tokens += reservation.settled_input + reservation.settled_output
            self._grace_used_cost += reservation.settled_cost
