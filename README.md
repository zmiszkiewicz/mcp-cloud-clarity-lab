# mcp-cloud-clarity-lab

Automation for the Instruqt track **Infoblox MCP for Multi-Cloud DDI**. Cloned
at track start by `track_scripts/setup-shell`; nothing here is run by hand
during a lab.

It lives in its own repo, separate from the track directory, so a fix to a
Python script needs only a `git push` — not an `instruqt track push`. A fix to
an assignment or a check needs the track push. Usually you need both.

```
scripts/
  lab_config.py         every env var, the topology, the CSP path registry
  csp_client.py         auth, retries, paging, fail-message helpers
  cloud_vpc.py          the AWS side: terraform outputs, tunnel state, dig-over-SSM
  baseline.py           the healthy TechCorp footprint
  breaks.py             2 breaks, each with apply / assert / fix
  seed_lab.py           THE entry point: baseline -> breaks -> assert
  verify_lab.py         --stage c1..c4, end-state checks only
  provision_mcp_keys.py service users + read-only and read/write keys
  revoke_mcp_keys.py    idempotent key revoke + service user delete
  teardown_lab.py       remove seeded objects; safe to run twice
  preflight.sh          offline checks; run before every git push
  pick_bedrock_model.py asks Bedrock which Claude models this account can invoke
  mcp_probe.py          what the mcp package exposes, and does the server answer
  traffic/README.md     iq-insighter wiring (TODO-19)
  ...vendored from iracic82, unchanged:
     allocation_subtenant.py  deallocation_subtenant.py  cleanup_broker_allocation.py
     sandbox_api.py  create_sandbox.py  delete_sandbox.py
     user_provision.py  user_cleanup.py  create_user.py  delete_user.py
     deploy_api_key.py

agent/                  Claude on Amazon Bedrock, driving two MCP servers
  app.py                Streamlit chat tab (port 8501)
  bedrock_agent.py      the tool loop
  mcp_client.py         McpFleet — Infoblox over HTTP, AWS over stdio
  requirements.txt

terraform/              the Part 3 AWS VPC, test VM and delivery scaffolding
```

## Maintainer commands

```bash
cd scripts

./preflight.sh                                  # offline; run before every push

python3 seed_lab.py                             # baseline + both breaks + assert
python3 seed_lab.py --baseline-only             # healthy footprint, no breaks
python3 seed_lab.py --break dhcp_range_overlap  # one break on its own
python3 seed_lab.py --assert-only               # verify, change nothing
python3 seed_lab.py --fix zone_missing_auth_server
python3 seed_lab.py --list                      # the break registry

LAB_AUTH_MODE=nsg  python3 seed_lab.py          # force the host-free variant
LAB_AUTH_MODE=host python3 seed_lab.py          # refuse to run without a host

python3 verify_lab.py --stage all               # every check, smoke test

python3 pick_bedrock_model.py --list            # every invokable Claude model
python3 mcp_probe.py                            # transport + live connection test

python3 teardown_lab.py --dry-run               # list what would be deleted
python3 teardown_lab.py --reset                 # teardown, then re-seed clean
```

## Environment variables

### Instruqt secrets — set in `config.yml` and the Instruqt UI

| Variable | Required | Purpose |
|---|---|---|
| `INFOBLOX_EMAIL` | yes | CSP admin login. Signs in, then switches into the participant's sandbox. |
| `INFOBLOX_PASSWORD` | yes | Password for the above. |
| `BROKER_API_TOKEN` | yes | Sandbox broker auth for allocation/deallocation. |
| `Infoblox_Token` | no | Legacy direct sandbox create/delete. Only if the broker is bypassed. |
| `MCP_RO_GROUPS` | no | Comma-separated groups for the read-only MCP user. Default `user,ib-mcp-server-user,ib-ddi-user`. |
| `MCP_RW_GROUPS` | no | Comma-separated groups for the read/write MCP user. Default `user,ib-mcp-server-admin,ib-ddi-admin`. |
| `MCP_RO_GROUP` / `MCP_RW_GROUP` | no | Single-name overrides replacing just the `ib-mcp-server-*` entry. |
| `LAB_DC_HOST` | optional | Universal DDI host name, used only when `LAB_AUTH_MODE` resolves to `host`. |
| `LAB_AUTH_MODE` | optional | `auto` (default), `host`, or `nsg`. See "What is authoritative for the zone" below. |

### Supplied by the Instruqt platform

| Variable | Purpose |
|---|---|
| `INSTRUQT_PARTICIPANT_ID` | Unique per participant. Names the sandbox, the Portal user, the MCP service users and every AWS resource. |
| `INSTRUQT_TRACK_SLUG` | Lab identifier passed to the broker; also the default lab tag value. |
| `INSTRUQT_USER_EMAIL` | Participant's email. Absent for anonymous event participants — `setup-shell` falls back to `guest@instruqt.com`. |
| `INSTRUQT_AWS_ACCOUNT_INFOBLOX_DEMO_AWS_ACCESS_KEY_ID` | Bedrock, the AWS MCP server, and Terraform. Written **only** to `/root/.aws/credentials` (mode 0600) — never to `.bashrc`, never to an agent variable, since agent variables render into assignment markdown. |
| `INSTRUQT_AWS_ACCOUNT_INFOBLOX_DEMO_AWS_SECRET_ACCESS_KEY` | As above. |

### Lab configuration

| Variable | Default | Purpose |
|---|---|---|
| `CSP_URL` | `csp.infoblox.com` | CSP host, without scheme. |
| `MCP_SERVER_URL` | `https://csp.infoblox.com/mcp` | Hosted Infoblox MCP Server endpoint. |
| `BROKER_API_URL` | `https://api-sandbox-broker.highvelocitynetworking.com/v1` | Broker endpoint. |
| `SANDBOX_NAME_PREFIX` | `lab` | Restricts which sandboxes the broker may allocate. |
| `USER_DOMAIN` | `infoblox.lab` | Domain for generated user emails. |
| `LAB_REPO_URL` | `https://github.com/zmiszkiewicz/mcp-cloud-clarity-lab.git` | Repo `setup-shell` clones. |
| `LAB_REPO_REF` | `main` | Branch to clone. Set this to test a branch without editing `setup-shell`. |
| `LAB_DIR` | `/root/infoblox-lab/mcp-cloud-clarity-lab` | Clone destination. |
| `BEDROCK_MODEL_ID` | *discovered* | Set by `pick_bedrock_model.py` at setup. Setting it explicitly skips discovery. |
| `BEDROCK_MODEL_PREFERENCE` | `sonnet` | Family discovery prefers, most preferred first. |
| `BEDROCK_FALLBACK_MODEL_ID` | `anthropic.claude-sonnet-5` | Used only if discovery cannot run at all. |
| `MCP_TRANSPORT` | *unset* | Pin `streamable-http` or `sse` once `mcp_probe.py` has told you which. Unset negotiates. |
| `BEDROCK_REGION` | `us-east-1` | Bedrock **and** VPC region. Must be in `config.yml` and have model access granted. |
| `AGENT_KEY_FILE` | `/opt/lab/mcp_key` | The key the assistant re-reads every turn. Rewriting it swaps its permissions live. |
| `AGENT_PORT` | `8501` | Port the chat tab serves on; must match `config.yml` and the `service` tabs. |
| `AWS_MCP_ENABLED` | `1` | Set `0` to run Infoblox-only, e.g. when debugging Parts 1/2/4. |
| `AWS_MCP_ARGS` | `awslabs.aws-api-mcp-server@latest` | The stdio AWS MCP server `uvx` launches. |
| `LAB_ANSWER_FILE` | `/opt/lab/answer_c1.txt` | Where `lab-answer` records the Part 1 count. |
| `LAB_MIN_SERVICES` | `10` | Plausibility floor for that count. See TODO-33. |
| `LAB_DC_RESOLVER` | *unset* | Resolver the Part 2 `dig` probe queries. **TODO-22** — the probe is skipped while unset. |

### Topology — change these and you must change the matching `assignment.md` prose

`preflight.sh` enforces this: it fails if a value the config defines never
appears in any assignment.

| Variable | Default | Flow-doc placeholder |
|---|---|---|
| `LAB_DNS_VIEW` | `techcorp-view` | |
| `LAB_ZONE_FQDN` | `svc.techcorp.internal.` | `[ZONE NAME]` |
| `LAB_APP_LABEL` | `payments` | (gives `[RECORD]` `payments.svc.techcorp.internal`) |
| `LAB_AUTH_MODE` | `auto` | picks `host` or `nsg` — see below |
| `LAB_DC_HOST` | `niosx-dc-01` | `[DNS SERVER]`, in `host` mode |
| `LAB_DNS_SERVER_GROUP` | `techcorp-dc-servers` | `[DNS SERVER]`, in `nsg` mode |
| `MCP_KEY_TTL_HOURS` | `24` | lifetime of a minted Service API key |
| `LAB_IP_SPACE` | `techcorp-ipam` | |
| `LAB_ADDRESS_BLOCK_ADDR` / `_CIDR` | `10.30.0.0` / `16` | |
| DC-01 subnet | `10.30.1.0/24` | `[NETWORK]` |
| Branch-02 subnet | `10.30.2.0/24` | `[SUBNET]` |
| `LAB_BRANCH_RANGE_START` / `_END` | `10.30.2.100` / `10.30.2.200` (healthy) | |
| `LAB_BROKEN_RANGE_START` / `_END` | `10.30.2.10` / `10.30.2.30` (broken) | |
| `LAB_RESERVED_START` / `_END` | `10.30.2.10` / `10.30.2.30` | |
| `LAB_VPC_NAME` | `techcorp-ai-vpc` | `[VPC NAME / ID]` |
| `LAB_VPC_CIDR` | `10.40.0.0/16` | |
| `LAB_TEST_VM_NAME` | `techcorp-ai-test` | |
| `LAB_DNS_SERVICE_IP` | *unset, discovered* | `[DNS SERVICE IP]` |
| `LAB_C3_MODE` | `as-a-service` | see below |
| `LAB_TAG_VALUE` | `$INSTRUQT_TRACK_SLUG` | tags every seeded object so teardown finds only its own |

**No credential or tenant identifier is hardcoded anywhere.** Everything above
is read from the environment.

## Part 3 delivery modes

`LAB_C3_MODE` selects how DNS gets into the VPC. It must match `c3_mode` in
Terraform (`setup-shell` passes it through as `TF_VAR_c3_mode`, so they cannot
drift at runtime).

### `as-a-service` — the designed path, and the default

The participant creates a **NIOS-X as a Service** Service Deployment with an
Access Location representing the VPC, then brings up a Site-to-Site VPN from the
VPC to the Infoblox point of presence.

Terraform pre-stages the Virtual Private Gateway, because attaching one takes
several minutes and teaches nothing. It deliberately does **not** create the VPN
connection: the customer gateway needs the Cloud Service IPs that only exist
once the Infoblox side is built, and building it is the point of the challenge.

**Risks, both real:**

- The manual version of this flow in the Threat Defense track runs to roughly
  twenty-five form fields across two tunnels, with BGP ASNs and pre-shared keys.
  The assistant collapses that, which is exactly the demo — but it is a lot of
  live API work inside a fifteen-minute part, and BGP convergence adds minutes
  that are not in anyone's control.
- The Service Deployment REST surface is unconfirmed (TODO-31/32).

### `forwarder` — the fallback

A Route 53 Resolver outbound endpoint in the VPC forwards
`svc.techcorp.internal` to a Universal DDI host. No IPsec, no BGP, and every API
call involved is confirmed and scriptable today.

The conversation the participant has is nearly identical — "give this VPC DNS,
with Universal DDI as the source of truth" — and the same `lab-dig` proves the
same outcome. What is lost is the NIOS-X as a Service story specifically.

**Switch to it if the tunnel path proves too slow in rehearsal:**

```bash
instruqt secrets create --name LAB_C3_MODE --value forwarder
```

Then rewrite `03/assignment.md` step 1's prompt to name a resolver endpoint and
forwarding rule instead of a Service Deployment. Nothing else changes: the
check, `cloud_vpc.py` and Terraform all already branch on the mode.

## Group membership and the host name

Two things this lab needs from a broker-allocated sandbox that it does not
create itself.

### The MCP users get **three** groups each, not one

| Role | Groups |
|---|---|
| read-only | `user`, `ib-mcp-server-user`, `ib-ddi-user` |
| read/write | `user`, `ib-mcp-server-admin`, `ib-ddi-admin` |

The reason for three is worth remembering: **an `ib-mcp-server-*` role gates
access to the MCP Server, but grants no permission to read DNS, DHCP or IPAM
data.** A user holding only that connects successfully and then has every tool
call come back empty — which reads as a broken lab rather than as a permissions
problem. The matching `ib-ddi-*` role is what makes the data visible.

`act_admin` is deliberately excluded. An account-admin "read-only" user would
make Part 4's RBAC exercise meaningless, and `check_c4` probes for exactly that
and fails the lab loudly if it finds it.

### What is authoritative for the zone

**A broker-allocated sandbox ships with no Universal DDI host.** Confirmed on a
live run: `/dns/host`, `/infra/hosts` and `/infra/services` all come back empty.
Part 2's whole fault is "the DNS server is not on the zone's Authoritative DNS
Servers list", which needs *something* to be that server.

`LAB_AUTH_MODE` picks what:

| Mode | Object | Zone field | Needs a host? |
|---|---|---|---|
| `host` | a Universal DDI host | `internal_secondaries` | yes |
| `nsg` | a DNS Server Group | `nsgs` | no — an AuthNSG needs only a name |
| `auto` | host if one exists, else server group | either | no |

Both fields are what the Portal renders under "Authoritative DNS Servers" on a
zone's edit page, so the participant's experience is nearly identical and
`authoritative_server_ids()` reads both regardless of mode — someone who fixes
it in the Portal gets credit for whichever kind the UI offered them.

**What `nsg` mode costs.** Nothing actually serves the zone, so there is no live
NXDOMAIN to `dig` for. The fault is a real, Portal-visible configuration error
and the diagnosis conversation is the same, but the participant is reasoning
about configuration rather than observing a symptom. `02/assignment.md` says so
plainly rather than promising a `dig` result that will not come.

Switch to `host` mode the moment the sandbox image ships a registered host —
that is the higher-fidelity lab and it is one env var away.

**The assignment does not hardcode either name.** `01/setup-shell` publishes
whatever was resolved as the `DNS_SERVER_NAME` agent variable and
`02/assignment.md` renders it, so the prose always names an object the
participant can actually find.

## Design notes

**Checks read the API, never the transcript.** The assistant's wording is
nondeterministic — the same correct fix can be described five different ways,
and a check that greps for phrasing fails a participant who did everything
right. Every assertion asks whether the world is in the required state,
regardless of how they got it there. Someone who fixes the zone in the Portal
rather than through the assistant passes, which is correct: the part is about
the outcome.

**Part 3's load-bearing check is a `dig`, not an object lookup.** A Service
Deployment that exists but has no working path into the VPC passes an
existence check and fails a query. The query is what the cloud team cares about,
so it is what decides the challenge — and the participant runs the identical
probe themselves through `lab-dig`, so there is never a "it works for the check
but not for me".

**Each break is `apply` + `assert` + `fix`, together in one registry.** `apply`
without `assert` is worse than no break at all — the participant spends fifteen
minutes hunting a fault that is not there. `fix` lives next to them so the
remediation cannot drift when the seed changes; `solve-shell` and
`seed_lab.py --fix` both use it.

**The `Todo` sentinel is not a string.** Anything that tries to build a request
from an unconfirmed path raises `LabTodo` with the question that needs
answering, at the point of use. There is no path by which this scaffolding
silently calls an invented endpoint.

**Everything seeded is tagged `{instruqt-lab: <track slug>}`,** on both sides.
`teardown_lab.py` deletes only tagged CSP objects in reverse-dependency order;
Terraform's `default_tags` stamps every AWS resource the same way, so an orphan
sweep has one filter to use. The Universal DDI host is never deleted — the lab
does not create it.

## Known pitfall: `source /root/.bashrc` moves the working directory

Instruqt's `set-workdir` writes a `cd` into `/root/.bashrc`. Every later
`source /root/.bashrc` therefore silently relocates the shell, and any relative
path after it stops resolving.

This cost the sibling track two failed starts, and the first was
near-undiagnosable: **python exits 2 and prints nothing at all** when it cannot
open a script file, so the track log showed only `+ python3 user_provision.py`
followed by `exit status 2` — no traceback, no message.

Two rules are enforced across every lifecycle script here:

- Every `python3` call uses an absolute `${SCRIPTS}/<name>.py`, and every file
  test uses an absolute path.
- `set-workdir` is called **last**, after every step that depends on the
  working directory.

A related trap in the same family: `rc=$?` inside `if ! cmd; then` captures the
status of the negation, not the command — it is always 0, which produces a
cheerful "failed (exit 0)". Use `cmd; rc=$?` instead.
