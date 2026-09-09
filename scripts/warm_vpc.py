#!/usr/bin/env python3
"""
Get the Part 3 VPC all the way to usable, at track boot, in the background.

WHY THIS RUNS AT BOOT AND NOT IN PART 3
---------------------------------------
Terraform already ran at boot. But creating the test VM is not the same as
being able to run a command on it: `terraform apply` returns as soon as the
instance is `running`, which is before sshd is accepting connections and before
cloud-init has installed the key pair's public half. Until both are true, every
probe against the VM fails to connect.

That waiting used to happen in `03/setup-shell`, where the participant is sat
watching a challenge load. It is the same wall-clock time either way — the
difference is whether it overlaps Parts 1 and 2, which take half an hour, or
lands squarely in the participant's lap. So it happens here.

By the time anyone reaches Part 3, this has long since finished and
`03/setup-shell` only has to read the result.

WHAT IT WRITES
--------------
`/opt/lab/vpc_status.json`:

    {"reach": "SSH to ec2-user@… established", "exec": "…",
     "probe": "resolved", "detail": "…", "ready": true}

`03/setup-shell` reports that rather than re-deriving it. Absent means this is
still running, which the challenge handles without blocking.

WHAT FAILS THE TRACK, AND WHAT DOES NOT
---------------------------------------
Three separate questions, and only two of them are fatal:

  1. Can we open an SSH session to the VM?    FATAL — nothing can run on it.
  2. Can we run a command, and is python3     FATAL — Part 3's check is a
     there?                                     Python DNS client run over SSH.
  3. Does a public name resolve from it?      WARNING ONLY.

(3) used to be fatal and should not have been. It is not the path Part 3
exercises — that one resolves an *internal* name through infrastructure the
participant has not built yet — so a quirk in public DNS resolution at boot
would fail a track whose Part 3 was perfectly capable of succeeding. It is kept
as a warm-up because when it does work it proves the whole chain end to end.

(1) and (2) genuinely make Part 3 impossible, and better to fail at boot, where
the participant loses a restart, than thirty minutes in, where they lose their
work. track_scripts/setup-shell blocks on this and fails the track start.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cloud_vpc  # noqa: E402


STATUS_FILE = os.environ.get("LAB_VPC_STATUS", "/opt/lab/vpc_status.json")

# The track start blocks on this, so it cannot be open-ended. A VM that is going
# to accept SSH does so within a minute or two of `running`; five is generous.
#
# Both names are read. LAB_SSM_WAIT is what the tracks in this estate already
# set, and renaming it in the code would silently ignore an override someone had
# deliberately configured — the worst kind of rename.
SSM_WAIT_SECONDS = int(
    os.environ.get("LAB_VM_WAIT", os.environ.get("LAB_SSM_WAIT", "300"))
)

# Whether an unusable test VM stops the track starting.
#
# Default 1: Part 3's check runs a command on that VM, so without it the
# challenge cannot be completed, and failing at boot costs a restart rather
# than half an hour of the participant's work.
#
# ---- EDIT THIS LINE to change the behaviour without an Instruqt secret ----
#   "1"  an unusable test VM fails the track start   (the strict default)
#   "0"  it becomes a warning; the track starts and Parts 1, 2 and 4 work,
#        and Part 3 fails at its own check instead of at boot
#
# Currently "0". It was set that way while the SSM Run Command problem was being
# diagnosed; that problem is now gone with SSM itself, but the SSH path replacing
# it has not yet had a green run against real AWS, and a first run of an untested
# exec path is exactly when you want the track to start anyway so you can read
# the diagnosis.
#
# PUT IT BACK TO "1" AFTER THE FIRST CLEAN RUN. Leaving it at "0" permanently
# means a broken test VM produces a track that starts, runs for half an hour,
# and fails at Part 3 — which is the outcome this whole gate exists to prevent.
#
# NOTE FOR ANYONE EDITING THIS: writing `LAB_REQUIRE_TEST_VM=0` above the
# os.environ.get() line does nothing. That creates a Python variable; it does
# not set an environment variable, so the get() below still returns its
# default. Change REQUIRE_TEST_VM_DEFAULT, or set the env var properly.
REQUIRE_TEST_VM_DEFAULT = "1"

# The environment still wins, so an Instruqt secret can override the file.
REQUIRE_TEST_VM = os.environ.get(
    "LAB_REQUIRE_TEST_VM", REQUIRE_TEST_VM_DEFAULT
) not in ("0", "false", "")


def write_status(**fields):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as handle:
        json.dump(fields, handle, indent=2)
    print(json.dumps(fields, indent=2), flush=True)


def main():
    source = ("LAB_REQUIRE_TEST_VM env var"
              if "LAB_REQUIRE_TEST_VM" in os.environ
              else "REQUIRE_TEST_VM_DEFAULT in warm_vpc.py")
    print(f"test VM required for track start: {REQUIRE_TEST_VM}  "
          f"(from {source})", flush=True)

    instance = cloud_vpc.test_vm_instance_id()
    if not instance:
        write_status(ready=False, reach="no test VM found", exec="skipped",
                     probe="skipped", fatal=True,
                     detail="terraform did not produce a test VM instance id")
        return 1

    # -- 1. Reachable at all? ----------------------------------------------
    print(f"waiting for SSH to {instance} "
          f"(up to {SSM_WAIT_SECONDS}s)...", flush=True)
    online, status = cloud_vpc.test_vm_reachable(instance,
                                                 wait=SSM_WAIT_SECONDS)
    if not online:
        # Print the facts before the verdict. A VM that never registers is the
        # case where SSM-based probing tells us nothing at all, so this is
        # exactly where the console block earns its keep — it is read over
        # ec2:GetConsoleOutput and does not need the agent to be working.
        print("\n--- test VM diagnosis ---", flush=True)
        try:
            print(cloud_vpc.diagnose_test_vm(instance), flush=True)
        except Exception as exc:                        # noqa: BLE001
            print(f"   diagnosis failed: {exc}", flush=True)
        print("-------------------------\n", flush=True)

        write_status(
            ready=False, reach=status, exec="skipped", probe="skipped",
            fatal=True,
            detail=("The test VM never became reachable over SSH. Part 3's "
                    "verification runs commands on it, so the challenge cannot "
                    "work. Check, in this order: that the security group's "
                    "port 22 rule covers this container's egress address (the "
                    "diagnosis above prints both, and they must agree); that "
                    "the private key exists where Terraform wrote it; that the "
                    "workload subnet is associated with the route table "
                    "carrying the default route; and that cloud-init finished "
                    "and sshd is up, which the console block reports."),
        )
        return 1
    print(f"   reachable: {status}", flush=True)

    # -- 2. Can we actually execute, and is python3 present? ----------------
    can_run, exec_detail = cloud_vpc.test_vm_can_run_commands(instance)
    print(f"   exec: {exec_detail}", flush=True)
    if not can_run:
        # Four theories about this have each been wrong. Print the facts.
        print("\n--- test VM diagnosis ---", flush=True)
        try:
            print(cloud_vpc.diagnose_test_vm(instance), flush=True)
        except Exception as exc:                        # noqa: BLE001
            print(f"   diagnosis failed: {exc}", flush=True)
        print("-------------------------\n", flush=True)

        write_status(ready=False, reach=status, exec=exec_detail, probe="skipped",
                     fatal=REQUIRE_TEST_VM,
                     detail=f"The test VM cannot run the Part 3 probe: "
                            f"{exec_detail}")
        if not REQUIRE_TEST_VM:
            print("⚠️  LAB_REQUIRE_TEST_VM=0 — continuing anyway. Parts 1, 2 "
                  "and 4 will work; Part 3 will not.", flush=True)
            return 0
        print("   Set LAB_REQUIRE_TEST_VM=0 to start the track anyway and run "
              "Parts 1, 2 and 4 while this is diagnosed.", flush=True)
        return 1

    # -- 3. Warm-up query. Informative, not fatal. --------------------------
    answers, probe_detail = cloud_vpc.resolve_from_test_vm("amazon.com")
    if answers:
        write_status(ready=True, reach=status, exec=exec_detail,
                     probe="resolved", fatal=False,
                     detail=f"amazon.com -> {answers[0]} from inside the VPC")
        return 0

    # Everything Part 3 needs is present; only the convenience check failed.
    write_status(
        ready=True, reach=status, exec=exec_detail, probe="failed", fatal=False,
        detail=(f"The VM is manageable and can run the probe, but the warm-up "
                f"query did not resolve: {probe_detail}. This does NOT block "
                f"Part 3 — that resolves an internal name through DNS the "
                f"participant builds, which does not exist yet. Worth a look "
                f"if step 3 later fails too."),
    )
    print("⚠️  warm-up query failed; not fatal — see the detail above",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
