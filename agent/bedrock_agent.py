#!/usr/bin/env python3
"""
The agent loop: Claude on Amazon Bedrock, driving the Infoblox and AWS MCP servers.

    participant prompt
        -> Claude (Bedrock)
        -> tool_use blocks
        -> executed against Infoblox or AWS, whichever the tool belongs to
        -> tool_result blocks back to Claude
        -> repeat until Claude stops calling tools
        -> answer

A manual loop rather than the SDK's tool runner, for two specific reasons: the
runner is beta and this runs on Bedrock, and we want each tool call surfaced in
the UI as it happens so the participant can see which call produced which part
of the answer. That visibility is a teaching goal, not a debug aid — Part 3's
whole argument is "one sentence, two systems, one coordinated change", and it
only lands if you can watch the calls interleave.

Credentials come from the Instruqt AWS sandbox account (Bedrock, and the AWS MCP
server) and from the participant's Infoblox Service API key (the Infoblox MCP
server). Neither is hardcoded.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import lab_config as cfg  # noqa: E402
from mcp_client import McpFleet, McpUnavailable  # noqa: E402


SYSTEM_PROMPT = f"""\
You are an operations assistant for the network team at TechCorp, connected to \
Infoblox Universal DDI through the Infoblox Model Context Protocol (MCP) Server \
and to Amazon Web Services through the AWS MCP server.

You are talking to a network engineer who is working a real incident and a real \
deployment. Ground every factual claim about their environment in a tool call. \
You have live access to their DNS, DHCP, IPAM and asset data, and to their AWS \
account — so never answer from general knowledge about how DNS usually works \
when you could look up how THEIR DNS is actually configured. If a tool call \
returns nothing useful, say so plainly rather than filling the gap with a \
plausible guess.

Three habits matter more than anything else.

BEFORE MAKING ANY CHANGE, state exactly what you intend to change — which \
objects, which fields, which values, in which system — and wait for the \
engineer to approve it. There is no dry-run mode on either side; write \
operations take effect immediately against a real tenant and a real AWS \
account. Never batch an unrequested change in alongside an approved one. If a \
change spans both Infoblox and AWS, lay out the whole plan in order before you \
execute any of it, so the engineer is approving a sequence rather than a \
surprise.

WHEN DIAGNOSING, distinguish what is CONFIGURED from what is actually \
HAPPENING. Most real faults live in the gap between the two. A zone that exists \
is not a zone that is being served; a DHCP range that exists is not a range \
that can issue a lease.

WHEN A CALL IS REFUSED, say so directly and explain which credential was used \
and what role it would need. Do not retry a denied write, and do not work \
around it. An access denial is information, not an obstacle.

Infoblox Universal DDI is the authoritative source for DNS, DHCP and IP address \
data here. When the cloud provider and Infoblox disagree about a record, \
Infoblox is the source of truth and the cloud is the thing to reconcile.

Be concise and concrete. Prefer a short answer with the tool output that backs \
it to a long explanation.

Reference: the Infoblox MCP Server endpoint is {cfg.MCP_SERVER_URL}. The AWS \
region for this lab is {cfg.VPC_REGION}.\
"""

MAX_ITERATIONS = 16


def bedrock_client():
    """
    Claude on Bedrock, in the Instruqt-provided AWS sandbox account.

    Credentials resolve through the standard boto3 chain — track_scripts/
    setup-shell writes them to /root/.aws/credentials, so nothing is passed in
    here and no key is ever held in this process.
    """
    from anthropic import AnthropicBedrockMantle
    return AnthropicBedrockMantle(aws_region=cfg.AWS_REGION)


async def run_turn(user_message, history, on_event=None):
    """
    Run one full turn: everything from the participant's message to a final
    answer, however many tool calls that takes.

    `history` is the running message list and is mutated in place, so the caller
    keeps the conversation across turns.

    `on_event(kind, payload)` is called as things happen — kind is one of
    "thinking", "warning", "tool_call", "tool_result", "text" — so the UI can
    show tool calls live instead of after the fact.

    Returns the final assistant text.
    """
    def emit(kind, payload):
        if on_event:
            on_event(kind, payload)

    client = bedrock_client()
    history.append({"role": "user", "content": user_message})

    async with McpFleet() as fleet:
        for warning in fleet.warnings:
            emit("warning", warning)

        if fleet.transport:
            emit("transport", fleet.transport)

        tools = await fleet.list_tools()
        emit("thinking",
             f"{len(tools)} tools available from: {', '.join(fleet.server_ids)}")

        final_text = ""
        for _ in range(MAX_ITERATIONS):
            response = client.messages.create(
                model=cfg.BEDROCK_MODEL_ID,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=history,
            )

            # Surface any prose in this turn before running the tool calls, so
            # the participant sees the reasoning ahead of the actions. In Part 3
            # this is where the deployment plan appears, which is the thing they
            # are being asked to review.
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    emit("text", block.text)
                    final_text = block.text

            if response.stop_reason != "tool_use":
                history.append({"role": "assistant", "content": response.content})
                return final_text

            history.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                emit("tool_call", {"name": block.name, "input": block.input})
                content, is_error, server_id = await fleet.call_tool(
                    block.name, block.input
                )
                emit("tool_result", {"name": block.name, "server": server_id,
                                     "content": content, "is_error": is_error})

                # Every tool_use block needs a matching tool_result in ONE user
                # message — splitting them across messages teaches Claude to
                # stop making parallel calls.
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                })

            history.append({"role": "user", "content": tool_results})

        emit("text", "Stopped after too many tool calls without reaching an "
                     "answer. Try narrowing the question.")
        return final_text


def preflight():
    """
    Check the agent before the UI accepts input, so a misconfigured lab says so
    instead of failing on the participant's first message.

    Returns a list of human-readable problems; empty means good to go. The AWS
    MCP server is deliberately not checked here — it is optional, it is slow to
    start, and McpFleet already degrades gracefully without it.
    """
    problems = []

    try:
        from mcp_client import read_key
        read_key()
    except McpUnavailable as exc:
        problems.append(str(exc))

    try:
        bedrock_client()
    except Exception as exc:                            # noqa: BLE001
        problems.append(
            f"Could not create the Bedrock client in {cfg.AWS_REGION}: {exc}"
        )

    return problems
