#!/usr/bin/env python3
"""
Single source of truth for the "Infoblox MCP for Multi-Cloud DDI" track.

Four things live here and nowhere else:

  1. Every environment variable the lab reads, with its default.
  2. The deterministic topology — zone names, CIDRs, host names, VPC names.
     The seeding, the break, the checks and the assignment prose all resolve
     from these same constants, so a rename in one place cannot desynchronise
     the rest. This is the "placeholder values are a single source of truth"
     requirement from the track flow, made literal.
  3. The AWS side of Part 3 — VPC name, CIDRs, region, test-VM tag.
  4. The CSP REST path registry, including the paths nobody has confirmed yet.

On (4): paths in production use elsewhere in this estate, or confirmed against
Infoblox's OpenAPI-generated Go client, are plain strings. Paths that still need
answering are `Todo` objects. Asking the registry for one raises `LabTodo` with
the exact question and the punch-list ID from README.md, at the point of use —
so an unfinished lab fails loudly on the right line instead of 404-ing somewhere
confusing.

NOTHING SECRET IS HARDCODED HERE. Credentials and tenant identifiers come from
the environment only.
"""

import os


# --------------------------------------------------------------------------- #
# TODO registry
# --------------------------------------------------------------------------- #

class LabTodo(RuntimeError):
    """Raised when the lab reaches a path or payload that is not confirmed yet."""


class Todo:
    """
    A placeholder standing in for a CSP REST path we have not confirmed.

    Deliberately not a string: anything that tries to use it as one blows up at
    the point of use with the question that needs answering, rather than
    silently building a request against a made-up endpoint.
    """

    def __init__(self, ident, question):
        self.ident = ident
        self.question = question

    def __str__(self):
        raise LabTodo(f"[{self.ident}] {self.question}")

    __repr__ = __str__

    def __fspath__(self):
        raise LabTodo(f"[{self.ident}] {self.question}")


def is_todo(value):
    return isinstance(value, Todo)


# --------------------------------------------------------------------------- #
# Environment — control plane
# --------------------------------------------------------------------------- #

# CSP tenant that owns the sandbox pool. Admin credentials, from Instruqt secrets.
CSP_URL = f"https://{os.environ.get('CSP_URL', 'csp.infoblox.com')}"
INFOBLOX_EMAIL = os.environ.get("INFOBLOX_EMAIL")
INFOBLOX_PASSWORD = os.environ.get("INFOBLOX_PASSWORD")

# Sandbox broker — the estate's current allocation path (allocation_subtenant.py).
BROKER_API_URL = os.environ.get(
    "BROKER_API_URL",
    "https://api-sandbox-broker.highvelocitynetworking.com/v1",
)
BROKER_API_TOKEN = os.environ.get("BROKER_API_TOKEN")
SANDBOX_NAME_PREFIX = os.environ.get("SANDBOX_NAME_PREFIX", "lab")

# Legacy direct-create path (create_sandbox.py / delete_sandbox.py). Only needed
# if the broker is bypassed.
INFOBLOX_TOKEN = os.environ.get("Infoblox_Token")

# Instruqt-supplied, per participant.
PARTICIPANT_ID = os.environ.get("INSTRUQT_PARTICIPANT_ID")
TRACK_SLUG = os.environ.get("INSTRUQT_TRACK_SLUG", "infoblox-mcp-cloud-clarity")
USER_EMAIL = os.environ.get("INSTRUQT_USER_EMAIL") or os.environ.get("INSTRUQT_EMAIL")
USER_DOMAIN = os.environ.get("USER_DOMAIN", "infoblox.lab")

# Hosted Infoblox MCP Server. Named per the brand guidelines: "Infoblox Model
# Context Protocol (MCP) Server" on first mention, "Infoblox MCP Server" after.
# Never "Infoblox IQ MCP Server".
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "https://csp.infoblox.com/mcp")

# Where setup-shell drops the clone. Checks source /root/.bashrc to pick this up.
LAB_DIR = os.environ.get("LAB_DIR", "/root/infoblox-lab/mcp-cloud-clarity-lab")
SCRIPT_DIR = os.path.join(LAB_DIR, "scripts")
TERRAFORM_DIR = os.path.join(LAB_DIR, "terraform")


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
#
# Claude runs on Amazon Bedrock in the Instruqt-provided AWS sandbox account and
# is served to the learner as a chat tab. Bedrock does NOT support the Claude
# API's `mcp_servers` connector (first-party / Foundry only), so the agent runs
# its own MCP client and drives the tool loop itself. agent/ has the code.
#
# This track connects the agent to TWO MCP servers at once — Infoblox for the
# DDI side and AWS for the cloud side — which is what makes Part 3's single
# conversation possible.

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-opus-5")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# The agent reads its Infoblox Service API key from this file on EVERY turn,
# rather than capturing it at boot. That is what makes the read-only ->
# read/write handover at Part 2 real: 02/setup-shell rewrites this file with the
# read/write key and 04/setup-shell writes the read-only key back, and the agent
# picks the change up on the next message with no restart.
AGENT_KEY_FILE = os.environ.get("AGENT_KEY_FILE", "/opt/lab/mcp_key")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8501"))

# Where the `lab-answer` helper records the number the participant reads off the
# Part 1 connection check. Instruqt has no native free-text answer field on a
# challenge, so the helper is how the track flow's "quiz field" is realised.
ANSWER_FILE = os.environ.get("LAB_ANSWER_FILE", "/opt/lab/answer_c1.txt")

# A healthy MCP connection returns a service catalog with dozens of entries; a
# broken one returns nothing or a handful. Anything at or above this is treated
# as "you were looking at a real catalog". See TODO-33 for why this is a band
# rather than an exact comparison.
MIN_PLAUSIBLE_SERVICE_COUNT = int(os.environ.get("LAB_MIN_SERVICES", "10"))

# The AWS MCP server the agent runs alongside Infoblox for Part 3. Stdio, run
# through uvx, so there is nothing to host and no second port to expose. It
# picks up credentials from the standard boto3 chain — track_scripts/setup-shell
# writes them to /root/.aws/credentials.
AWS_MCP_ENABLED = os.environ.get("AWS_MCP_ENABLED", "1") not in ("0", "false", "")
AWS_MCP_COMMAND = os.environ.get("AWS_MCP_COMMAND", "uvx")
AWS_MCP_ARGS = [
    a for a in os.environ.get(
        "AWS_MCP_ARGS", "awslabs.aws-api-mcp-server@latest"
    ).split()
    if a
]

# CSP groups assigned to each MCP user.
#
# Confirmed from a live sandbox. The tenant follows a consistent
# `ib-<service>-admin` / `ib-<service>-user` convention (ib-ddi-admin /
# ib-ddi-user, ib-td-admin / ib-td-user, and so on), and the MCP pair is
# ib-mcp-server-admin / ib-mcp-server-user.
#
# THREE groups per user, not one. An MCP-server role gates access to the MCP
# Server; it does not grant permission to read DNS, DHCP or IPAM data. Without
# the matching ib-ddi-* role the connection succeeds and then every tool call
# comes back empty or denied — which reads as a broken lab rather than a
# permissions problem.
#
# Deliberately NOT act_admin: an account-admin "read-only" user would make the
# Part 4 RBAC exercise meaningless.
MCP_RO_GROUPS = [
    g.strip() for g in os.environ.get(
        "MCP_RO_GROUPS", "user,ib-mcp-server-user,ib-ddi-user"
    ).split(",") if g.strip()
]
MCP_RW_GROUPS = [
    g.strip() for g in os.environ.get(
        "MCP_RW_GROUPS", "user,ib-mcp-server-admin,ib-ddi-admin"
    ).split(",") if g.strip()
]

# Single-name overrides. If set, each replaces just the MCP-server group in the
# corresponding list.
MCP_RO_GROUP = os.environ.get("MCP_RO_GROUP")
MCP_RW_GROUP = os.environ.get("MCP_RW_GROUP")

# State files written by setup, read by checks and teardown. All live in
# SCRIPT_DIR because that is the working directory the vendored iracic82
# scripts assume.
STATE_FILES = {
    "sandbox_id": "sandbox_id.txt",        # CSP account UUID
    "external_id": "external_id.txt",      # same value, broker's name for it
    "subtenant_id": "subtenant_id.txt",    # broker sandbox id, used to deallocate
    "sandbox_name": "sandbox_name.txt",    # human name, e.g. lab-adventure-0086
    "sfdc_account_id": "sfdc_account_id.txt",
    "user_id": "user_id.txt",              # interactive portal user
    "user_email": "user_email.txt",
    "user_password": "user_password.txt",
    "mcp_service_user_id": "mcp_service_user_id.txt",
    "mcp_ro_key_id": "mcp_ro_key_id.txt",
    "mcp_rw_key_id": "mcp_rw_key_id.txt",
    "mcp_ro_key": "mcp_ro_key.txt",        # chmod 600 — the secret itself
    "mcp_rw_key": "mcp_rw_key.txt",        # chmod 600 — the secret itself
    "vpc_outputs": "vpc_outputs.json",     # terraform output, Part 3
}


# --------------------------------------------------------------------------- #
# Topology — the deterministic world every participant starts in
# --------------------------------------------------------------------------- #
#
# These are the values behind every placeholder in the track flow document:
#
#   [ZONE NAME]        ZONE_FQDN
#   [DNS SERVER]       DC_HOST_NAME
#   [NETWORK]          SUBNETS["dc-01"]
#   [SUBNET]           SUBNETS["branch-02"]
#   [VPC NAME / ID]    VPC_NAME
#   [DNS SERVICE IP]   discovered at runtime, written to vpc_outputs.json
#
# Overridable so a maintainer can run two seedings side by side in one tenant,
# but the defaults are what the assignment prose names. CHANGE A DEFAULT HERE AND
# YOU MUST CHANGE THE MATCHING assignment.md TEXT.

DNS_VIEW_NAME = os.environ.get("LAB_DNS_VIEW", "techcorp-view")

# [ZONE NAME] — the internal zone the cloud team reports NXDOMAIN on. This is
# also the zone Part 3's test VM resolves against, which is deliberate: it ties
# the two halves of the lab into one story rather than two unrelated exercises.
ZONE_FQDN = os.environ.get("LAB_ZONE_FQDN", "svc.techcorp.internal.")

# The record the ticket names and the Part 3 dig probe queries.
APP_LABEL = os.environ.get("LAB_APP_LABEL", "payments")
APP_FQDN = f"{APP_LABEL}.{ZONE_FQDN}"

BASELINE_A_RECORDS = {
    "payments": {"address": "10.30.1.40", "comment": "Payments API - INC-4471"},
    "checkout": {"address": "10.30.1.41", "comment": "Checkout service"},
    "catalog":  {"address": "10.30.1.42", "comment": "Catalog service"},
    "inference": {"address": "10.30.1.43", "comment": "AI inference endpoint"},
}

BASELINE_CNAME_RECORDS = {
    "api": {"target": APP_FQDN, "comment": "Alias for the payments API"},
}

# [DNS SERVER] — the Universal DDI host serving the data-centre network. Part 2's
# break removes it from the zone's Authoritative DNS Servers list.
#
# Not created by this lab: the host is part of the sandbox the broker hands over.
# find_dc_host() discovers it if the configured name does not match.
DC_HOST_NAME = os.environ.get("LAB_DC_HOST", "niosx-dc-01")

# IPAM.
IP_SPACE_NAME = os.environ.get("LAB_IP_SPACE", "techcorp-ipam")
ADDRESS_BLOCK = {
    "address": os.environ.get("LAB_ADDRESS_BLOCK_ADDR", "10.30.0.0"),
    "cidr": int(os.environ.get("LAB_ADDRESS_BLOCK_CIDR", "16")),
    "name": "TechCorp Corporate",
}

SUBNETS = {
    # [NETWORK] — where the DNS server lives and where the ticket originates.
    "dc-01": {
        "address": "10.30.1.0", "cidr": 24,
        "name": "DC-01", "comment": "Primary data centre server segment",
    },
    # [SUBNET] — Part 4's unguided DHCP break lands here.
    "branch-02": {
        "address": "10.30.2.0", "cidr": 24,
        "name": "Branch-02", "comment": "Branch office client segment",
    },
}

# Branch-02 DHCP range. Healthy baseline: .100-.200, clear of the reserved block.
BRANCH_RANGE_HEALTHY = {
    "start": os.environ.get("LAB_BRANCH_RANGE_START", "10.30.2.100"),
    "end": os.environ.get("LAB_BRANCH_RANGE_END", "10.30.2.200"),
    "name": "Branch-02 DHCP",
}

# The reserved fixed addresses the IPAM cleanup was supposed to avoid.
BRANCH_RESERVED_BLOCK = {
    "start": os.environ.get("LAB_RESERVED_START", "10.30.2.10"),
    "end": os.environ.get("LAB_RESERVED_END", "10.30.2.30"),
    "name": "Branch-02 reserved infrastructure",
}

# Broken state for Part 4: the range was "narrowed" onto the reserved block, so
# every address in it is already spoken for and no lease can be issued.
BRANCH_RANGE_BROKEN = {
    "start": os.environ.get("LAB_BROKEN_RANGE_START", "10.30.2.10"),
    "end": os.environ.get("LAB_BROKEN_RANGE_END", "10.30.2.30"),
    "name": "Branch-02 DHCP",
}

UTILIZATION_THRESHOLD_PCT = float(os.environ.get("LAB_UTIL_THRESHOLD", "90"))

# The client host Part 4 requests a lease on.
CLIENT_HOST_NAME = os.environ.get("LAB_CLIENT_HOST", "branch-02-client")
CLIENT_HOST_MAC = os.environ.get("LAB_CLIENT_MAC", "02:42:0a:1e:02:0a")

# Tag stamped on everything the seeder creates, so teardown finds its own
# objects and leaves anything else in the tenant alone.
LAB_TAG_KEY = "instruqt-lab"
LAB_TAG_VALUE = os.environ.get("LAB_TAG_VALUE", TRACK_SLUG)


# --------------------------------------------------------------------------- #
# Part 3 — the AWS side
# --------------------------------------------------------------------------- #
#
# Terraform stands all of this up during track setup so it is warm and healthy
# before the participant reaches Part 3. The participant's conversation is about
# putting DNS *into* it, not about waiting for a VPC to exist.

# [VPC NAME / ID]. Suffixed with the participant id by Terraform so two
# participants in one AWS account never collide.
VPC_NAME = os.environ.get("LAB_VPC_NAME", "techcorp-ai-vpc")
VPC_CIDR = os.environ.get("LAB_VPC_CIDR", "10.40.0.0/16")
VPC_WORKLOAD_SUBNET_CIDR = os.environ.get("LAB_VPC_SUBNET_CIDR", "10.40.1.0/24")
VPC_REGION = os.environ.get("LAB_VPC_REGION", os.environ.get("AWS_DEFAULT_REGION",
                                                             "us-east-1"))
# The test VM inside the VPC that Part 3 step 3 digs from.
TEST_VM_NAME = os.environ.get("LAB_TEST_VM_NAME", "techcorp-ai-test")

# How Part 3 delivers DNS into the VPC.
#
#   as-a-service  the designed path. The participant creates a NIOS-X as a
#                 Service Deployment with an Access Location for the VPC, and
#                 the AWS side brings up the Site-to-Site VPN to the Infoblox
#                 PoP. Terraform pre-stages the VGW and the VPN connection so
#                 only the Infoblox half and the tunnel parameters are live work.
#
#   forwarder     the fallback. A Route 53 Resolver outbound endpoint in the VPC
#                 forwards svc.techcorp.internal to a Universal DDI host. No
#                 IPsec, no BGP, deterministic inside the timebox. Use this if
#                 the tunnel path proves too slow on the event day — the
#                 conversation the participant has is nearly identical.
#
# See README.md "Part 3 delivery modes" before changing this.
C3_MODE = os.environ.get("LAB_C3_MODE", "as-a-service")

# The Service Deployment the participant creates on the Infoblox side.
SERVICE_DEPLOYMENT_NAME = os.environ.get(
    "LAB_SERVICE_DEPLOYMENT", "techcorp-ai-vpc-dns"
)
ACCESS_LOCATION_NAME = os.environ.get("LAB_ACCESS_LOCATION", "techcorp-ai-vpc-aws")

# Where the check reads the DNS service IP from once it exists. Written by
# 03/check-shell via terraform output, or by the participant's own work.
DNS_SERVICE_IP = os.environ.get("LAB_DNS_SERVICE_IP")


# --------------------------------------------------------------------------- #
# CSP REST path registry
# --------------------------------------------------------------------------- #
#
# CONFIRMED — either in production use elsewhere in this estate today, or
# verified against Infoblox's OpenAPI-generated Go client
# (github.com/infobloxopen/universal-ddi-go-client).

PATHS = {
    # Identity / session. From user_provision.py and create_user.py.
    "signin":          "/v2/session/users/sign_in",
    "account_switch":  "/v2/session/account_switch",
    "groups":          "/v2/groups",
    "users":           "/v2/users",
    "user_password":   "/v2/users/{user_id}/password",
    "current_account": "/v2/current_account",
    "current_user":    "/v2/current_user",

    # API keys. `current_api_keys` mints a key for the *calling* identity
    # (deploy_api_key.py). `iam/v2/keys` lists and deletes.
    "current_api_keys": "/v2/current_api_keys",
    "iam_keys":         "/api/iam/v2/keys",
    "iam_key_by_id":    "/api/iam/v2/keys/{key_id}",

    # ---------------------------------------------------------------------- #
    # DDI — confirmed against the dnsconfig / dnsdata / ipam packages of the
    # Go client. Base path /api/ddi/v1.
    # ---------------------------------------------------------------------- #

    "dns_view":          "/api/ddi/v1/dns/view",
    "dns_view_id":       "/api/ddi/v1/dns/view/{view_id}",

    # THE central object for this track. An AuthZone carries
    # `internal_secondaries: [{"host": "<dns/host id>"}]` — the list the Infoblox
    # Portal renders as "Authoritative DNS Servers". It is a plain PATCHable
    # field (unlike `fqdn` and `primary_type`, which are read-only after
    # create), which is exactly why Part 2's break uses it: the participant can
    # fix it through the agent OR through the Portal and the check reads the
    # same field either way.
    "dns_auth_zone":     "/api/ddi/v1/dns/auth_zone",
    "dns_forward_zone":  "/api/ddi/v1/dns/forward_zone",
    "zone_child":        "/api/ddi/v1/dns/zone_child",

    # The DNS config profile IS the Server object — in Universal DDI there is no
    # separate "config profile" resource.
    "dns_config_profile": "/api/ddi/v1/dns/server",

    # The DNS-side view of a host. Read-only in the generated model, so this
    # track reads it and never writes it — the zone owns the association we care
    # about. (The sibling track patches `server` here; that is its risk, not
    # ours.)
    "dns_host":          "/api/ddi/v1/dns/host",

    # DNS records. `type` is the textual mnemonic ("A", "CNAME"); `rdata` is a
    # type-specific map — {"address": ...} for A, {"cname": ...} for CNAME.
    "dns_record":        "/api/ddi/v1/dns/record",

    # IPAM and DHCP. A range hangs off `space` + `parent`, NOT a `subnet` field.
    # A fixed address reserves exactly ONE address.
    "ipam_ip_space":     "/api/ddi/v1/ipam/ip_space",
    "ipam_address_block": "/api/ddi/v1/ipam/address_block",
    "ipam_subnet":       "/api/ddi/v1/ipam/subnet",
    "dhcp_range":        "/api/ddi/v1/ipam/range",
    "dhcp_fixed_address": "/api/ddi/v1/dhcp/fixed_address",

    # Infrastructure (inframgmt package — base path /api/infra/v1).
    "infra_hosts":        "/api/infra/v1/hosts",
    "infra_services":     "/api/infra/v1/services",
    "infra_applications": "/api/infra/v1/applications",
    "infra_detail_hosts": "/api/infra/v1/detail_hosts",
    "infra_detail_services": "/api/infra/v1/detail_services",

    # ---------------------------------------------------------------------- #
    # UNCONFIRMED — still questions for the CSP API docs.
    # Each Todo id maps to a row in README.md "TODO punch list".
    # ---------------------------------------------------------------------- #

    "service_deployment": Todo(
        "TODO-31",
        "What is the REST path for a NIOS-X as a Service *Service Deployment*, "
        "and for the Access Locations under it? There is no package for this in "
        "the public universal-ddi-go-client (inframgmt covers Hosts and "
        "Services only), so it needs answering from the CSP API docs directly. "
        "Part 3's Infoblox-side check falls back to the dig probe from the test "
        "VM while this is unanswered, which still validates the outcome — it "
        "just cannot name the object that produced it.",
    ),
    "access_location": Todo(
        "TODO-32",
        "REST path for Access Locations, and which field carries the Cloud "
        "Service IP the test VM must query. Part 3 step 3 needs that IP; today "
        "it is read from terraform output or supplied via LAB_DNS_SERVICE_IP.",
    ),
    "service_catalog": Todo(
        "TODO-33",
        "Which endpoint does the MCP server's service-discovery tool actually "
        "wrap? Part 1 asks the participant to count the services it returns and "
        "the check would like to compare that number against the tenant. "
        "/api/infra/v1/services lists DEPLOYED services on hosts, which is a "
        "different thing from the CSP service catalog the discovery tool "
        "describes — comparing against it would fail participants who counted "
        "correctly. Until the right endpoint is known, check_c1 validates the "
        "answer for plausibility rather than for exactness.",
    ),
    "dhcp_lease": Todo(
        "TODO-15",
        "Which endpoint LISTS active DHCP leases? The ipam package only exposes "
        "POST /dhcp/leases_command (for clearing leases) — there is no "
        "lease-list operation in the public Go client. Part 4's final assertion "
        "wants to prove a lease was issued to the branch client host.",
    ),
    "dns_activity_cube": Todo(
        "TODO-16",
        "Which endpoint backs the DNS activity analytics the Part 1 and Part 2 "
        "exploration prompts rely on — top queried domains, NXDOMAIN by client, "
        "query failures over 24h? The MCP setup guide calls these analytics "
        "cubes; the checks need the REST path, the time-window parameter and "
        "the response shape.",
    ),
    "audit_log": Todo(
        "TODO-18",
        "REST path for the CSP audit log, and the filter that isolates changes "
        "made by a specific service user. Part 2 step 2 asks the participant to "
        "find their own change in the Portal; the check would like to confirm "
        "the record exists.",
    ),
}


def path(key, **fmt):
    """
    Resolve a registry key to a REST path.

    Raises LabTodo — loudly, with the question — for anything unconfirmed.
    """
    try:
        value = PATHS[key]
    except KeyError:
        raise KeyError(f"No CSP path registered under {key!r}") from None

    if is_todo(value):
        raise LabTodo(f"[{value.ident}] {value.question}")

    return value.format(**fmt) if fmt else value


def require_env():
    """Fail fast with one message naming every missing variable."""
    missing = [
        name for name, value in (
            ("INFOBLOX_EMAIL", INFOBLOX_EMAIL),
            ("INFOBLOX_PASSWORD", INFOBLOX_PASSWORD),
            ("INSTRUQT_PARTICIPANT_ID", PARTICIPANT_ID),
        ) if not value
    ]
    if missing:
        raise SystemExit(
            "❌ Missing required environment variables: " + ", ".join(missing) +
            "\n   See mcp-cloud-clarity-lab/README.md for the full list."
        )
