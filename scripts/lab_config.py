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
# The assistant
# --------------------------------------------------------------------------- #
#
# Claude Code, running against Amazon Bedrock in the Instruqt sandbox account.
# The participant works in a terminal tab; scripts/setup_claude_code.sh installs
# and configures it.
#
# This replaced a custom Streamlit agent that spoke MCP through the `mcp` Python
# package. Owning an MCP client meant owning its churn — a renamed factory, a
# changed signature, a swapped HTTP library — and four track starts died on that
# without teaching a participant anything. Claude Code is the vendor-documented
# path for this server and maintains the client itself.

# The model, as a cross-region inference profile id. Claude Code needs a profile
# id here; a bare `anthropic.…` fails with an on-demand-throughput error.
#
# Pinned rather than defaulted: Claude Code on Bedrock defaults its primary
# model to Opus 5 and its `sonnet` alias to Sonnet 4.5, so an unpinned lab runs
# a different model than intended AND is billed at the Opus rate.
# pick_bedrock_model.py confirms the account can invoke this and falls back
# sensibly if not.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID",
                                  "us.anthropic.claude-sonnet-4-6")
BEDROCK_MODEL_PREFERENCE = os.environ.get("BEDROCK_MODEL_PREFERENCE", "sonnet")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Where the participant runs Claude Code. Its own directory, not the lab's
# scripts directory — an assistant should not open onto the machinery that
# built its environment.
CLAUDE_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR", "/root/techcorp")

# The Infoblox Service API key currently in force. setup_claude_code.sh reads
# this file and bakes it into the MCP server registration, so rewriting the file
# and re-running that script with --keys-only is how Part 2 hands over write
# access. Unlike the old per-turn agent, this needs Claude Code restarted — the
# challenge boundary is where that happens.
AGENT_KEY_FILE = os.environ.get("AGENT_KEY_FILE", "/opt/lab/mcp_key")

# The AWS MCP server Claude Code runs alongside Infoblox for Part 3.
AWS_MCP_ENABLED = os.environ.get("AWS_MCP_ENABLED", "1") not in ("0", "false", "")

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

# Which service users to provision. ONE, read/write, by default.
#
# The track used to mint two and swap between them at Part 2 and Part 4, so that
# "the assistant could not have written even if it tried" was enforced by CSP
# rather than claimed by the prose. In practice it meant a key swap plus a
# Claude Code restart at two challenge boundaries, and participants arriving at
# Part 2 unable to make the change they had just been told to make.
#
# Set MCP_ROLES=read_only,read_write to restore the two-key flow.
MCP_ROLES = [r.strip() for r in
             os.environ.get("MCP_ROLES", "read_write").split(",") if r.strip()]

# How long a minted Service API key lives.
#
# POST /v2/current_api_keys REQUIRES expires_at. Omitting it does not default to
# anything — CSP rejects the call with
# `HTTP interceptor error: invalid datetime or duration`, which is a 400 that
# names neither the field nor the endpoint's expectation. That message cost a
# track start; if you see it again on any CSP POST, look for a missing or
# malformed timestamp before anything else.
#
# 24 hours rather than the estate's traditional hardcoded far-future date. The
# track runs for 90 minutes and cleanup revokes both keys explicitly, so this is
# only the backstop for a cleanup that did not run — and a backstop measured in
# hours is worth having. A hardcoded calendar date is also a time bomb: it works
# until it silently does not.
MCP_KEY_TTL_HOURS = int(os.environ.get("MCP_KEY_TTL_HOURS", "24"))

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
    "mcp_key_id": "mcp_key_id.txt",
    "mcp_key": "mcp_key.txt",              # chmod 600 — the secret itself
    # Only written when MCP_ROLES includes read_only — the optional two-key flow.
    "mcp_ro_key_id": "mcp_ro_key_id.txt",
    "mcp_ro_key": "mcp_ro_key.txt",
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

# [DNS SERVER] — what Part 2's break removes from the zone's Authoritative DNS
# Servers list. Which OBJECT that is depends on what the sandbox actually has.
#
# AUTH_MODE picks between two ways of expressing "this thing is authoritative
# for this zone", both of which the Infoblox Portal renders in the same place on
# a zone's edit page:
#
#   host   A Universal DDI host, via the zone's `internal_secondaries`. The
#          higher-fidelity option: a real server really does answer, so the
#          NXDOMAIN in the ticket is a genuine query result rather than a
#          described one. Requires the sandbox to HAVE a registered host.
#
#   nsg    A DNS Server Group, via the zone's `nsgs`. An AuthNSG needs only a
#          name to exist, so the lab can create it and no host is required.
#          The configuration fault is identical and the participant's
#          conversation is nearly identical — what is lost is that nothing
#          actually serves the zone, so there is no live NXDOMAIN to dig for.
#
#   auto   Use a host if the tenant has one, otherwise fall back to a server
#          group. The default, because a broker-allocated sandbox turns out not
#          to ship with a host and a lab that refuses to start is worse than one
#          that starts slightly less vividly.
#
# resolve_dns_authority() logs which one it picked and why.
AUTH_MODE = os.environ.get("LAB_AUTH_MODE", "auto")

# Used when AUTH_MODE resolves to `host`. Not created by this lab.
DC_HOST_NAME = os.environ.get("LAB_DC_HOST", "niosx-dc-01")

# Used when AUTH_MODE resolves to `nsg`. Created by the lab, and torn down.
DNS_SERVER_GROUP_NAME = os.environ.get("LAB_DNS_SERVER_GROUP",
                                       "techcorp-dc-servers")

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

# The NIOS-X as a Service objects. SEEDED, not built by the participant — the
# Infoblox MCP Server is read-only, so nothing in the lab can create these
# during Part 3. Seeding uses the CSP REST API with admin credentials instead,
# and Part 3 becomes the AWS half of the deployment.
SERVICE_DEPLOYMENT_NAME = os.environ.get(
    "LAB_SERVICE_DEPLOYMENT", "techcorp-ai-vpc-dns"
)
ENDPOINT_NAME = os.environ.get("LAB_ENDPOINT_NAME", "techcorp-ai-vpc-endpoint")
ACCESS_LOCATION_NAME = os.environ.get("LAB_ACCESS_LOCATION", "techcorp-ai-vpc-aws")

# The endpoint's anycast/VIP address, which the VPC's DHCP option set will
# eventually point at. Pinned rather than left to the assistant to invent: the
# Part 3 check needs to know where to send its query, and "10.40.0.53" chosen
# on the fly is not something a check can predict.
#
# Inside the VPC CIDR but outside both workload subnets, so it cannot collide
# with an instance address.
SERVICE_IP = os.environ.get("LAB_SERVICE_IP", "10.40.0.53")

# Instance size for the endpoint. Validated against GET /supportedsizes at seed
# time, which is also how you find out what else is on offer.
# A live sandbox reports S/M/L/XL from GET /supportedsizes — not the SMALL /
# MEDIUM / LARGE this originally assumed. service_deployment.pick_size()
# validates against that endpoint anyway, so a tenant using the other spelling
# still works; this is just the value that avoids a needless fallback.
ENDPOINT_SIZE = os.environ.get("LAB_ENDPOINT_SIZE", "S")

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

    # DNS Server Group. The host-free way to express "these servers are
    # authoritative for this zone": an AuthNSG needs only a `name` to exist, and
    # an AuthZone references a list of them in `nsgs`. See AUTH_MODE below for
    # why that matters.
    "dns_auth_nsg":      "/api/ddi/v1/dns/auth_nsg",

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

    # ---------------------------------------------------------------------- #
    # NIOS-X as a Service. Base path /api/universalinfra/v1.
    #
    # Not in the public universal-ddi-go-client — these were read out of the
    # MCP server's own tool schemas against a live tenant. Creation order is
    # strict, because each needs the id of the one above it:
    #
    #   universalservices -> endpoints -> accesslocations
    # ---------------------------------------------------------------------- #
    "universal_service":  "/api/universalinfra/v1/universalservices",
    "endpoints":          "/api/universalinfra/v1/endpoints",
    "access_locations":   "/api/universalinfra/v1/accesslocations",
    "supported_sizes":    "/api/universalinfra/v1/supportedsizes",

    # Locations live under the infra API, not universalinfra.
    "infra_locations":    "/api/infra/v1/locations",

    # What a Universal Service is actually serving. Read-only: there is no POST
    # here. The association is made by giving the service a DNS capability
    # whose profile_id points at a dns/server config profile, and by the zone
    # carrying primary_type "cloud".
    "us_associations":
        "/api/ddi/v1/dns/universal_service/{service_id}/associations",

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
