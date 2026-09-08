# Traffic generation (TODO-19)

## Why this matters

Several prompts in this track are only as good as the telemetry behind them:

| Where | Prompt | Needs |
|---|---|---|
| Part 1 step 3 | "Give me a summary of my Infoblox environment" | query volume, service health |
| Part 2 step 3 | "Which clients generated the most NXDOMAIN responses in the last 24 hours?" | DNS activity cube |
| Part 2 step 3 | "What is the health status of my DNS services?" | host metrics |

Without traffic, those prompts return empty and the participant has a hollow
conversation. The *structural* prompts — zone configuration, authoritative
server lists, DHCP range utilization — all work fine on seeded data alone, which
is why the two graded incidents (INC-4471 and the Part 4 DHCP fault) deliberately
do not depend on anything here.

**Nothing in this directory is wired up yet.** The exploration prompts are
marked in `02/assignment.md` with a note telling the participant that an empty
answer is the telemetry, not the assistant. That is honest, but it is not as good
as real data.

## What needs answering

`iq-insighter` (TME-787) is the estate's traffic generator. Two questions:

1. **How is it invoked?** Container, script, or a service in the sandbox?
   Whatever it is, it gets started from `01/setup-shell`, where the TODO-19
   marker already sits.

2. **How long must it run before the analytics cubes are populated?** This is
   the design risk, not a detail. If the cubes need a genuine 24-hour window,
   per-participant ephemeral tenants cannot satisfy these prompts at all, and
   the telemetry has to come from a shared pre-warmed tenant instead — which is
   a different architecture, not a configuration change.

Answer (2) before building anything for (1).

## What the traffic should look like

Enough for the prompts above to return something interesting:

- Steady queries against `svc.techcorp.internal` from clients on `10.30.1.0/24`
  and `10.30.2.0/24`
- A visible NXDOMAIN population — one or two clients querying names that do not
  exist, so "which clients generated the most NXDOMAIN" has a clear answer
- Enough query volume that "top queried domains" is not a three-row list

Note that once Part 2's break is applied, `svc.techcorp.internal` queries hitting
the data-centre host genuinely NXDOMAIN. That is real signal the participant can
find, and it is free — it does not need a generator.
