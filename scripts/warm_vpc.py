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

EXITS NON-ZERO IF THE VM IS NOT USABLE. A VPC whose test VM cannot be reached
is not a lab that can complete Part 3 — the load-bearing check runs `dig` on
that VM. Better to fail the track at boot, where the participant loses nothing
but a restart, than thirty minutes in, where they lose the work they have done.
track_scripts/setup-shell blocks on this and fails the track start.
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


def write_status(**fields):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as handle:
        json.dump(fields, handle, indent=2)
    print(json.dumps(fields, indent=2), flush=True)


def main():
    instance = cloud_vpc.test_vm_instance_id()
    if not instance:
        write_status(ready=False, ssm="no test VM found", probe="skipped",
                     detail="terraform did not produce a test VM instance id")
        return 1

    print(f"waiting for SSM to adopt {instance} "
          f"(up to {SSM_WAIT_SECONDS}s)...", flush=True)
    online, status = cloud_vpc.ssm_registered(instance, wait=SSM_WAIT_SECONDS)

    if not online:
        write_status(
            ready=False, ssm=status, probe="skipped",
            detail=("The test VM never became manageable through SSM. Part 3's "
                    "verification runs commands on it, so step 3 will not work. "
                    "Check that `ssm` is in the AWS services list in config.yml, "
                    "that the three SSM interface endpoints came up, and that "
                    "the instance profile is attached."),
        )
        return 1

    # One warm-up query against a public name. It proves the whole path — SSM
    # transport, python3 on the VM, the DNS client itself — without depending on
    # anything the participant has not built yet.
    answers, detail = cloud_vpc.resolve_from_test_vm("amazon.com")
    if answers:
        write_status(ready=True, ssm=status, probe="resolved",
                     detail=f"amazon.com -> {answers[0]} from inside the VPC")
        return 0

    write_status(ready=False, ssm=status, probe="failed", detail=detail)
    return 1


if __name__ == "__main__":
    sys.exit(main())
