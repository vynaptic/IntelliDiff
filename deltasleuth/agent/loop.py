"""One chat turn: the scoped question, the model's tool calls within the profile's budgets, then an
answer the validator accepts (after at most one repair) or an abstention with the reason.

The turn is persisted in one transaction, even when it fails: the question, each tool call with
its framed result (role ``tool``: later turns may cite it), the answer with its citations and every
``llm_calls`` row. Calls go out as ``production_tokenised`` data, and as ``production_clear_declared``
too when the run's spec declares clear values or a business-code key, which tool results show raw. So the
egress policy refuses a profile that does not list what a run carries, and an external host unless this
installation approves each class for external hosts.

The profile's budgets hold per call and per turn in the client, which lives for one question. The
``STANDING`` budgets hold over questions: before any model call the turn sums what the stored calls of the
run's chats, and of the asker's chats in the last 24 hours, were charged, and abstains once one is spent;
the client starts from those sums, so no call of the turn passes them either. The asker's
``compare_key`` calls count against one rate over all their sessions and turns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from deltasleuth.agent import prompt
from deltasleuth.agent.tools import COMPARE_LIMIT, Scope, ToolError, clear_declared, run_tool, tools_for
from deltasleuth.agent.validate import Evidence, check
from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.core.spec import SpecError
from deltasleuth.core.types import CompiledSpec
from deltasleuth.llm.client import LLMClient
from deltasleuth.llm.model import (
    BudgetExceeded,
    DataClass,
    LLMError,
    LLMRequest,
    Message,
    TextPart,
    ToolCallPart,
    ToolChoice,
)
from deltasleuth.llm.policy import EgressPolicy
from deltasleuth.llm.profiles import STANDING
from deltasleuth.security.crypto import Sealer
from deltasleuth.store import repo

# Tool results carry a run's masked data; the question goes as the analyst typed it.
DATA_CLASSES = frozenset({DataClass.PRODUCTION_TOKENISED})
MAX_OUTPUT_TOKENS = 2048  # unless the profile declares its max_output
INCOMPLETE = ("max_tokens", "content_filter", "refusal")  # stop reasons that leave nothing to check


class AIDisabled(RuntimeError):
    """No LLM profiles are configured."""


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[str, ...] = ()
    abstained: bool = False


def data_classes(spec: CompiledSpec) -> frozenset[DataClass]:
    """What a request about a run of ``spec`` carries: masked data, and with it the raw values its tool
    results show when the spec declares any clear or its key is a business code."""
    return DATA_CLASSES | {DataClass.PRODUCTION_CLEAR_DECLARED} if clear_declared(spec) else DATA_CLASSES


def open_client(ctx: Context) -> LLMClient:
    """A client of the configured chat profile, under this environment's egress policy and the
    profile's limits, which every question shares."""
    if ctx.llm is None:
        raise AIDisabled("AI disabled")
    settings = ctx.settings
    pid = settings.llm_profile
    egress = EgressPolicy(settings.llm_environment, settings.llm_external_data_classes)
    return LLMClient.from_profile(ctx.llm.get(pid), egress=egress, gate=ctx.llm.gate(pid))


async def answer(
    ctx: Context, session_id: str, question: str, who: Principal, about: Mapping[str, Any] | None = None
) -> Answer:
    """Answer ``question`` in the session for ``who``, the asker, whose roles bound the tools; persist
    the turn. LookupError for an unknown session. ``about``: the root cause a fixed question explains
    (``tools.cause_facts``), which the answer must cite."""
    async with open_client(ctx) as client:
        async with ctx.pool.connection() as conn:
            session = await repo.get_chat_session(conn, session_id)
            if session is None:
                raise LookupError(f"no chat session {session_id}")
            sealer = await ctx.sealer(conn, session["run_id"])
            run = await repo.get_run(conn, session["run_id"])
            carried = data_classes(await ctx.spec(conn, run["spec_id"]))
            client.prior = await repo.chat_tokens(conn, session["run_id"], who.actor)
        session["messages"] = [sealer.open_row(m) for m in session["messages"]]
        turn = _Turn(ctx, session, client, who, sealer, carried)
        if about is not None:  # its facts follow the question, framed like a tool result, and are evidence
            facts = prompt.frame("root_cause", data=about)
            question = f"{question}\n\n{facts}"
            turn.evidence.add(prompt.unframe(facts))
            turn.must = about["root_cause_id"]
        reply = Answer("No answer: the turn failed.", abstained=True)
        try:
            reply = _spent(client) or await turn.answer(question)
        finally:  # a failed turn keeps its question, tool calls and llm_calls rows
            await turn.save(question, reply)
    return reply


class _Turn:
    def __init__(
        self,
        ctx: Context,
        session: dict[str, Any],
        client: LLMClient,
        who: Principal,
        sealer: Sealer,
        carried: frozenset[DataClass] = DATA_CLASSES,
    ) -> None:
        """``session`` with its messages opened by the run's ``sealer``, which seals the turn's;
        ``carried``: the data classes of every request (``data_classes`` of the run's spec)."""
        self.ctx, self.client, self.session, self.sealer = ctx, client, session, sealer
        self.carried = carried
        self.meter = client.new_turn()
        self.calls: list[tuple[ToolCallPart, str]] = []
        earlier = session["messages"]
        tools = [m for m in earlier if m["role"] == "tool"]
        self.evidence = Evidence()
        for m in tools:
            self.evidence.add(prompt.unframe(m["content"]))
        self.must: str | None = None  # the root cause a fixed question asked about
        compares = ctx.llm.rate("compare_key", COMPARE_LIMIT) if ctx.llm is not None else None
        self.scope = Scope(session["run_id"], replace(who, via=f"chat:{session['session_id']}"), compares)
        said = [Message(m["role"], (TextPart(m["content"]),)) for m in earlier if m["role"] != "tool"]
        self.messages = [Message.system(prompt.SYSTEM), *said]

    async def answer(self, question: str) -> Answer:
        self.messages.append(
            Message.user(prompt.question(self.scope.run_id, self.session["key_masked"], question))
        )
        try:
            text = await self._converse()
            citations, problems = check(text, self.evidence, self.must)
            if problems:
                self.messages.append(Message.user(prompt.repair(problems)))
                text = await self._converse()
                # What is still wrong is stored and shown: none of it in the model's own words.
                citations, problems = check(text, self.evidence, self.must, shown=True)
        except BudgetExceeded as exc:
            # A standing budget's figures sum other people's questions: the asker is told its name only.
            said = prompt.spent(exc.budget) if exc.budget in STANDING else prompt.unavailable(exc)
            return Answer(said, abstained=True)
        except LLMError as exc:
            return Answer(prompt.unavailable(exc), abstained=True)
        if problems:
            return Answer(prompt.abstain(problems), abstained=True)
        return Answer(text, citations)

    async def _converse(self) -> str:
        """Call the model until it answers; once the tool-call budget is spent it must answer."""
        while True:
            spent = self.meter.tool_calls_remaining == 0
            response = await self.client.complete(self._request("none" if spent else "auto"), turn=self.meter)
            if response.stop_reason in INCOMPLETE:
                why = "the model gave no complete answer"
                if response.stop_reason == "max_tokens":  # a reasoning model spends its reply limit thinking
                    why += f" within {self._limit()} tokens; raise the profile's capabilities.max_output"
                raise LLMError(why, error_class=response.stop_reason)
            if spent or not response.tool_calls:
                self.messages.append(Message.assistant(response.text))
                return response.text
            # Parallel calls past the budget are refused, not run; the next call must answer.
            over = max(self.meter.tool_calls - self.meter.budgets.max_tool_calls, 0)
            self.meter.tool_calls -= over
            runnable = len(response.tool_calls) - over
            self.messages.append(response.message)
            for i, call in enumerate(response.tool_calls):
                self.messages.append(await self._run(call) if i < runnable else _refused(call))

    def _limit(self) -> int:
        return self.client.effective.max_output or MAX_OUTPUT_TOKENS

    def _request(self, tool_choice: ToolChoice) -> LLMRequest:
        return LLMRequest(
            tuple(self.messages),
            tools_for(self.scope.who),  # a viewer's model never sees compare_key
            tool_choice,
            max_output_tokens=self._limit(),
            data_classes=self.carried,
        )

    async def _run(self, call: ToolCallPart) -> Message:
        args = dict(call.arguments) if call.arguments_valid else None
        try:
            data = await run_tool(self.ctx, self.scope, call.name, args)
            content, is_error = prompt.frame(call.name, data=data), False
        except (ToolError, LookupError, ValueError, SpecError) as exc:  # refusals the model can read
            content, is_error = prompt.frame(call.name, error=str(exc)), True
        self.evidence.add(prompt.unframe(content))  # an error frame holds no data
        self.calls.append((call, content))
        return Message.tool_result(call.id, content, is_error=is_error)

    async def save(self, question: str, answer: Answer) -> None:
        sid, sealer = self.session["session_id"], self.sealer
        async with self.ctx.pool.connection() as conn, conn.transaction():
            await repo.append_chat_message(conn, sid, "user", _payload(question), sealer)
            for call, content in self.calls:
                made = {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                await repo.append_chat_message(conn, sid, "tool", _payload(content, [made]), sealer)
            said = _payload(answer.text)
            await repo.append_chat_message(conn, sid, "assistant", said, sealer, citations=answer.citations)
            for row in self.client.call_log:
                await repo.insert_llm_call(conn, sid, row)


def _spent(client: LLMClient) -> Answer | None:
    """The abstention of a turn whose ``STANDING`` budget the stored calls have spent, before any call."""
    budgets = client.profile.budgets
    for name in STANDING:
        if (limit := getattr(budgets, name)) is not None and client.prior.get(name, 0) >= limit:
            return Answer(prompt.spent(name), abstained=True)
    return None


def _payload(content: str, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What a stored message seals."""
    return {"content": content, "tool_calls": tool_calls or []}


def _refused(call: ToolCallPart) -> Message:
    return Message.tool_result(
        call.id, prompt.frame(call.name, error="the tool-call budget is spent"), is_error=True
    )
