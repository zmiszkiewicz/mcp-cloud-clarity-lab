#!/usr/bin/env python3
"""
The chat tab the participant works in.

Streamlit, deliberately: the track needs one browser tab with a chat box and a
visible record of which call produced which answer, and that is about eighty
lines of Streamlit versus a frontend build nobody will maintain.

Every tool call is rendered as it happens, collapsed but expandable, tagged with
the server it went to. That is a teaching decision. The argument of the lab is
"the assistant reasons, Infoblox supplies the trusted data" — and in Part 3,
"one sentence coordinates two systems". A participant who can watch an Infoblox
call return the smoking gun, and then watch an AWS call act on it, believes both
of those in a way they will not from prose.

Run:  streamlit run app.py --server.port 8501 --server.address 0.0.0.0
"""

import asyncio
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import lab_config as cfg  # noqa: E402
from bedrock_agent import preflight, run_turn  # noqa: E402
from mcp_client import McpUnavailable  # noqa: E402


st.set_page_config(page_title="TechCorp Operations Assistant",
                   page_icon="🛠️", layout="wide")

SERVER_ICONS = {"infoblox": "🧭", "aws": "☁️"}


# --------------------------------------------------------------------------- #
# Sidebar — what the participant is actually connected to
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.markdown("### Connected servers")
    st.caption("Infoblox MCP Server")
    st.code(cfg.MCP_SERVER_URL, language=None)
    if cfg.AWS_MCP_ENABLED:
        st.caption(f"AWS MCP Server · `{cfg.VPC_REGION}`")
        st.code(" ".join(cfg.AWS_MCP_ARGS), language=None)
    else:
        st.caption("AWS MCP Server: disabled")

    st.markdown("### Model")
    st.code(cfg.BEDROCK_MODEL_ID, language=None)
    st.caption(f"Amazon Bedrock · `{cfg.AWS_REGION}`")

    # Recorded on the first successful turn. Worth surfacing: which transport
    # the hosted server actually speaks was an open question through several
    # builds, and this is where it gets answered.
    if st.session_state.get("transport"):
        st.caption(f"Transport: `{st.session_state['transport']}`")

    # Which Infoblox credential is in force right now. This changes when Part 2
    # hands over the read/write key and changes back at Part 4 — worth showing,
    # because "which credential am I acting as" is the lesson underneath the
    # write-safety story.
    st.markdown("### Infoblox credential")
    try:
        from mcp_client import read_key
        key = read_key()
        st.success(f"Service API key active (…{key[-6:]})")
    except McpUnavailable as exc:
        st.error(str(exc))

    if st.button("Clear conversation"):
        st.session_state.history = []
        st.session_state.transcript = []
        st.rerun()


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

if "history" not in st.session_state:
    st.session_state.history = []       # the message list Claude sees
if "transcript" not in st.session_state:
    st.session_state.transcript = []    # what we render, incl. tool calls

st.title("TechCorp Operations Assistant")
st.caption("Connected to Infoblox Universal DDI through the Infoblox Model "
           "Context Protocol (MCP) Server, and to AWS through the AWS MCP "
           "server.")

problems = preflight()
if problems:
    st.error("The assistant is not ready:\n\n"
             + "\n".join(f"- {p}" for p in problems))
    st.stop()


def render(entry):
    """Render one transcript entry."""
    kind = entry["kind"]

    if kind == "user":
        with st.chat_message("user"):
            st.markdown(entry["text"])

    elif kind == "text":
        with st.chat_message("assistant"):
            st.markdown(entry["text"])

    elif kind == "warning":
        st.warning(entry["text"])

    elif kind == "tool":
        server = entry.get("server", "infoblox")
        icon = "⚠️" if entry.get("is_error") else SERVER_ICONS.get(server, "🔧")
        # Strip the routing prefix for display — the participant cares that the
        # call went to Infoblox, not that the agent namespaces its tool table.
        label = entry["name"].split("__", 1)[-1]
        with st.expander(f"{icon} {server} · `{label}`", expanded=False):
            st.caption("Request")
            st.json(entry.get("input") or {})
            st.caption("Response")
            st.code(str(entry.get("content", ""))[:8000], language=None)


for entry in st.session_state.transcript:
    render(entry)


# --------------------------------------------------------------------------- #
# Turn
# --------------------------------------------------------------------------- #

prompt = st.chat_input("Ask about your DNS, DHCP, IP address management, or "
                       "your cloud environment…")

if prompt:
    st.session_state.transcript.append({"kind": "user", "text": prompt})
    render(st.session_state.transcript[-1])

    live = st.container()
    pending = {}

    def on_event(kind, payload):
        """
        Called from inside the agent loop as things happen.

        Tool calls are rendered the moment they return rather than after the
        turn completes, so a long investigation shows progress instead of a
        spinner. Part 3 can run a dozen calls; a spinner for that long looks
        like a hang.
        """
        if kind == "tool_call":
            pending["name"] = payload["name"]
            pending["input"] = payload["input"]
        elif kind == "tool_result":
            entry = {
                "kind": "tool",
                "name": payload["name"],
                "server": payload.get("server"),
                "input": pending.get("input"),
                "content": payload["content"],
                "is_error": payload["is_error"],
            }
            st.session_state.transcript.append(entry)
            with live:
                render(entry)
        elif kind == "transport":
            st.session_state["transport"] = payload
        elif kind == "warning":
            entry = {"kind": "warning", "text": payload}
            st.session_state.transcript.append(entry)
            with live:
                render(entry)
        elif kind == "text" and payload.strip():
            entry = {"kind": "text", "text": payload}
            st.session_state.transcript.append(entry)
            with live:
                render(entry)

    with st.spinner("Working…"):
        try:
            asyncio.run(run_turn(prompt, st.session_state.history, on_event))
        except McpUnavailable as exc:
            st.error(str(exc))
        except Exception as exc:                        # noqa: BLE001
            st.error(f"The assistant hit an error: {exc}")
