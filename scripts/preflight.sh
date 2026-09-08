#!/usr/bin/env bash
#
# Run before every `git push` to this repo.
#
# Catches the class of bug that otherwise only surfaces three minutes into a
# track start with a participant watching: a Python file that does not parse, a
# break with no matching assertion, a registry key referenced by a script but
# never defined, or an assignment that names a value the config no longer has.
#
# Deliberately does NOT call CSP or AWS. It needs no credentials and runs
# offline.

set -uo pipefail
cd "$(dirname "$0")"

FAILED=0
step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
pass() { printf '   ✅ %s\n' "$1"; }
fail() { printf '   ❌ %s\n' "$1"; FAILED=1; }

# --------------------------------------------------------------------------- #
step "Python syntax"
if python3 -m py_compile ./*.py 2>/tmp/preflight_py.log; then
  pass "all scripts parse"
else
  fail "py_compile failed:"; sed 's/^/      /' /tmp/preflight_py.log
fi

# --------------------------------------------------------------------------- #
step "Module imports"
# lab_config and csp_client must import with no environment set at all —
# anything that reads a required env var at import time breaks `--help`.
if env -i PATH="$PATH" python3 -c "import lab_config, csp_client, baseline, breaks" \
     2>/tmp/preflight_import.log; then
  pass "core modules import cleanly with an empty environment"
else
  fail "import failed:"; sed 's/^/      /' /tmp/preflight_import.log
fi

# cloud_vpc must import even where boto3 is absent — the check container is not
# guaranteed to have it, and an ImportError at module scope would turn a
# skippable probe into a failed challenge.
if env -i PATH="$PATH" python3 -c "import cloud_vpc" 2>/tmp/preflight_cloud.log; then
  pass "cloud_vpc imports without boto3 present"
else
  fail "cloud_vpc import failed:"; sed 's/^/      /' /tmp/preflight_cloud.log
fi

# --------------------------------------------------------------------------- #
step "Break registry integrity"
python3 - <<'PY'
import sys
import breaks

problems = []
for name, spec in breaks.BREAKS.items():
    for required in ("part", "summary", "apply", "assert", "fix"):
        if required not in spec:
            problems.append(f"{name}: missing {required!r}")
    for callable_key in ("apply", "assert", "fix"):
        if callable_key in spec and not callable(spec[callable_key]):
            problems.append(f"{name}: {callable_key} is not callable")
    # Every break must belong to a part that exists and has a check.
    if spec.get("part") not in (2, 4):
        problems.append(f"{name}: part {spec.get('part')} is not 2 or 4")

if problems:
    for problem in problems:
        print(f"      {problem}")
    sys.exit(1)
print(f"      {len(breaks.BREAKS)} breaks, each with apply/assert/fix")
PY
if [ $? -eq 0 ]; then pass "break registry is well formed"; else fail "break registry is malformed"; fi

# --------------------------------------------------------------------------- #
step "Overlap helper"
python3 - <<'PY'
import sys
from breaks import ranges_overlap

cases = [
    ("10.30.2.10",  "10.30.2.30",  "10.30.2.10", "10.30.2.30", True),   # identical
    ("10.30.2.100", "10.30.2.200", "10.30.2.10", "10.30.2.30", False),  # disjoint
    ("10.30.2.20",  "10.30.2.120", "10.30.2.10", "10.30.2.30", True),   # partial
    ("10.30.2.30",  "10.30.2.40",  "10.30.2.10", "10.30.2.30", True),   # touching
    ("10.30.2.31",  "10.30.2.40",  "10.30.2.10", "10.30.2.30", False),  # just clear
]
bad = [c for c in cases if ranges_overlap(*c[:4]) is not c[4]]
if bad:
    for case in bad:
        print(f"      wrong result for {case}")
    sys.exit(1)
print(f"      {len(cases)} overlap cases correct")
PY
if [ $? -eq 0 ]; then pass "range overlap logic is correct"; else fail "range overlap logic is wrong"; fi

# --------------------------------------------------------------------------- #
step "Seeded state is internally consistent"
python3 - <<'PY'
import sys
import lab_config as cfg
from breaks import ranges_overlap

problems = []

# The healthy range must NOT overlap the reserved block, or the baseline is
# already broken and Part 4 has nothing to teach.
if ranges_overlap(cfg.BRANCH_RANGE_HEALTHY["start"],
                  cfg.BRANCH_RANGE_HEALTHY["end"],
                  cfg.BRANCH_RESERVED_BLOCK["start"],
                  cfg.BRANCH_RESERVED_BLOCK["end"]):
    problems.append("the HEALTHY DHCP range overlaps the reserved block")

# The broken range MUST overlap it, or the break does not reproduce.
if not ranges_overlap(cfg.BRANCH_RANGE_BROKEN["start"],
                      cfg.BRANCH_RANGE_BROKEN["end"],
                      cfg.BRANCH_RESERVED_BLOCK["start"],
                      cfg.BRANCH_RESERVED_BLOCK["end"]):
    problems.append("the BROKEN DHCP range does not overlap the reserved block")

# The record Part 3's dig probe expects must exist in the baseline.
if cfg.APP_LABEL not in cfg.BASELINE_A_RECORDS:
    problems.append(f"APP_LABEL {cfg.APP_LABEL!r} has no baseline A record — "
                    f"check_c3 asserts against it")

# Part 3 mode must be one the checks understand.
if cfg.C3_MODE not in ("as-a-service", "forwarder"):
    problems.append(f"LAB_C3_MODE {cfg.C3_MODE!r} is not as-a-service or forwarder")

# Zone FQDNs must be fully qualified — CSP is strict about the trailing dot.
for name, value in (("ZONE_FQDN", cfg.ZONE_FQDN), ("APP_FQDN", cfg.APP_FQDN)):
    if not value.endswith("."):
        problems.append(f"{name} {value!r} is missing its trailing dot")

if problems:
    for problem in problems:
        print(f"      {problem}")
    sys.exit(1)
print("      topology constants agree with each other")
PY
if [ $? -eq 0 ]; then pass "topology is self-consistent"; else fail "topology is inconsistent"; fi

# --------------------------------------------------------------------------- #
step "Assignments match the config"
# The single-source-of-truth rule from the track flow: every value the prose
# names must come from lab_config. This catches the specific failure where
# someone changes a CIDR here and the assignment silently keeps the old one.
python3 - <<'PY'
import os
import sys
import lab_config as cfg

track = os.path.abspath(os.path.join(
    os.path.dirname(__file__) if "__file__" in dir() else ".",
    "..", "..", "infoblox-mcp-cloud-clarity"))
if not os.path.isdir(track):
    print("      track directory not alongside the repo — skipping")
    sys.exit(0)

wanted = {
    "zone":     cfg.ZONE_FQDN.rstrip("."),
    "app":      cfg.APP_FQDN.rstrip("."),
    "network":  f"{cfg.SUBNETS['dc-01']['address']}/{cfg.SUBNETS['dc-01']['cidr']}",
    "subnet":   f"{cfg.SUBNETS['branch-02']['address']}/{cfg.SUBNETS['branch-02']['cidr']}",
    "vpc":      cfg.VPC_NAME,
}

blob = ""
for root, _dirs, files in os.walk(track):
    for name in files:
        if name == "assignment.md":
            with open(os.path.join(root, name)) as handle:
                blob += handle.read()

missing = [f"{k} ({v})" for k, v in wanted.items() if v not in blob]
if missing:
    print("      no assignment mentions: " + ", ".join(missing))
    print("      Either the prose drifted from lab_config, or these values are")
    print("      not surfaced to the participant at all. Both are worth a look.")
    sys.exit(1)
print("      every topology value the config defines appears in the prose")
PY
if [ $? -eq 0 ]; then pass "assignments and config agree"; else fail "assignments drifted from config"; fi

# --------------------------------------------------------------------------- #
step "Unresolved TODOs"
python3 - <<'PY'
import lab_config as cfg
todos = {k: v for k, v in cfg.PATHS.items() if cfg.is_todo(v)}
if todos:
    print(f"      {len(todos)} CSP path(s) still unconfirmed:")
    for key, todo in sorted(todos.items(), key=lambda kv: kv[1].ident):
        print(f"        {todo.ident}  {key}")
    print("      Each degrades a check rather than blocking it — see README.md.")
else:
    print("      none — every CSP path is confirmed")
PY

# --------------------------------------------------------------------------- #
printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf '\033[1;32m✅ preflight passed\033[0m\n'
else
  printf '\033[1;31m❌ preflight failed — do not push\033[0m\n'
fi
exit "$FAILED"
