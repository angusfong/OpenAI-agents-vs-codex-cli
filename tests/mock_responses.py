"""Scripted mock of the OpenAI Responses API for key-less e2e testing.

Serves POST /v1/responses as an SSE stream. Scenarios are lists of stages;
each request consumes the stage matched by inspecting the request input
(which tool outputs are already present), so multi-step agent turns work
exactly like the real API: reasoning summaries stream first, then tool
calls, then a final assistant message. Events are serialized from the real
`openai` pydantic models, so anything the SDK would reject fails here too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response_reasoning_item import Summary
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)


@dataclass
class Stage:
    """One sampling response: reasoning summary + one output item."""

    reasoning: str
    # ("shell", [commands]) | ("apply_patch", operation_dict) | ("message", text)
    kind: str = "message"
    payload: Any = ""
    # substring of the request input JSON that must be present for this
    # stage to be considered "already done" (i.e. its tool output exists)
    done_marker: str | None = None


@dataclass
class MockResponsesServer:
    stages: list[Stage]
    host: str = "127.0.0.1"
    port: int = 0
    requests: list[dict] = field(default_factory=list)
    _seq: int = 0
    _counter: int = 0

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/responses", self.handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://{self.host}:{self.port}/v1"

    async def stop(self) -> None:
        await self._runner.cleanup()

    def _pick_stage(self, body: dict) -> Stage:
        input_json = json.dumps(body.get("input", []))
        for stage in self.stages:
            if stage.done_marker and stage.done_marker in input_json:
                continue
            return stage
        return self.stages[-1]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        stage = self._pick_stage(body)
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await resp.prepare(request)
        for event in self._events_for(stage, body):
            payload = event.model_dump_json(exclude_none=True)
            await resp.write(
                f"event: {event.type}\ndata: {payload}\n\n".encode("utf-8")
            )
        await resp.write_eof()
        return resp

    # -- event construction ------------------------------------------------

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _base_response(self, body: dict, rid: str) -> Response:
        return Response(
            id=rid,
            object="response",
            created_at=1_700_000_000,
            model=body.get("model", "mock-model"),
            output=[],
            parallel_tool_calls=True,
            tool_choice="auto",
            tools=[],
            status="in_progress",
        )

    def _events_for(self, stage: Stage, body: dict) -> list[Any]:
        self._counter += 1
        n = self._counter
        rid = f"resp_mock_{n}"
        events: list[Any] = []
        response = self._base_response(body, rid)
        events.append(
            ResponseCreatedEvent(
                type="response.created", response=response, sequence_number=self._next()
            )
        )

        # 1. Reasoning item with streamed summary deltas.
        reasoning_item = ResponseReasoningItem(
            id=f"rs_{n}", type="reasoning", summary=[]
        )
        events.append(
            ResponseOutputItemAddedEvent(
                type="response.output_item.added",
                item=reasoning_item,
                output_index=0,
                sequence_number=self._next(),
            )
        )
        for i in range(0, len(stage.reasoning), 12):
            events.append(
                ResponseReasoningSummaryTextDeltaEvent(
                    type="response.reasoning_summary_text.delta",
                    item_id=f"rs_{n}",
                    output_index=0,
                    summary_index=0,
                    delta=stage.reasoning[i : i + 12],
                    sequence_number=self._next(),
                )
            )
        reasoning_done = ResponseReasoningItem(
            id=f"rs_{n}",
            type="reasoning",
            summary=[Summary(type="summary_text", text=stage.reasoning)],
        )
        events.append(
            ResponseOutputItemDoneEvent(
                type="response.output_item.done",
                item=reasoning_done,
                output_index=0,
                sequence_number=self._next(),
            )
        )

        # 2. The stage's main output item.
        if stage.kind == "shell":
            item: dict[str, Any] = {
                "type": "shell_call",
                "id": f"sh_{n}",
                "call_id": f"call_sh_{n}",
                "status": "completed",
                "action": {
                    "type": "exec",
                    "commands": list(stage.payload),
                    "timeout_ms": 20000,
                },
            }
        elif stage.kind == "apply_patch":
            item = {
                "type": "apply_patch_call",
                "id": f"ap_{n}",
                "call_id": f"call_ap_{n}",
                "status": "completed",
                "operation": stage.payload,
            }
        else:
            text = str(stage.payload)
            message = ResponseOutputMessage(
                id=f"msg_{n}",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(type="output_text", text=text, annotations=[])
                ],
            )
            events.append(
                ResponseOutputItemAddedEvent(
                    type="response.output_item.added",
                    item=message.model_copy(update={"content": []}),
                    output_index=1,
                    sequence_number=self._next(),
                )
            )
            for i in range(0, len(text), 16):
                events.append(
                    ResponseTextDeltaEvent(
                        type="response.output_text.delta",
                        item_id=f"msg_{n}",
                        output_index=1,
                        content_index=0,
                        delta=text[i : i + 16],
                        logprobs=[],
                        sequence_number=self._next(),
                    )
                )
            item = message.model_dump()

        events.append(
            _RawOutputItemDone(item=item, sequence_number=self._next())
        )

        final = response.model_copy(
            update={
                "status": "completed",
                "output": [reasoning_done.model_dump(), item],
                "usage": ResponseUsage(
                    input_tokens=120 * n,
                    output_tokens=40 * n,
                    total_tokens=160 * n,
                    input_tokens_details=InputTokensDetails(
                        cached_tokens=0, cache_write_tokens=0
                    ),
                    output_tokens_details=OutputTokensDetails(reasoning_tokens=17),
                ),
            }
        )
        events.append(
            ResponseCompletedEvent(
                type="response.completed", response=final, sequence_number=self._next()
            )
        )
        return events


class _RawOutputItemDone:
    """output_item.done carrying a raw dict item (shell/apply_patch calls are
    not part of ResponseOutputItemDoneEvent's item union in every SDK rev,
    so serialize manually)."""

    type = "response.output_item.done"

    def __init__(self, item: dict, sequence_number: int):
        self.item = item
        self.sequence_number = sequence_number

    def model_dump_json(self, **kwargs) -> str:
        return json.dumps(
            {
                "type": self.type,
                "item": self.item,
                "output_index": 1,
                "sequence_number": self.sequence_number,
            }
        )
