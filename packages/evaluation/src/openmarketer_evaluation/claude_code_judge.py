"""A judge that answers through Claude Code's headless mode, for the evaluation only.

The judge is the paid part of a benchmark. This lets a maintainer who has a
Claude Code subscription put the judge's questions to ``claude -p`` instead of
spending model-provider credit. It is a ``ChatModel``, so the questions, the
strict parsing, the scores and the reports are the same on both routes.

This is a deliberate exception to two rules of the project, and it stays in
this package: model calls of the product go through a role in
``config/models.yaml`` and ``ModelRouter``, and this one does not. Nothing of
the product may import it. It names no model: one is asked for only when the
caller passes it, otherwise Claude Code uses its own default.

Claude Code is an agent that can read files and run commands, and the judge is
shown untrusted text (repository lines, the analyzer's draft). So it is run in
a way that leaves it nothing to act with, and every call is checked for it:

- no tools (``--tools ""`` removes the built-in ones), no MCP servers, no
  skills, hooks, plugins, memory files or project instructions
  (``--safe-mode``, ``--strict-mcp-config``, ``--disable-slash-commands``, no
  settings files), nobody to approve anything, one turn, nothing saved;
- its working directory is a new empty temporary folder, never a checkout;
- its environment is built from a short list: what it needs to find its login,
  and no key of any model provider, so that it answers on the subscription;
- what it reports about itself is read back: a session that started with a
  tool, an MCP server or a skill, that used a tool, ran a hook, was handed
  anything in the user's name, took more than one turn or was paid for with an
  API key is refused, and its reply is not used. A report of an unknown kind
  from the session itself is refused too, rather than taken to be harmless.

A failure of any kind (not installed, a non-zero exit, a timeout, a usage
limit, output that cannot be read) is an ``LLMError`` with a fixed message, as
a provider failure is; what the program printed goes to the log. There is no
retry: a call is made once. The program runs in a process group of its own, and
on a timeout the whole group is killed, so a wrapper cannot leave the real
process behind.

What this route cannot do: set the temperature, limit the reply's length or
ask for structured output (``NOT_APPLIED``), and say what a call cost.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from openmarketer_core.llm import ChatReply, LLMError

logger = logging.getLogger(__name__)

ROUTE = "claude_code"
DEFAULT_TIMEOUT_S = 180.0
# Settings of the judge role that have no counterpart on this route.
NOT_APPLIED = ("temperature", "max_tokens", "structured_output", "fallbacks")
# How the model is named when neither the caller nor Claude Code says which one answered.
UNNAMED_MODEL = "claude-code-default"

# The variables of this process that Claude Code may see. Everything else is withheld,
# model-provider keys and the variables of a Claude Code session this may be run from included.
INHERITED_VARIABLES = (
    "PATH",  # to find the program and what it starts
    "HOME",  # where its login is
    "USER",
    "LOGNAME",
    "TMPDIR",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
)

# Fixed sentences: nothing the program printed is repeated in an error.
NOT_INSTALLED = "Claude Code is not installed, or not on the PATH"
TIMED_OUT = "Claude Code did not answer in time"
FAILED = "Claude Code ended with an error"
UNREADABLE = "what Claude Code printed could not be read"
NOT_RESTRICTED = "Claude Code did not run without tools; its reply was not used"
PAID_WITH_A_KEY = "Claude Code would answer with an API key, not the subscription login"
NO_ANSWER = "Claude Code reported an error instead of an answer (a usage limit is one cause)"

_MAX_LOGGED_CHARS = 2000
# After the process group was killed: how long to wait for its pipes to close.
_REAP_TIMEOUT_S = 5.0

# What a restricted session reports about itself besides its start. ``thinking_tokens`` is a
# running count of tokens and carries nothing else. Any other kind (a hook that started or
# answered, for one) means the session was not what was asked for.
_HARMLESS_SYSTEM_EVENTS = ("init", "thinking_tokens")


def command_line(executable: str, system_prompt: str, model: str | None) -> list[str]:
    """The command for one question. The question itself is not in it: it goes to stdin."""
    command = [
        executable,
        "--print",
        "--output-format", "stream-json",  # one JSON object per line, the session's facts first
        "--verbose",  # stream-json needs it
        "--tools", "",  # no built-in tool exists in the session
        "--strict-mcp-config",  # and no MCP server, since no --mcp-config is given
        "--safe-mode",  # no CLAUDE.md, skills, plugins, hooks, custom commands or agents
        "--disable-slash-commands",
        "--setting-sources", "",  # no user, project or local settings file
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",  # whatever would ask is denied
        "--no-session-persistence",
        "--max-turns", "1",
        "--system-prompt", system_prompt,  # replaces Claude Code's own
    ]  # fmt: skip
    if model is None:
        return command
    if not model or model.startswith("-"):
        raise ValueError("a model name does not start with a dash")
    # One argument, so that the name can never be read as an option of its own.
    return [*command, f"--model={model}"]


def environment() -> dict[str, str]:
    """The environment Claude Code is run in: ``INHERITED_VARIABLES`` and nothing else."""
    return {name: os.environ[name] for name in INHERITED_VARIABLES if name in os.environ}


def _events(printed: str) -> list[dict[str, Any]]:
    events = []
    for line in printed.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError) as e:
            raise LLMError(UNREADABLE) from e
        if not isinstance(event, dict):
            raise LLMError(UNREADABLE)
        events.append(event)
    return events


def _one(events: list[dict[str, Any]], kind: str, subtype: str | None = None) -> dict[str, Any]:
    found = [
        event
        for event in events
        if event.get("type") == kind and (subtype is None or event.get("subtype") == subtype)
    ]
    if len(found) != 1:
        raise LLMError(UNREADABLE)
    return found[0]


def _acted_or_was_acted_on(events: list[dict[str, Any]]) -> bool:
    """Whether the session did anything but answer, or was given anything but the question.

    A ``user`` event in the output is something handed to the model in the
    user's name after the question (a tool's result is one); a ``system`` event
    other than the harmless ones is the session doing something (a hook); a
    block of the answer that is neither text nor thinking is a tool call.
    """
    for event in events:
        if event.get("type") == "user":
            return True
        if event.get("type") == "system" and event.get("subtype") not in _HARMLESS_SYSTEM_EVENTS:
            return True
        message = event.get("message") if event.get("type") == "assistant" else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") not in ("text", "thinking"):
                return True
    return False


def read_reply(printed: str, requested_model: str | None) -> ChatReply:
    """Turn what one restricted ``claude -p`` call printed into a reply, or refuse it.

    Raises ``LLMError`` with a fixed message when the output is not what a
    restricted single-turn session prints: see the module docstring for what is
    checked. The model is the one Claude Code says it used; failing that, the
    one asked for; failing that, ``UNNAMED_MODEL``.
    """
    events = _events(printed)
    started = _one(events, "system", "init")
    result = _one(events, "result")
    nothing_to_act_with = all(
        started.get(name) == [] for name in ("tools", "mcp_servers", "slash_commands", "skills")
    )
    # Present and empty: a result that does not say what it was denied is not taken on trust.
    nothing_was_denied = result.get("permission_denials") == []
    if not nothing_to_act_with or not nothing_was_denied or _acted_or_was_acted_on(events):
        raise LLMError(NOT_RESTRICTED)
    if started.get("apiKeySource") != "none":
        raise LLMError(PAID_WITH_A_KEY)
    answer = result.get("result")
    if result.get("is_error") is not False or result.get("subtype") != "success":
        raise LLMError(NO_ANSWER)
    if result.get("num_turns") != 1 or not isinstance(answer, str):
        raise LLMError(UNREADABLE)
    used = result.get("modelUsage")
    models = [name for name in used if isinstance(name, str)] if isinstance(used, dict) else []
    reported = models[0] if len(models) == 1 else started.get("model")
    model = reported if isinstance(reported, str) and reported else requested_model
    return ChatReply(
        message={"role": "assistant", "content": answer},
        model=model or UNNAMED_MODEL,
        cost_usd=0.0,  # not known on this route; ``JudgeVerdicts.cost_known`` says so
    )


@dataclass
class ClaudeCodeJudge:
    """``ChatModel`` that puts each question to a restricted ``claude -p`` call.

    ``model`` is passed on as ``--model`` when given; it is never chosen here.
    The role is not looked up anywhere: this route has no role configuration.
    """

    model: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    executable: str = "claude"

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatReply:
        if tools or tool_choice is not None:
            raise ValueError("the Claude Code judge takes no tools")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        question = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        command = command_line(self.executable, system, self.model)
        with tempfile.TemporaryDirectory(prefix="openmarketer-judge-") as empty_folder:
            done = _run(command, question, cwd=empty_folder, timeout_s=self.timeout_s)
        try:
            if done.returncode != 0:
                raise LLMError(FAILED)
            return read_reply(done.stdout, self.model)
        except LLMError:
            # For the operator. The error itself carries none of it.
            logger.warning(
                "claude -p exited with %s: stdout %r, stderr %r",
                done.returncode,
                done.stdout[-_MAX_LOGGED_CHARS:],
                done.stderr[-_MAX_LOGGED_CHARS:],
            )
            raise


@dataclass(frozen=True)
class _Finished:
    returncode: int
    stdout: str
    stderr: str


def _run(command: list[str], question: str, *, cwd: str, timeout_s: float) -> _Finished:
    """Run the program once, with the question on its stdin, and wait at most ``timeout_s``.

    It is the leader of a new session, hence of a new process group. When time
    is up the whole group is killed, not only the program started here: a
    wrapper script that started the real program must not leave it running.
    Its pipes are then closed, so nothing is left to wait on.
    """
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment(),
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise LLMError(NOT_INSTALLED) from e
    try:
        stdout, stderr = process.communicate(question, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logger.warning("claude -p was stopped after %ss", timeout_s)
        _kill_group(process)
        raise LLMError(TIMED_OUT) from None
    return _Finished(returncode=process.returncode, stdout=stdout, stderr=stderr)


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Kill everything in the program's process group and leave no pipe or child behind."""
    try:
        os.killpg(process.pid, signal.SIGKILL)  # its pid is its group's id: it leads a session
    except ProcessLookupError:
        pass  # the group is gone already
    try:
        process.communicate(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Something that left the group still holds a pipe open. Stop reading from it.
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        process.wait()
