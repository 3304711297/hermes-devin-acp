"""Current Hermes provider for the official Devin CLI over ACP.

This plugin deliberately keeps the Devin integration out of Hermes core. It uses
Hermes' external-process ProviderProfile seam and a small ACP client that speaks
Devin's current wire shape.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# Subcommands of the Devin CLI that are mutually exclusive with `models list`.
_ACP_SUBCOMMANDS = frozenset({"acp"})


class DevinACPClient:
    """OpenAI-client-shaped facade over ``devin acp``.

    Devin is the actual coding agent. Hermes supplies the host-side filesystem and
    terminal capabilities needed by ACP and receives the final agent message.
    """

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        acp_cwd: str | None = None,
        **_: Any,
    ) -> None:
        self.api_key = api_key or "devin-acp"
        self.base_url = base_url or "acp://devin"
        self._default_headers = dict(default_headers or {})
        self._command = command or _resolve_command()
        self._base_args = list(args or _resolve_args())
        self._cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = _ChatNamespace(self)
        self.is_closed = False
        self._proc: subprocess.Popen[str] | None = None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        self.is_closed = True
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def list_models(self, timeout_seconds: float = 15.0) -> list[str]:
        """Return the models this account can actually select in an ACP session.

        The ACP ``session/new`` config option is the only authority on what
        ``session/set_config_option`` will accept, so it wins over the much
        larger ``devin models list`` catalog (which is account-global, not
        selectable per session). The catalog is used only when the session
        advertises nothing at all, so an offline/flaky session probe does not
        leave the user with an empty picker.
        """
        session_models = self._list_session_models(timeout_seconds)
        if session_models:
            return session_models

        catalog = self._list_cli_catalog(timeout_seconds)
        if catalog:
            logger.warning(
                "Devin ACP session advertised no model options; exposing the unverified "
                "CLI catalog (%d models). Unlisted selections resolve to the session default.",
                len(catalog),
            )
        return catalog

    def _list_session_models(self, timeout_seconds: float) -> list[str]:
        """Model values advertised by a real ACP session, or [] when unavailable."""
        try:
            session = self._new_session(timeout_seconds=timeout_seconds, allow_terminal=False)
        except Exception as exc:
            logger.debug("Devin ACP session model probe failed: %s", exc)
            return []
        finally:
            self.close()
        return _session_model_ids(session)

    def _list_cli_catalog(self, timeout_seconds: float) -> list[str]:
        """The account-wide `devin models list` catalog, or [] when unavailable."""
        try:
            result = subprocess.run(
                [self._command, *self._catalog_args(), "models", "list", "--format", "json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return _parse_model_catalog(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.debug("Devin CLI model catalog unavailable: %s", exc)
        return []

    def _catalog_args(self) -> list[str]:
        """Base args minus the ACP subcommand.

        ``devin models list`` is a sibling of ``devin acp``, so the configured ACP
        args must not be forwarded to it, while unrelated flags (config paths,
        verbosity) still are.
        """
        return [arg for arg in self._base_args if arg not in _ACP_SUBCOMMANDS]

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: Any = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        requested_model = str(model or "").strip()
        prompt = _format_messages(messages or [])
        timeout_seconds = _normalise_timeout(timeout)
        response_text, reasoning_text, actual_model = self._run_prompt(
            prompt,
            timeout_seconds=timeout_seconds,
            model=requested_model,
        )
        message = _SimpleNamespace(
            content=response_text,
            tool_calls=[],
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        completion = _SimpleNamespace(
            choices=[_SimpleNamespace(message=message, finish_reason="stop")],
            usage=_SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                prompt_tokens_details=_SimpleNamespace(cached_tokens=0),
            ),
            model=actual_model or requested_model or "devin-acp",
        )
        if not stream:
            return completion
        return _completion_to_stream_chunks(completion)

    def _run_prompt(self, prompt: str, *, timeout_seconds: float, model: str) -> tuple[str, str, str]:
        session = self._new_session(timeout_seconds=timeout_seconds, allow_terminal=True, model=model)
        session_id = str(session.get("sessionId") or "").strip()
        if not session_id:
            raise RuntimeError("Devin ACP did not return a sessionId.")

        requested_model = model.strip()
        effective_model = requested_model
        if requested_model:
            effective_model = (
                _apply_model_option(
                    self,
                    session,
                    requested_model,
                    timeout_seconds=timeout_seconds,
                )
                or requested_model
            )

        # After model selection, continue within the same ACP process/session.
        process, inbox, stderr_tail = self._runtime
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        self._request(
            process,
            inbox,
            stderr_tail,
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            },
            timeout_seconds=timeout_seconds,
            text_parts=text_parts,
            reasoning_parts=reasoning_parts,
        )

        actual_model = effective_model or requested_model
        stopped_model = self._actual_model_from_stopped_update
        if stopped_model:
            actual_model = stopped_model
        self.close()
        return "".join(text_parts), "".join(reasoning_parts), actual_model

    def _new_session(
        self,
        *,
        timeout_seconds: float,
        allow_terminal: bool,
        model: str = "",
    ) -> dict[str, Any]:
        args = list(self._base_args)
        if model:
            # Official CLI syntax: global --model is accepted with devin acp.
            # Only pass values that cannot make the CLI reject startup; the
            # session-authoritative value is applied later via set_config_option.
            model_flag = _cli_model_flag(model)
            if model_flag and "--model" not in args:
                args = [*args, "--model", model_flag]
        self._spawn(args)

        process = self._proc
        if process is None or process.stdin is None or process.stdout is None:
            self.close()
            raise RuntimeError("Could not start Devin ACP with stdio pipes.")

        import queue
        import threading
        from collections import deque

        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=50)

        def pump_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    inbox.put(json.loads(line))
                except Exception:
                    inbox.put({"raw": line.rstrip("\n")})

        def pump_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_tail.append(line.rstrip("\n"))

        threading.Thread(target=pump_stdout, daemon=True).start()
        threading.Thread(target=pump_stderr, daemon=True).start()

        self._runtime = (process, inbox, stderr_tail)
        self._actual_model_from_stopped_update = ""

        init = {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
            # Current Devin ACP builds have a Windsurf-derived compatibility gate.
            # The version is configurable so a future upstream bump does not require
            # a Hermes release merely to update client metadata.
            "clientInfo": {
                "name": "windsurf",
                "title": "Windsurf",
                "version": os.environ.get("HERMES_DEVIN_ACP_WINDSURF_VERSION", "1.110.1"),
            },
        }
        self._request(
            process, inbox, stderr_tail, "initialize", init, timeout_seconds=timeout_seconds
        )
        session = self._request(
            process,
            inbox,
            stderr_tail,
            "session/new",
            {"cwd": self._cwd, "mcpServers": []},
            timeout_seconds=timeout_seconds,
        ) or {}
        if not isinstance(session, dict):
            raise RuntimeError("Devin ACP returned an invalid session/new result.")
        self._apply_mode_option(session, timeout_seconds=timeout_seconds)
        return session

    def _apply_mode_option(self, session: dict[str, Any], *, timeout_seconds: float) -> None:
        mode = os.environ.get("HERMES_DEVIN_ACP_MODE", "").strip()
        if not mode:
            return
        option = next(
            (
                item
                for item in (session.get("configOptions") or [])
                if isinstance(item, dict)
                and (item.get("id") == "mode" or item.get("category") == "mode")
            ),
            None,
        )
        if not option:
            logger.warning("Devin ACP did not advertise a mode config option; ignoring HERMES_DEVIN_ACP_MODE=%r.", mode)
            return
        valid = {
            str(item.get("value"))
            for item in (option.get("options") or [])
            if isinstance(item, dict) and item.get("value") is not None
        }
        if valid and mode not in valid:
            raise RuntimeError(
                f"Devin ACP does not advertise mode '{mode}'. Available modes: {', '.join(sorted(valid))}."
            )
        process, inbox, stderr_tail = self._runtime
        self._request(
            process,
            inbox,
            stderr_tail,
            "session/set_config_option",
            {
                "sessionId": str(session.get("sessionId")),
                "configId": str(option.get("id") or "mode"),
                "value": mode,
            },
            timeout_seconds=timeout_seconds,
        )

    def _spawn(self, args: list[str]) -> None:
        if self._proc is not None:
            self.close()
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags
            creationflags = windows_hide_flags()
        except Exception:
            creationflags = 0

        env = os.environ.copy()
        try:
            from tools.environments.local import hermes_subprocess_env
            env = hermes_subprocess_env(inherit_credentials=True)
        except Exception:
            pass

        try:
            self._proc = subprocess.Popen(
                [self._command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                env=env,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Devin CLI was not found: '{self._command}'. Install Devin CLI and run `devin auth login`."
            ) from exc

    def _request(
        self,
        process: subprocess.Popen[str],
        inbox: Any,
        stderr_tail: Any,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        text_parts: list[str] | None = None,
        reasoning_parts: list[str] | None = None,
    ) -> Any:
        import time

        request_id = getattr(self, "_next_id", 0) + 1
        self._next_id = request_id
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                msg = inbox.get(timeout=0.1)
            except Exception:
                continue
            if self._handle_message(msg, process, text_parts, reasoning_parts):
                continue
            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                err = msg.get("error") or {}
                raise RuntimeError(f"Devin ACP {method} failed: {err.get('message') or err}")
            return msg.get("result")

        stderr_text = "\n".join(stderr_tail).strip()
        if stderr_text:
            raise RuntimeError(f"Devin ACP process failed during {method}: {stderr_text}")
        raise TimeoutError(f"Timed out waiting for Devin ACP response to {method}.")

    def _handle_message(
        self,
        msg: dict[str, Any],
        process: subprocess.Popen[str],
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
    ) -> bool:
        method = msg.get("method")
        # JSON-RPC responses have no method field; let _request() match them
        # against its request id instead of replying with a spurious error.
        if not isinstance(method, str):
            return False
        if method == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            kind = str(update.get("sessionUpdate") or "").strip()
            content = update.get("content") or {}
            text = str(content.get("text") or "") if isinstance(content, dict) else ""
            if kind == "agent_message_chunk" and text and text_parts is not None:
                text_parts.append(text)
            elif kind == "agent_thought_chunk" and text and reasoning_parts is not None:
                reasoning_parts.append(text)
            if kind in {"agent_stopped", "agent_end"} and isinstance(update, dict):
                maybe_model = ((update.get("_meta") or {}).get("model") or update.get("model"))
                if maybe_model:
                    self._actual_model_from_stopped_update = str(maybe_model)
            return True

        params = msg.get("params") or {}
        message_id = msg.get("id")
        if method == "session/request_permission":
            # Hermes has no interactive ACP permission surface here. Default to
            # canceling permission requests rather than silently auto-approving.
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"outcome": {"outcome": "cancelled"}},
            }
            process.stdin.write(json.dumps(response) + "\n")
            process.stdin.flush()
            return True

        try:
            if method == "fs/read_text_file":
                result = _read_text_file(params, self._cwd)
            elif method == "fs/write_text_file":
                result = _write_text_file(params, self._cwd)
            elif method == "terminal/create":
                result = _terminal_create(params, self._cwd)
            elif method == "terminal/output":
                result = _terminal_output(params)
            elif method == "terminal/wait_for_exit":
                result = _terminal_wait(params)
            elif method == "terminal/kill":
                result = _terminal_kill(params)
            elif method == "terminal/release":
                result = _terminal_release(params)
            elif method == "_cognition.ai/request_diagnostics":
                result = {}
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unsupported Devin ACP client method: {method}",
                    },
                }
                process.stdin.write(json.dumps(response) + "\n")
                process.stdin.flush()
                return True
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32602, "message": str(exc)},
            }
            process.stdin.write(json.dumps(response) + "\n")
            process.stdin.flush()
            return True

        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
        process.stdin.flush()
        return True


class _SimpleNamespace:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _ChatCompletions:
    def __init__(self, client: DevinACPClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ChatNamespace:
    def __init__(self, client: DevinACPClient) -> None:
        self.completions = _ChatCompletions(client)


def _resolve_command() -> str:
    return (
        os.environ.get("HERMES_DEVIN_ACP_COMMAND", "").strip()
        or os.environ.get("DEVIN_CLI_PATH", "").strip()
        or "devin"
    )


def _resolve_args() -> list[str]:
    raw = os.environ.get("HERMES_DEVIN_ACP_ARGS", "").strip()
    return shlex.split(raw) if raw else ["acp"]


def _normalise_timeout(timeout: Any) -> float:
    if isinstance(timeout, (int, float)):
        return float(timeout)
    values = [getattr(timeout, x, None) for x in ("read", "write", "connect", "pool", "timeout")]
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return max(nums) if nums else 900.0


def _format_messages(messages: list[dict[str, Any]]) -> str:
    sections: list[str] = [
        "You are the active Devin coding-agent backend for Hermes.",
        "Operate on the current workspace and complete the user's task end-to-end.",
    ]
    transcript: list[str] = []
    labels = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool"}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "context").strip().lower()
        content = message.get("content")
        rendered = _render_content(content)
        if rendered:
            transcript.append(f"{labels.get(role, 'Context')}:\n{rendered}")
    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
    sections.append("Continue from the latest user request.")
    return "\n\n".join(sections)


def _render_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"].strip()
        if isinstance(content.get("content"), str):
            return content["content"].strip()
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"].strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _parse_model_catalog(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except Exception:
        return []

    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        found.append(value)

    def walk(node: Any, key_hint: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, key_hint)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            lk = str(key).lower()
            if lk in {"id", "modelid", "model_id", "slug", "value"}:
                if isinstance(value, str):
                    add(value)
            elif lk in {"models", "availablemodels", "available_models", "options"}:
                walk(value, lk)
            elif isinstance(value, (dict, list)):
                walk(value, lk)

    walk(data)
    # Guard against accidentally treating a family/display label as a model.
    return [m for m in found if _looks_like_model_id(m)]


def _looks_like_model_id(value: str) -> bool:
    lowered = value.lower()
    return any(
        token in lowered
        for token in ("swe", "gpt", "claude", "opus", "sonnet", "gemini", "codex", "kimi", "glm", "deepseek")
    ) or lowered in {"adaptive", "swe", "gpt", "claude", "gemini", "codex"}


def _session_model_ids(session: dict[str, Any]) -> list[str]:
    for option in session.get("configOptions") or []:
        if not isinstance(option, dict):
            continue
        if option.get("id") != "model" and option.get("category") != "model":
            continue
        result: list[str] = []
        for item in option.get("options") or []:
            if isinstance(item, dict):
                value = item.get("value")
                if isinstance(value, str) and value.strip():
                    result.append(value.strip())
        return list(dict.fromkeys(result))

    result = []
    for item in ((session.get("models") or {}).get("availableModels") or []):
        if isinstance(item, dict):
            value = item.get("modelId")
            if isinstance(value, str) and value.strip():
                result.append(value.strip())
    return list(dict.fromkeys(result))


def _model_option(session: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            option
            for option in (session.get("configOptions") or [])
            if isinstance(option, dict)
            and (option.get("id") == "model" or option.get("category") == "model")
        ),
        None,
    )


class _ModelChoice(_SimpleNamespace):
    """The model value that will really be sent, plus why it differs from the request."""

    value: str
    note: str


def _advertised_model_values(option: dict[str, Any] | None) -> list[str]:
    """Session-advertised model values in Devin's own order (duplicates removed)."""
    if not option:
        return []
    result: list[str] = []
    for item in option.get("options") or []:
        if isinstance(item, dict):
            value = item.get("value")
            if isinstance(value, str) and value.strip():
                result.append(value.strip())
    return list(dict.fromkeys(result))


def _resolve_model_choice(
    requested_model: str,
    advertised: list[str],
    default: str = "",
) -> _ModelChoice:
    """Map a requested model onto a value the live ACP session actually accepts.

    Resolution order:
      1. exact match on an advertised value;
      2. dotted-version normalization (``swe-1.6-slow`` -> ``swe-1-6-slow``);
      3. family match on every token of the request (``swe``, ``claude-sonnet``),
         picking the earliest entry Devin itself advertises;
      4. the session default, with the substitution reported in ``note``.

    Raises only when the session advertised no model option at all, which means
    this Devin build has no model selection surface to drive.
    """
    requested = (requested_model or "").strip()
    if not advertised:
        raise RuntimeError(
            "Devin ACP did not advertise a model config option; cannot select a model."
        )

    if requested in advertised:
        return _ModelChoice(value=requested, note="")

    normalized = requested.replace(".", "-")
    if normalized in advertised:
        return _ModelChoice(value=normalized, note="")

    # Family aliases such as "swe" / "claude" / "sonnet": match on every token of
    # the request so "swe-2" still resolves when the session only advertises
    # "swe-2-enterprise". Among matches, the earliest advertised entry wins:
    # Devin lists its current family default first, so "swe" behaves like
    # Devin's own shorthand for the family instead of an arbitrary pick, and the
    # result never depends on set/dict iteration order.
    family_tokens = [t for t in requested.lower().replace("_", "-").split("-") if t]
    if family_tokens:
        value = next(
            (
                candidate
                for candidate in advertised
                if all(token in candidate.lower() for token in family_tokens)
            ),
            "",
        )
        if value:
            return _ModelChoice(value=value, note=f"requested '{requested}' -> '{value}'")

    fallback = default if default in advertised else advertised[0]
    return _ModelChoice(
        value=fallback,
        note=(
            f"requested '{requested}' is not advertised by this Devin session; "
            f"using session default '{fallback}' instead"
        ),
    )


def _apply_model_option(
    client: DevinACPClient,
    session: dict[str, Any],
    requested_model: str,
    *,
    timeout_seconds: float,
) -> str:
    """Select the Devin session model, degrading instead of failing.

    Returns the model value that was really sent to ``session/set_config_option``
    (which may differ from ``requested_model`` when the session is narrower than
    the account catalog), so callers and logs report the truth.
    """
    option = _model_option(session)
    if not option:
        logger.warning(
            "Devin ACP did not advertise a model config option; leaving the session default in place."
        )
        return ""

    advertised = _advertised_model_values(option)
    default = str(option.get("currentValue") or "").strip()
    choice = _resolve_model_choice(requested_model, advertised, default)
    if choice.note:
        logger.warning(
            "Devin ACP model '%s' resolved to '%s' (%s). Advertised models: %s.",
            requested_model,
            choice.value,
            choice.note,
            ", ".join(advertised),
        )

    process, inbox, stderr_tail = client._runtime
    client._request(
        process,
        inbox,
        stderr_tail,
        "session/set_config_option",
        {
            "sessionId": str(session.get("sessionId")),
            "configId": str(option.get("id") or "model"),
            "value": choice.value,
        },
        timeout_seconds=timeout_seconds,
    )
    return choice.value


def _cli_model_flag(model: str, advertised: list[str] | None = None) -> str:
    """Return a ``--model`` value for process startup, or "" to omit the flag.

    Only family aliases (``swe``) and values this account's session actually
    advertises are safe: Devin CLI rejects unknown ids at startup, which would
    make an unrelated model choice break the whole session.
    """
    requested = (model or "").strip()
    if not requested:
        return ""
    advertised = list(advertised or [])
    if advertised:
        return _resolve_model_choice(requested, advertised).value
    return requested if requested == "swe" else ""


def _path_within_cwd(path_text: str, cwd: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        raise PermissionError("ACP paths must be absolute.")
    resolved, root = path.resolve(), Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path '{resolved}' is outside workspace '{root}'.") from exc
    return resolved


def _read_text_file(params: dict[str, Any], cwd: str) -> dict[str, str]:
    path = _path_within_cwd(str(params.get("path") or ""), cwd)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    line, limit = params.get("line"), params.get("limit")
    if isinstance(line, int) and line > 0:
        rows = content.splitlines(keepends=True)
        start = line - 1
        end = start + limit if isinstance(limit, int) and limit > 0 else None
        content = "".join(rows[start:end])
    return {"content": content}


def _write_text_file(params: dict[str, Any], cwd: str) -> None:
    path = _path_within_cwd(str(params.get("path") or ""), cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(params.get("content") or ""), encoding="utf-8")
    return None


class _TerminalState:
    def __init__(self, process: subprocess.Popen[str], output_limit: int = 1_000_000) -> None:
        import threading

        self.process = process
        self.output_limit = output_limit
        self._chunks: list[str] = []
        self._size = 0
        self._lock = threading.Lock()
        self.truncated = False
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def _read_output(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        for chunk in iter(stream.readline, ""):
            encoded_size = len(chunk.encode("utf-8", errors="replace"))
            with self._lock:
                remaining = self.output_limit - self._size
                if remaining <= 0:
                    self.truncated = True
                    continue
                if encoded_size > remaining:
                    # Keep the visible output bounded by the requested ACP limit.
                    kept = chunk.encode("utf-8", errors="replace")[:remaining].decode("utf-8", errors="ignore")
                    self._chunks.append(kept)
                    self._size += len(kept.encode("utf-8"))
                    self.truncated = True
                else:
                    self._chunks.append(chunk)
                    self._size += encoded_size

    def output(self) -> str:
        # Give a just-started process a small, bounded opportunity to publish its
        # first chunk without ever blocking the ACP request for long-running tools.
        self._reader.join(timeout=0.05)
        with self._lock:
            return "".join(self._chunks)


_TERMINALS: dict[str, _TerminalState] = {}


def _terminal_create(params: dict[str, Any], cwd: str) -> dict[str, str]:
    command = str(params.get("command") or "")
    args = params.get("args") or []
    term_cwd = str(params.get("cwd") or cwd)
    _path_within_cwd(term_cwd, cwd)
    if not command:
        raise ValueError("terminal/create requires command")
    if not isinstance(args, list):
        raise ValueError("terminal/create args must be a list")
    env = os.environ.copy()
    for item in params.get("env") or []:
        if isinstance(item, dict) and item.get("name"):
            env[str(item["name"])] = str(item.get("value") or "")
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags
        creationflags = windows_hide_flags()
    except Exception:
        creationflags = 0
    proc = subprocess.Popen(
        [command, *[str(a) for a in args]],
        cwd=term_cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    output_limit = params.get("outputByteLimit")
    try:
        output_limit = int(output_limit) if output_limit is not None else 1_000_000
    except (TypeError, ValueError):
        output_limit = 1_000_000
    state = _TerminalState(proc, max(1, output_limit))
    terminal_id = f"hermes-devin-term-{id(proc)}"
    _TERMINALS[terminal_id] = state
    return {"terminalId": terminal_id}


def _terminal_output(params: dict[str, Any]) -> dict[str, Any]:
    state = _get_terminal(params)
    result: dict[str, Any] = {
        "output": state.output(),
        "truncated": state.truncated,
    }
    if state.process.poll() is not None:
        result["exitStatus"] = {"exitCode": state.process.returncode}
    return result


def _terminal_wait(params: dict[str, Any]) -> dict[str, Any]:
    state = _get_terminal(params)
    state.process.wait()
    return {"exitCode": state.process.returncode, "signal": None}


def _terminal_kill(params: dict[str, Any]) -> None:
    state = _get_terminal(params)
    state.process.kill()
    return None


def _terminal_release(params: dict[str, Any]) -> None:
    terminal_id = str(params.get("terminalId") or "")
    state = _TERMINALS.pop(terminal_id, None)
    if state is not None and state.process.poll() is None:
        state.process.terminate()
    return None


def _get_terminal(params: dict[str, Any]) -> _TerminalState:
    terminal_id = str(params.get("terminalId") or "")
    state = _TERMINALS.get(terminal_id)
    if state is None:
        raise KeyError(f"Unknown terminalId '{terminal_id}'")
    return state


def _completion_to_stream_chunks(completion: _SimpleNamespace) -> list[_SimpleNamespace]:
    message = completion.choices[0].message
    return [
        _SimpleNamespace(
            choices=[
                _SimpleNamespace(
                    index=0,
                    delta=_SimpleNamespace(
                        role="assistant",
                        content=message.content or None,
                        tool_calls=None,
                        reasoning=message.reasoning,
                        reasoning_content=message.reasoning_content,
                    ),
                    finish_reason="stop",
                )
            ],
            model=completion.model,
            usage=None,
        ),
        _SimpleNamespace(choices=[], model=completion.model, usage=completion.usage),
    ]


def _session_model_ids_safe(session: dict[str, Any]) -> list[str]:
    return _session_model_ids(session)


class DevinACPProfile(ProviderProfile):
    def create_client(self, **client_kwargs: Any) -> DevinACPClient:
        return DevinACPClient(**client_kwargs)

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> list[str] | None:
        try:
            creds = _resolve_external_credentials(self)
            client = self.create_client(
                api_key=creds.get("api_key"),
                base_url=creds.get("base_url"),
                command=creds.get("command"),
                args=creds.get("args"),
            )
            models = client.list_models(timeout_seconds=timeout)
            return models or None
        except Exception as exc:
            logger.debug("Devin model catalog unavailable: %s", exc)
            return None


def _resolve_external_credentials(profile: ProviderProfile) -> dict[str, Any]:
    try:
        from hermes_cli.auth import resolve_external_process_provider_credentials
        return resolve_external_process_provider_credentials(profile.name)
    except Exception:
        return {
            "api_key": profile.name,
            "base_url": profile.base_url,
            "command": _resolve_command(),
            "args": _resolve_args(),
        }


devin_acp = DevinACPProfile(
    name="devin-acp",
    aliases=("devin", "devin-subscription", "swe-2-devin"),
    display_name="Devin Subscription",
    description="Official Devin CLI models over ACP",
    signup_url="https://app.devin.ai/",
    api_mode="chat_completions",
    auth_type="external_process",
    base_url="acp://devin",
    supports_health_check=False,
    process_command="devin",
    process_args=("acp",),
    process_command_env_vars=("HERMES_DEVIN_ACP_COMMAND", "DEVIN_CLI_PATH"),
    process_args_env_var="HERMES_DEVIN_ACP_ARGS",
    fallback_models=("swe",),
)

register_provider(devin_acp)
