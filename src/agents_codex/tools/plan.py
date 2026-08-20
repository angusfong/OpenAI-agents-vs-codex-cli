"""update_plan tool, mirroring Codex CLI's plan tool: the model maintains a
step list with statuses; the UI renders it as a live checklist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from agents import RunContextWrapper, function_tool


class PlanStep(BaseModel):
    step: str
    status: Literal["pending", "in_progress", "completed"]


@dataclass
class PlanState:
    explanation: str | None = None
    steps: list[PlanStep] = field(default_factory=list)


@function_tool(name_override="update_plan")
async def update_plan(
    ctx: RunContextWrapper,
    steps: list[PlanStep],
    explanation: str | None = None,
) -> str:
    """Update the shared task plan. Provide the full list of steps each time;
    keep at most one step in_progress.

    Args:
        steps: The complete, ordered plan with a status per step.
        explanation: Optional note about why the plan changed.
    """
    state: PlanState = getattr(ctx.context, "plan", None) or PlanState()
    state.steps = steps
    state.explanation = explanation
    if ctx.context is not None:
        ctx.context.plan = state
    done = sum(1 for s in steps if s.status == "completed")
    return f"Plan updated: {done}/{len(steps)} steps completed."
