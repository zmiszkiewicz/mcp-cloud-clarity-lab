#!/usr/bin/env python3
"""
Get the Part 3 VPC all the way to usable, at track boot, in the background.

WHY THIS RUNS AT BOOT AND NOT IN PART 3
---------------------------------------
Terraform already ran at boot. But creating the test VM is not the same as
being able to run a command on it: the SSM agent registers a minute or two
after the instance boots, and until it does, every probe against the VM sits
Pending until something times out.

That waiting used to happen in `03/setup-shell`, where the participant is sat
watching a challenge load. It is the same wall-clock time either way — the
difference is whether it overlaps Parts 1 and 2, which take half an hour, or
lands squarely in the participant's lap. So it happens here.

By the time anyone reaches Part 3, this has long since finished and
`03/setup-shell` only has to read the result.

WHAT IT WRITES
--------------
`/opt/lab/vpc_status.json`:

    {"ssm": "Online", "probe": "resolved", "detail": "…", "ready": true}

`03/setup-shell` reports that rather than re-deriving it. Absent means this is
still running, which the challenge handles without blocking.

WHAT FAILS THE TRACK, AND WHAT DOES NOT
---------------------------------------
Three separate questions, and only two of them are fatal:

  1. Is the VM manageable through SSM?        FATAL — nothing can run on it.
  2. Can we run a command, and is python3     FATAL — Part 3's check is a
     there?                                     Python DNS client run over SSM.
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

# The track start blocks on this, so it cannot be open-ended. An SSM agent that
# is going to register does so in a minute or two; five is generous.
SSM_WAIT_SECONDS = int(os.environ.get("LAB_SSM_WAIT", "300"))

# Whether an unusable test VM stops the track starting.
#
# Default 1: Part 3's check runs a command on that VM, so without it the
# challenge cannot be completed, and failing at boot costs a restart rather
# than half an hour of the participant's work.
#
# Set LAB_REQUIRE_TEST_VM=0 to downgrade it to a warning — useful when you want
# to run Parts 1, 2 and 4 while the VM is still being diagnosed. Part 3 will
# still fail; it will just fail there instead of here.
REQUIRE_TEST_VM = os.environ.get("LAB_REQUIRE_TEST_VM", "1") not in ("0", "false")


def write_status(**fields):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as handle:
        json.dump(fields, handle, indent=2)
    print(json.dumps(fields, indent=2), flush=True)


def main():
    instance = cloud_vpc.test_vm_instance_id()
    if not instance:
        write_status(ready=False, ssm="no test VM found", exec="skipped",
                     probe="skipped", fatal=True,
                     detail="terraform did not produce a test VM instance id")
        return 1

    # -- 1. Manageable at all? ---------------------------------------------
    print(f"waiting for SSM to adopt {instance} "
          f"(up to {SSM_WAIT_SECONDS}s)...", flush=True)
    online, status = cloud_vpc.ssm_registered(instance, wait=SSM_WAIT_SECONDS)
    if not online:
        write_status(
            ready=False, ssm=status, exec="skipped", probe="skipped",
            fatal=True,
            detail=("The test VM never became manageable through SSM. Part 3's "
                    "verification runs commands on it, so the challenge cannot "
                    "work. Check that `ssm` is in the AWS services list in "
                    "config.yml, that the three SSM interface endpoints came "
                    "up, and that the instance profile is attached."),
        )
        return 1
    print(f"   SSM: {status}", flush=True)

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

        write_status(ready=False, ssm=status, exec=exec_detail, probe="skipped",
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
        write_status(ready=True, ssm=status, exec=exec_detail,
                     probe="resolved", fatal=False,
                     detail=f"amazon.com -> {answers[0]} from inside the VPC")
        return 0

    # Everything Part 3 needs is present; only the convenience check failed.
    write_status(
        ready=True, ssm=status, exec=exec_detail, probe="failed", fatal=False,
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
