"""Fake claude_agent_sdk module for testing run_session without the real SDK."""

import types
from dataclasses import dataclass, field


@dataclass
class SystemMessage:
    subtype: str
    data: dict = field(default_factory=dict)


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class AssistantMessage:
    content: list = field(default_factory=list)


@dataclass
class ResultMessage:
    session_id: str = None
    num_turns: int = 0
    is_error: bool = False
    result: str = None


class ProcessError(Exception):
    pass


class Options:
    """Accepts every knob (new SDK)."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class StrictOptions:
    """Rejects max_buffer_size (old SDK) -> TypeError fallback path."""

    def __init__(self, cwd, permission_mode, model, max_turns):
        self.kwargs = dict(cwd=cwd, permission_mode=permission_mode,
                           model=model, max_turns=max_turns)


def make_sdk(messages=(), raise_exc=None, strict_options=False):
    """Build a fake claude_agent_sdk module.

    messages: messages the query() async generator yields (in order)
    raise_exc: exception raised after yielding all messages
    strict_options: use an Options class without the max_buffer_size knob
    """
    mod = types.ModuleType("claude_agent_sdk")
    mod.SystemMessage = SystemMessage
    mod.TextBlock = TextBlock
    mod.ToolUseBlock = ToolUseBlock
    mod.AssistantMessage = AssistantMessage
    mod.ResultMessage = ResultMessage
    mod.ProcessError = ProcessError
    mod.ClaudeAgentOptions = StrictOptions if strict_options else Options

    async def _agen():
        for m in messages:
            yield m
        if raise_exc is not None:
            raise raise_exc

    def query(prompt, options):
        return _agen()

    mod.query = query
    return mod
