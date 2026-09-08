#!/usr/bin/env python3
"""
The agent's connection to its MCP servers — Infoblox for DDI, AWS for the cloud.

WHY THIS FILE EXISTS
--------------------
The Claude API has an `mcp_servers` connector that would make most of this
unnecessary: Claude connects to the remote MCP server itself and you never write
a tool loop. That connector is not available on Amazon Bedrock, which is where
this lab's Claude runs. So the agent connects to the servers itself, lists their
tools, and executes tool calls on Claude's behalf.

That turned out to be a feature rather than a workaround. It makes visible
exactly which server answered which question — and in Part 3, where a single
sentence from the participant produces a run of Infoblox calls followed by a run
of AWS calls, seeing that interleaving IS the lesson.

TWO SERVERS, ONE CONVERSATION
-----------------------------
    infoblox   https://csp.infoblox.com/mcp        Streamable HTTP, Token auth
    aws        awslabs.aws-api-mcp-server (uvx)    stdio, standard boto3 chain

Tool names are prefixed with the server id (`infoblox__get_dns_zones`) so Claude
can always tell where a tool comes from, and so two servers that both expose a
`list_resources` cannot collide. The prefix is stripped again before the call
goes out to the server.

AUTHENTICATION
--------------
Infoblox: `Authorization: Token <service api key>` — the same header the MCP
Server expects everywhere. The key is read from disk on EVERY connection rather
than captured once, so swapping the file swaps the agent's permissions with no
restart. That is what makes the read-only -> read/write handover in Part 2 real
rather than narrative.

AWS: nothing is passed here at all. The stdio server inherits the process
environment and resolves credentials through the standard chain, which
track_scripts/setup-shell has pointed at /root/.aws/credentials.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import lab_config as cfg  # noqa: E402


PREFIX_SEPARATOR = "__"


class McpUnavailable(RuntimeError):
    """An MCP server could not be reached or refused the connection."""


# --------------------------------------------------------------------------- #
# Transport discovery
# --------------------------------------------------------------------------- #
#
# The `mcp` package has moved this factory's name around between releases, and
# importing a specific symbol at module scope turns a rename into an ImportError
# at the participant's first question. Look it up instead.
#
# Order matters: Streamable HTTP first because it is the current standard and
# what a hosted /mcp endpoint most likely serves; HTTP+SSE second as the older
# fallback. Set MCP_TRANSPORT to pin one once you know which it is.

_STREAMABLE_HTTP_NAMES = (
    "streamablehttp_client",       # documented name in mcp 1.9-era releases
    "streamable_http_client",      # snake_case variant
    "streamablehttp",
    "connect",
)
_SSE_NAMES = ("sse_client", "connect_sse", "connect")


def _factories_in(module_name, candidate_names):
    """Every plausible client factory a transport module exposes, best first."""
    try:
        module = __import__(module_name, fromlist=["*"])
    except ImportError:
        return []

    found, seen = [], set()
    for name in candidate_names:
        fn = getattr(module, name, None)
        if callable(fn) and name not in seen:
            found.append((name, fn))
            seen.add(name)

    # Nothing matched the known names — the package renamed it again. Take any
    # public callable whose name looks like a client factory rather than giving
    # up, and let the connection attempt decide whether it was the right one.
    if not found:
        for name in dir(module):
            if name.startswith("_") or name in seen:
                continue
            if name.endswith("client") or name.startswith("connect"):
                fn = getattr(module, name)
                if callable(fn):
                    found.append((name, fn))
    return found


def _http_client(headers):
    """
    An HTTP client the installed MCP transport will accept, carrying our auth.

    DO NOT `import httpx` HERE. mcp 2.x is built on **httpx2**, not httpx — its
    type hints read `httpx2.AsyncClient` — and the venv has no `httpx` at all,
    so importing it by name is a ModuleNotFoundError dressed up as a connection
    failure. Guessing the other name instead would be the same mistake with a
    different spelling.

    So ask the SDK. `create_mcp_http_client(headers=...)` is exported from both
    transport modules and returns whatever client that build expects, already
    configured the way MCP wants it. The direct imports below are only for a
    build that does not export it.
    """
    for module_name in ("mcp.shared._httpx_utils",
                        "mcp.client.streamable_http",
                        "mcp.client.sse"):
        try:
            module = __import__(module_name, fromlist=["create_mcp_http_client"])
        except ImportError:
            continue
        factory = getattr(module, "create_mcp_http_client", None)
        if callable(factory):
            return factory(headers=headers)

    # No SDK factory. Try the http libraries it might be built on, newest
    # convention first.
    for library in ("httpx2", "httpx"):
        try:
            module = __import__(library)
        except ImportError:
            continue
        return module.AsyncClient(headers=headers, timeout=60.0)

    raise McpUnavailable(
        "The installed mcp package exposes no create_mcp_http_client, and "
        "neither httpx2 nor httpx is importable — there is no way to build an "
        "authenticated HTTP client. Check the agent venv."
    )


def _transport_candidates():
    """[(label, factory), ...] to try in order."""
    pinned = os.environ.get("MCP_TRANSPORT", "").strip().lower()

    transports = [
        ("streamable-http", "mcp.client.streamable_http", _STREAMABLE_HTTP_NAMES),
        ("sse", "mcp.client.sse", _SSE_NAMES),
    ]
    if pinned:
        transports = [t for t in transports if t[0] == pinned] or transports

    candidates = []
    for label, module_name, names in transports:
        for symbol, factory in _factories_in(module_name, names):
            candidates.append((f"{label} ({module_name}.{symbol})", factory))
    return candidates


# --------------------------------------------------------------------------- #
# Infoblox credential
# --------------------------------------------------------------------------- #

def read_key():
    """
    The Infoblox Service API key currently in force.

    Read fresh every time. 02/setup-shell writes the read/write key here when
    the participant reaches the remediation step; 04/setup-shell writes the
    read-only key back for the RBAC exercise.
    """
    try:
        with open(cfg.AGENT_KEY_FILE) as handle:
            key = handle.read().strip()
    except FileNotFoundError:
        raise McpUnavailable(
            f"No Infoblox Service API key at {cfg.AGENT_KEY_FILE}. The track "
            f"setup did not finish — restart the track."
        ) from None
    if not key:
        raise McpUnavailable(f"{cfg.AGENT_KEY_FILE} is empty.")
    return key


def auth_headers():
    return {"Authorization": f"Token {read_key()}"}


# --------------------------------------------------------------------------- #
# One session per server
# --------------------------------------------------------------------------- #

class McpFleet:
    """
    Every MCP server the agent has, held open for the duration of one turn.

    Opened per turn rather than per process so that a key swap between turns
    takes effect, and so a dropped connection to one server cannot wedge the
    whole app.

    A failure to reach AWS is NOT fatal — Parts 1, 2 and 4 do not need it, and a
    participant should not lose the whole assistant because a stdio server would
    not start. A failure to reach Infoblox IS fatal, because without it there is
    no lab.
    """

    def __init__(self, infoblox_url=None, with_aws=None):
        self.infoblox_url = infoblox_url or cfg.MCP_SERVER_URL
        self.with_aws = cfg.AWS_MCP_ENABLED if with_aws is None else with_aws
        self._exit_stack = None
        self._sessions = {}       # server id -> ClientSession
        self.warnings = []        # non-fatal problems worth showing the learner
        self.transport = None     # which transport actually connected

    # ------------------------------------------------------------- lifecycle -

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        self._exit_stack = AsyncExitStack()
        try:
            await self._connect_infoblox()
        except Exception:
            await self._exit_stack.aclose()
            raise

        if self.with_aws:
            try:
                await self._connect_aws()
            except Exception as exc:                    # noqa: BLE001
                # Deliberately non-fatal. See the class docstring.
                self.warnings.append(
                    f"The AWS MCP server did not start ({exc}). Infoblox tools "
                    f"still work; the cloud deployment in Part 3 will not."
                )
        return self

    async def __aexit__(self, *exc_info):
        if self._exit_stack:
            await self._exit_stack.aclose()

    async def _connect_infoblox(self):
        """
        Connect to the hosted Infoblox MCP Server, negotiating the transport.

        TWO THINGS VARY HERE AND BOTH BIT US.

        First, WHICH TRANSPORT the server speaks. Streamable HTTP is the current
        standard and what a hosted `/mcp` endpoint almost certainly serves, but
        HTTP+SSE is the older alternative and some deployments still use it.

        Second, WHAT THE CLIENT LIBRARY CALLS IT. `mcp.client.streamable_http`
        exists across versions but the factory inside it has not kept one name —
        a pinned `mcp>=1.9.0` resolved to a build where importing
        `streamablehttp_client` raises ImportError even though the module is
        right there. Importing a specific symbol at module scope turns that into
        a dead assistant with an error that looks like a packaging fault.

        So: try each candidate in turn and use the first that connects. The
        winner is recorded in `self.transport` and shown in the UI sidebar, which
        also answers TODO-27 empirically on the first real run instead of by
        reading documentation.
        """
        from mcp import ClientSession

        attempts, last_exc = [], None

        for label, factory in _transport_candidates():
            try:
                streams = await self._exit_stack.enter_async_context(
                    await self._invoke_factory(factory)
                )
                # 1.x streamablehttp yields (read, write, get_session_id); 2.x
                # and sse yield (read, write). Take the first two either way.
                read, write = streams[0], streams[1]
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
            except Exception as exc:                    # noqa: BLE001
                attempts.append(f"{label}: {type(exc).__name__}: {exc}")
                last_exc = exc
                continue

            self._sessions["infoblox"] = session
            self.transport = label
            return

        raise McpUnavailable(
            f"Could not connect to the Infoblox MCP Server at "
            f"{self.infoblox_url} over any supported transport.\n\n"
            + "\n".join(f"  - {a}" for a in attempts)
            + "\n\nIf every attempt is an ImportError, the installed `mcp` "
              "package does not expose the transports this agent knows about — "
              "run `python3 scripts/mcp_probe.py` to see what it does export. "
              "If they are connection or auth errors instead, note that access "
              "is gated by role-based access control: a service user with no "
              "MCP Server role is refused outright rather than degraded."
        ) from last_exc

    async def _invoke_factory(self, factory):
        """
        Call a transport factory with authentication, whichever way it takes it.

        THE 1.x / 2.x SPLIT. These are not the same function:

            # mcp 1.x
            streamablehttp_client(url, headers={...})
                -> yields (read, write, get_session_id)

            # mcp 2.x  — renamed, and headers are gone
            streamable_http_client(url, *, http_client=None,
                                   terminate_on_close=True)
                -> yields (read, write)

        In 2.x authentication goes on a pre-built httpx client instead of a
        headers kwarg, so passing `headers=` raises TypeError — the function is
        found, and then the call fails. Inspecting the signature and choosing
        the right form handles both lines without pinning either.

        The httpx client we build here is entered into the same exit stack as
        the session, so it closes with the turn rather than leaking a connection
        pool per message.
        """
        import inspect

        headers = auth_headers()

        try:
            params = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            params = {}

        if "headers" in params:
            return factory(self.infoblox_url, headers=headers)

        if "http_client" in params:
            client = await self._exit_stack.enter_async_context(
                _http_client(headers)
            )
            return factory(self.infoblox_url, http_client=client)

        # Neither — call it bare and let the connection attempt report why.
        # Unauthenticated, so this will almost certainly be refused, but a 401
        # is a far more useful message than a TypeError about kwargs.
        return factory(self.infoblox_url)

    async def _connect_aws(self):
        """
        The AWS MCP server, over stdio.

        Run through `uvx` so there is nothing to install ahead of time and
        nothing to host. It inherits this process's environment, which is how it
        finds the sandbox AWS credentials — we never hand it a key.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=cfg.AWS_MCP_COMMAND,
            args=cfg.AWS_MCP_ARGS,
            env={
                **os.environ,
                "AWS_REGION": cfg.VPC_REGION,
                "AWS_DEFAULT_REGION": cfg.VPC_REGION,
                # The AWS API MCP server refuses to run mutating calls unless
                # this is set. Part 3 is a deployment exercise, so it must be —
                # and saying so here, in one place, is better than a participant
                # discovering it as a confusing refusal mid-conversation.
                "AWS_API_MCP_WORKING_DIR": "/opt/lab/aws-mcp",
                "READ_OPERATIONS_ONLY": "false",
            },
        )
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(params)
        )
        session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await session.initialize()
        self._sessions["aws"] = session

    # ----------------------------------------------------------------- tools -

    @property
    def server_ids(self):
        return list(self._sessions)

    async def list_tools(self):
        """
        Every tool across every connected server, as Anthropic tool definitions.

        The shapes line up almost exactly — an MCP tool has name, description
        and inputSchema; a Claude tool wants name, description and input_schema.
        The only real work is the prefix, and stamping the server name into the
        description so Claude's tool choice is informed by which system it is
        about to touch.
        """
        tools = []
        for server_id, session in self._sessions.items():
            result = await session.list_tools()
            for tool in result.tools:
                schema = tool.inputSchema or {"type": "object", "properties": {}}
                description = tool.description or ""
                tools.append({
                    "name": f"{server_id}{PREFIX_SEPARATOR}{tool.name}"[:64],
                    "description": f"[{server_id}] {description}".strip(),
                    "input_schema": schema,
                })
        return tools

    async def call_tool(self, prefixed_name, arguments):
        """
        Execute one tool call and return (text, is_error, server_id).

        Errors are RETURNED rather than raised. Claude handles "that call failed
        because X" far better than the agent loop dying, and a denied write is a
        legitimate, expected outcome in Part 4 — the whole point of that
        exercise is for the participant to watch the refusal happen.
        """
        server_id, _, tool_name = prefixed_name.partition(PREFIX_SEPARATOR)
        session = self._sessions.get(server_id)

        if session is None:
            return (f"No MCP server named {server_id!r} is connected.",
                    True, server_id)

        try:
            result = await session.call_tool(tool_name, arguments or {})
        except Exception as exc:                        # noqa: BLE001
            return f"Tool call failed: {exc}", True, server_id

        chunks = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            chunks.append(text if text is not None else str(block))

        return ("\n".join(chunks) or "(no content returned)",
                bool(getattr(result, "isError", False)),
                server_id)
