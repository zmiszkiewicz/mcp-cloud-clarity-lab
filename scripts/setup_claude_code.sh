#!/usr/bin/env bash
#
# Install and configure Claude Code as the lab's assistant.
#
# IDEMPOTENT AND RE-RUNNABLE. 01/setup-shell calls it to stand the assistant up;
# 02 and 04 call it again to swap which Infoblox key it holds. Every step
# converges rather than accumulating.
#
# WHY CLAUDE CODE RATHER THAN A CUSTOM AGENT
# ------------------------------------------
# This track used to ship its own Streamlit chat app driving the MCP protocol
# through the `mcp` Python package. That meant owning an MCP client, and the
# client library moved underneath it repeatedly — a renamed factory, a changed
# signature, a swapped HTTP library. Four separate track starts died on it,
# none of them for a reason a participant would have learned anything from.
#
# Claude Code is the vendor-documented path (see the Infoblox "Connect Claude
# Code to the Infoblox MCP Server" guide) and owns the MCP client itself. It
# also gives the lab three things the custom agent had to fake: a real `/mcp`
# connection view, real per-tool approval prompts, and a real conversation
# transcript.
#
# AUTHENTICATION
# --------------
# No Anthropic API key and no browser login. Claude Code runs against Amazon
# Bedrock in the Instruqt sandbox account, resolving AWS credentials through the
# standard chain — which track_scripts/setup-shell has already written to
# /root/.aws/credentials.
#
# Usage:
#   setup_claude_code.sh                 install if needed, configure everything
#   setup_claude_code.sh --keys-only     just re-register the MCP servers

set -uo pipefail

KEYS_ONLY=0
[ "${1:-}" = "--keys-only" ] && KEYS_ONLY=1

LAB_DIR="${LAB_DIR:-/root/infoblox-lab/mcp-cloud-clarity-lab}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-/root/techcorp}"
KEY_FILE="${AGENT_KEY_FILE:-/opt/lab/mcp_key}"
MCP_URL="${MCP_SERVER_URL:-https://csp.infoblox.com/mcp}"
# Bedrock's region, which is NOT the VPC's. The Claude models are enabled in
# us-east-1; the lab VPC lives wherever the NIOS-X AMI exists, currently
# eu-central-1. Reading AWS_DEFAULT_REGION here would point Bedrock at a region
# with no model access and fail every prompt.
REGION="${BEDROCK_REGION:-us-east-1}"
# DISCOVERED, not hardcoded — and the region can change as a result.
#
# A cross-region inference profile is geography-scoped: `us.anthropic.…` only
# resolves in a US region and `eu.anthropic.…` only in an EU one. Pinning a
# `us.` id and then moving the lab to eu-central-1 produced
#
#     400 The provided model identifier is invalid.
#
# on every prompt, which names neither the region nor the geography and reads
# like the model was withdrawn. pick_bedrock_model.py asks Bedrock what this
# account can actually invoke in this region, prefixes correctly, and falls
# back to another region if there is nothing here — so the answer is right
# wherever the lab runs.
#
# ANTHROPIC_MODEL set in the environment still wins, as the manual override.
if [ -n "${ANTHROPIC_MODEL:-}" ]; then
  MODEL="${ANTHROPIC_MODEL}"
  echo "   model pinned by ANTHROPIC_MODEL: ${MODEL}"
else
  # ONE invocation. stdout is "<model id> <region>", stderr is the reasoning —
  # which is worth keeping, so it goes to a file and then into this log rather
  # than being thrown away or, worse, earning a second call to Bedrock.
  PICK_ERR="$(mktemp)"
  PICK_OUT="$(BEDROCK_REGION="${REGION}" \
    python3 "${LAB_DIR}/scripts/pick_bedrock_model.py" 2>"${PICK_ERR}" || true)"
  [ -s "${PICK_ERR}" ] && cat "${PICK_ERR}"
  rm -f "${PICK_ERR}"

  if [ -n "${PICK_OUT}" ]; then
    MODEL="${PICK_OUT%% *}"
    PICKED_REGION="${PICK_OUT##* }"
    # The picker may move Bedrock to another region when this one has no
    # model. Honour that, or the id and the endpoint disagree and every call
    # fails with the same unhelpful 400 this was meant to fix.
    if [ -n "${PICKED_REGION}" ] && [ "${PICKED_REGION}" != "${REGION}" ]; then
      echo "   Bedrock moves to ${PICKED_REGION}: no usable model in ${REGION}"
      REGION="${PICKED_REGION}"
    fi
  else
    # Discovery failed outright — boto3 missing, no credentials yet. Derive the
    # prefix from the region rather than defaulting to a `us.` id that is
    # guaranteed wrong outside the US.
    case "${REGION}" in
      eu-*) MODEL="eu.anthropic.claude-sonnet-4-6" ;;
      ap-*) MODEL="apac.anthropic.claude-sonnet-4-6" ;;
      *)    MODEL="us.anthropic.claude-sonnet-4-6" ;;
    esac
    echo "⚠️  could not query Bedrock; using ${MODEL} unverified"
  fi
fi
# Where the lab's VPC actually is, which is a different region from Bedrock's.
# The AWS MCP server acts on that VPC, so pointing it at REGION would have it
# looking for the VPC in the region where the models live and finding nothing.
VPC_REGION="${LAB_VPC_REGION:-${AWS_DEFAULT_REGION:-eu-central-1}}"

export PATH="/root/.local/bin:${PATH}"

# --------------------------------------------------------------------------- #
# 1. Install
#
# The native installer needs no Node and drops a binary at ~/.local/bin/claude.
# --------------------------------------------------------------------------- #
if [ "${KEYS_ONLY}" -eq 0 ]; then
  if ! command -v claude >/dev/null 2>&1; then
    echo "=== Installing Claude Code ==="
    curl -fsSL https://claude.ai/install.sh | bash || {
      echo "❌ Claude Code install failed."
      echo "   Retry by hand: curl -fsSL https://claude.ai/install.sh | bash"
      exit 1
    }
  fi

  command -v claude >/dev/null 2>&1 || {
    echo "❌ claude is not on PATH after install. Looked in /root/.local/bin."
    exit 1
  }
  echo "✅ $(claude --version 2>&1 | head -1) at $(command -v claude)"

  # ------------------------------------------------------------------------ #
  # MAKE IT REACHABLE FROM THE PARTICIPANT'S SHELL.
  #
  # The PATH export at the top of this script is scoped to this script. The
  # installer puts the binary in ~/.local/bin, which is NOT on the default PATH
  # in this container — so setup succeeded, reported success, and the
  # participant still got `bash: claude: command not found`. Setup passing and
  # the lab working were two different things.
  #
  # Three remedies, because interactive and login shells read different files
  # and it is not worth betting on which one a terminal tab gives you:
  #
  #   /usr/local/bin symlink   already on PATH for every shell. The reliable one.
  #   /etc/profile.d           login shells (bash -l), which do NOT read .bashrc
  #   /root/.bashrc            interactive non-login shells, which do
  # ------------------------------------------------------------------------ #
  ln -sf "$(command -v claude)" /usr/local/bin/claude

  cat > /etc/profile.d/claude-code.sh <<'PROFILE'
# Claude Code installs to ~/.local/bin, which is not on this container's
# default PATH.
case ":$PATH:" in
  *":/root/.local/bin:"*) ;;
  *) export PATH="/root/.local/bin:$PATH" ;;
esac
PROFILE
  chmod 0644 /etc/profile.d/claude-code.sh

  grep -q '/root/.local/bin' /root/.bashrc 2>/dev/null || \
    echo 'export PATH="/root/.local/bin:$PATH"' >> /root/.bashrc

  # Verify the way the PARTICIPANT will experience it, not the way this script
  # does. Setup previously passed while the Assistant tab said "command not
  # found", because this script had the binary on its own PATH and never
  # checked anyone else's.
  interactive_ok=0; login_ok=0
  bash -ic  'command -v claude' >/dev/null 2>&1 && interactive_ok=1
  bash -lic 'command -v claude' >/dev/null 2>&1 && login_ok=1
  echo "   reachable from an interactive shell: ${interactive_ok}"
  echo "   reachable from a login shell:        ${login_ok}"

  if [ "${interactive_ok}" -eq 0 ] && [ "${login_ok}" -eq 0 ]; then
    echo "❌ claude is installed at $(command -v claude) but no shell the"
    echo "   participant opens can find it. The Assistant tab would report"
    echo "   'command not found'."
    echo "   /usr/local/bin/claude: $(ls -l /usr/local/bin/claude 2>&1)"
    exit 1
  fi
fi

# --------------------------------------------------------------------------- #
# 2. Settings — Bedrock, and the model pin
#
# Written to the settings file rather than exported, so the values survive a
# shell the participant opens themselves and do not leak into every child
# process.
#
# PINNING THE MODEL IS NOT OPTIONAL. Unpinned, Claude Code on Bedrock defaults
# its primary model to Opus 5 and its `sonnet` alias to Sonnet 4.5 — so an
# unpinned lab is both a different model than intended and billed at the Opus
# rate. The `us.` prefix is the cross-region inference profile; a bare
# `anthropic.…` id fails with an on-demand-throughput error that never mentions
# inference profiles.
# --------------------------------------------------------------------------- #
if [ "${KEYS_ONLY}" -eq 0 ]; then
  mkdir -p /root/.claude "${PROJECT_DIR}"

  cat > /root/.claude/settings.json <<SETTINGS
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_REGION": "${REGION}",
    "ANTHROPIC_MODEL": "${MODEL}",
    "DISABLE_AUTOUPDATER": "1"
  },
  "includeCoAuthoredBy": false
}
SETTINGS
  echo "✅ Bedrock configured: ${MODEL} in ${REGION}"

  # A working directory of its own, so Claude Code has a project to sit in and
  # the participant is not running an agent from inside the lab's own scripts.
  #
  # Written TWICE, deliberately. The project copy is what a participant would
  # expect; the user-level copy at ~/.claude/CLAUDE.md applies from any
  # directory, so the assistant still behaves correctly if someone starts it
  # from the wrong tab. Project context is a nice-to-have; the write-safety
  # instruction is not.
  cat > "${PROJECT_DIR}/CLAUDE.md" <<'PROJECT'
# TechCorp network operations

You are assisting the on-call network engineer at TechCorp.

Infoblox Universal DDI is the authoritative source for DNS, DHCP and IP address
data. Ground every factual claim about this environment in an MCP tool call
rather than in general knowledge about how DNS usually works. If a call returns
nothing useful, say so plainly instead of filling the gap with a plausible
guess.

THE INFOBLOX MCP SERVER IS READ-ONLY IN THIS BUILD. Write verbs (post, patch,
put, delete) are refused, so you cannot change Infoblox configuration yourself.
Do not attempt a write to find out, and never describe a change as though you
had made it.

What you do instead is more useful: draft a change the engineer can apply. When
you identify something that needs changing, state precisely —

  * the object, by name and by resource id
  * the field
  * its current value
  * the value it should have
  * where in the Infoblox Portal to make the change

— then say plainly that they must apply it, because you cannot. When they tell
you it is done, VERIFY it: re-read the object and report what you now see,
rather than taking their word for it. That verification is the part they cannot
easily do for themselves, and it is worth doing carefully.

The AWS MCP server DOES accept writes. For anything on the AWS side, state what
you intend to change and wait for approval before executing it. There is no
dry-run; it takes effect immediately against a real account.

CLOUD DNS DEPLOYMENT. A NIOS-X HOST already exists and is already serving DNS.
It is an EC2 instance inside the lab's own VPC, it registered itself with a
join token at track start, and its DNS service was confirmed answering on port
53 before the lab started. Read it before proposing anything, and do not offer
to create it.

It is a HOST, not a NIOS-X as a Service endpoint. There is no point of
presence, no IPsec tunnel, no customer gateway, no Site-to-Site VPN and no
Access Location anywhere in this design. A sandbox tenant has no PoP
entitlement, so as-a-Service cannot work here at all. If you find yourself
proposing a VPN to reach the DNS service, stop: the service is already inside
the VPC, a few addresses away from the workloads that need it.

The work that remains is on the AWS side: the VPC still resolves through
AmazonProvidedDNS, so it needs a DHCP options set pointing at the NIOS-X
host's private address. Read that address from Infoblox rather than assuming
it.

A DHCP OPTIONS SET DOES NOT MOVE A RUNNING INSTANCE. This is the part that
looks finished and is not. An instance picks up DHCP options when it takes or
renews a lease, so attaching a new set to the VPC leaves every already-running
instance on the resolver it booted with, for as long as its current lease
lasts. The test VM will keep asking AmazonProvidedDNS and keep returning
NXDOMAIN while the VPC configuration is completely correct.

So after you attach the options set, the job is not done: the VM has to renew
its lease. Tell the engineer to run `lab-renew-dns`, which handles it and
reports what the resolver actually became. You can also reboot the instance
yourself through the AWS MCP server — propose it, get approval, and wait for
it to come back. A reboot keeps the instance's public address.

Do not suggest `dhclient -r` on this VM. Amazon Linux 2023 has no dhclient,
so it fails with "command not found". Do not suggest `nmcli device reapply`
either: it re-applies the profile already in memory and never asks the server
for a new lease. In practice a reboot is what works here, because forcing a
real re-request means bouncing the interface and every way of doing that
arrives over SSH on that same interface and kills its own connection.

Do not tell the engineer that instances "will now use" the new resolver when
all you have done is attach the options set. That claim is only true of
instances that start or renew afterwards. Say what you changed, say what still
has to happen, and verify by resolving a name rather than by re-reading the
configuration you just wrote.

When diagnosing, distinguish what is CONFIGURED from what is actually HAPPENING.
Most real faults live in the gap between the two: a zone that exists is not a
zone that is being served, and a DHCP range that exists is not a range that can
issue a lease.

When a call is refused, say so directly and explain what was refused and why.
Do not retry it or work around it — a refusal is information, not an obstacle.
PROJECT
  cp "${PROJECT_DIR}/CLAUDE.md" /root/.claude/CLAUDE.md
fi

# --------------------------------------------------------------------------- #
# 3. Register the MCP servers
#
# `--scope user` writes ~/.claude.json, which avoids the trust prompt that a
# project-scoped .mcp.json triggers on first use. Re-registering means removing
# first: `claude mcp add` refuses to overwrite an existing name.
#
# This block is what the --keys-only path re-runs. The Infoblox header carries
# whichever key is in /opt/lab/mcp_key right now, so rewriting that file and
# calling this again is how Part 2 hands over write access.
# --------------------------------------------------------------------------- #
[ -s "${KEY_FILE}" ] || {
  echo "❌ No Infoblox Service API key at ${KEY_FILE}."
  exit 1
}
API_KEY="$(cat "${KEY_FILE}")"

cd "${PROJECT_DIR}" || exit 1

claude mcp remove infoblox-mcp --scope user >/dev/null 2>&1 || true
if claude mcp add --transport http infoblox-mcp "${MCP_URL}" \
     --header "Authorization: Token ${API_KEY}" \
     --scope user >/dev/null 2>&1; then
  echo "✅ infoblox-mcp registered (key …${API_KEY: -6})"
else
  echo "❌ could not register infoblox-mcp"
  exit 1
fi

# The AWS side of Part 3. stdio through uvx, inheriting this process's AWS
# credentials. Non-fatal: Parts 1, 2 and 4 do not need it.
if [ "${AWS_MCP_ENABLED:-1}" != "0" ]; then
  mkdir -p /opt/lab/aws-mcp
  claude mcp remove aws-api --scope user >/dev/null 2>&1 || true
  if claude mcp add --transport stdio aws-api \
       --scope user \
       --env AWS_REGION="${VPC_REGION}" \
       --env AWS_API_MCP_WORKING_DIR=/opt/lab/aws-mcp \
       --env READ_OPERATIONS_ONLY=false \
       -- uvx awslabs.aws-api-mcp-server@latest >/dev/null 2>&1; then
    echo "✅ aws-api registered (acting in ${VPC_REGION})"
  else
    echo "⚠️  could not register the AWS MCP server; Part 3 will not be able to"
    echo "    act on AWS. Parts 1, 2 and 4 are unaffected."
  fi
fi

# --------------------------------------------------------------------------- #
# 4. Report
#
# `claude mcp list` attempts a real connection to each server, so this is the
# genuine answer to "is the Infoblox MCP Server reachable with this key" — the
# question several earlier builds could only guess at.
# --------------------------------------------------------------------------- #
echo "--- claude mcp list ---"
claude mcp list 2>&1 || echo "⚠️  claude mcp list failed"

exit 0
