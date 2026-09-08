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
        TODO-27 — transport. This uses Streamable HTTP, the current MCP standard
        transport and what a hosted server at an /mcp path almost certainly
        speaks. If the Infoblox MCP Server turns out to expose HTTP+SSE instead,
        swap `streamablehttp_client` for `sse_client` from `mcp.client.sse`;
        nothing else in this class is transport-aware. Confirm against the MCP
        Setup Guide before the event.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        try:
            read, write, _ = await self._exit_stack.enter_async_context(
                streamablehttp_client(self.infoblox_url, headers=auth_headers())
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
        except Exception as exc:                        # noqa: BLE001
            raise McpUnavailable(
                f"Could not connect to the Infoblox MCP Server at "
                f"{self.infoblox_url}. Access is gated by role-based access "
                f"control — if the service user behind your key has no MCP "
                f"Server role, the connection is refused outright rather than "
                f"degraded. ({exc})"
            ) from exc

        self._sessions["infoblox"] = session

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
