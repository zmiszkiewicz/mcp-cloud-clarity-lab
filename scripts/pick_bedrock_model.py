#!/usr/bin/env python3
"""
Choose which Claude model the assistant runs on, by asking Bedrock.

WHY DISCOVER RATHER THAN HARDCODE
---------------------------------
Bedrock model availability varies by account and by region, and the id you must
pass to InvokeModel is not always the id ListFoundationModels returns. Newer
Anthropic models are frequently gated behind a *cross-region inference profile*
— `us.anthropic.claude-...` rather than `anthropic.claude-...` — and calling the
bare model id then fails with a message about on-demand throughput that does not
mention inference profiles at all.

Hardcoding an id means a track that works in one account and dies in another,
with a confusing error. Asking Bedrock what it actually has takes one API call at
setup and prints the answer into the log.

SELECTION
---------
Preference is by family substring, most preferred first, from
BEDROCK_MODEL_PREFERENCE (default "sonnet"). Within a family the newest is
chosen, judged by the date in the model id, then by the version suffix.

An explicit BEDROCK_MODEL_ID always wins and skips discovery entirely — that is
the escape hatch when you know exactly what you want.

Usage:
    python3 pick_bedrock_model.py            print the chosen id
    python3 pick_bedrock_model.py --list     show every Anthropic model available
"""

import argparse
import os
import re
import sys


# THE PROFILE PREFIX MUST MATCH THE REGION.
#
# A cross-region inference profile is geography-scoped, and its id carries that
# geography as a prefix: `us.anthropic.…` only resolves in a US region,
# `eu.anthropic.…` only in an EU one. Passing the wrong one produces
#
#     400 The provided model identifier is invalid.
#
# which says nothing about geographies and reads like the model does not exist.
# That is exactly what happened when the lab VPC moved to eu-central-1 while
# the model stayed pinned to a `us.` profile.
GEO_PREFIXES = (
    ("us-",   "us."),
    ("eu-",   "eu."),
    ("ap-",   "apac."),
    ("ca-",   "ca."),
    ("sa-",   "sa."),
)


def geo_prefix(region):
    """The inference-profile prefix for a region, or '' if none is known."""
    for start, prefix in GEO_PREFIXES:
        if region.startswith(start):
            return prefix
    return ""


# Bedrock's region, which is NOT necessarily the lab VPC's — see
# track_scripts/setup-shell. BEDROCK_REGION wins; AWS_DEFAULT_REGION is the
# fallback for standalone runs.
REGION = os.environ.get("BEDROCK_REGION") or \
    os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Where to go if this region has no invokable Anthropic model at all. Bedrock's
# region is independent of everything else the lab does, so falling back costs
# nothing but a little latency — and an assistant that works in the wrong
# region beats one that does not work in the right one.
FALLBACK_REGION = os.environ.get("BEDROCK_FALLBACK_REGION", "us-east-1")

# The model family and version we want, WITHOUT a geography prefix. The prefix
# is added per-region so the same preference works anywhere.
#
# Pinned rather than left to discovery because Claude Code's own default on
# Bedrock is Opus 5 for the primary model and Sonnet 4.5 for the `sonnet`
# alias. Unpinned, this lab would silently run a different model at a higher
# rate — which is also how a run ended up on `eu.anthropic.claude-sonnet-5`,
# an id that does not exist.
PREFERRED_MODEL_SUFFIX = os.environ.get(
    "BEDROCK_PREFERRED_MODEL", "anthropic.claude-sonnet-4-6"
)


def preferred_for(region):
    """The preferred model id, prefixed for this region's geography."""
    explicit = os.environ.get("BEDROCK_PREFERRED_MODEL_ID")
    if explicit:
        return explicit
    return f"{geo_prefix(region)}{PREFERRED_MODEL_SUFFIX}"


PREFERRED_MODEL_ID = preferred_for(REGION)

# Family to fall back to if the preferred id is not invokable here.
DEFAULT_PREFERENCE = os.environ.get("BEDROCK_MODEL_PREFERENCE", "sonnet")

# Last resort when Bedrock cannot be queried at all.
FALLBACK_MODEL_ID = os.environ.get(
    "BEDROCK_FALLBACK_MODEL_ID", PREFERRED_MODEL_ID
)


def _client(service, region=None):
    import boto3
    return boto3.client(service, region_name=region or REGION)


def _version_key(model_id):
    """
    Sort key that puts the newest model first.

    Ordering these by the embedded DATE is wrong, and wrong in a way that picks
    the older model: `claude-sonnet-4-5-20250929-v1:0` carries a date while
    `claude-sonnet-4-6` does not, so a date-first comparison ranks 4.5 above
    4.6. The family version is the real signal; the date only breaks ties
    within one version.

    So: strip the date and the `-vN` suffix, read the major/minor pair out of
    what remains, and use the date afterwards.
    """
    tail = model_id.split("anthropic.")[-1]

    date = re.search(r"(20\d{6})", tail)
    stripped = re.sub(r"20\d{6}", "", re.sub(r"-v\d+.*$", "", tail))

    pair = re.search(r"-(\d+)-(\d+)", stripped)
    if pair:
        major, minor = int(pair.group(1)), int(pair.group(2))
    else:
        single = re.search(r"-(\d+)\b", stripped)
        major, minor = (int(single.group(1)), 0) if single else (0, 0)

    return (major, minor, int(date.group(1)) if date else 0, model_id)


def available_models(region=None):
    """
    Every Anthropic text model this account can invoke in this region, as
    (invoke_id, summary) — where invoke_id is already the inference profile id
    when the model requires one.
    """
    bedrock = _client("bedrock", region)

    try:
        summaries = bedrock.list_foundation_models(
            byProvider="anthropic", byOutputModality="TEXT"
        )["modelSummaries"]
    except Exception as exc:                            # noqa: BLE001
        raise RuntimeError(f"could not list Bedrock foundation models: {exc}")

    # Inference profiles, keyed by the underlying model they front. A model that
    # only supports INFERENCE_PROFILE cannot be invoked by its bare id.
    profiles = {}
    try:
        paginator = bedrock.get_paginator("list_inference_profiles")
        for page in paginator.paginate():
            for profile in page.get("inferenceProfileSummaries", []):
                for model in profile.get("models", []):
                    arn = model.get("modelArn", "")
                    base = arn.rsplit("/", 1)[-1] if arn else ""
                    if base:
                        profiles.setdefault(base, profile["inferenceProfileId"])
    except Exception as exc:                            # noqa: BLE001
        print(f"⚠️  could not list inference profiles ({exc}); will only "
              f"consider models invokable by their bare id", file=sys.stderr)

    usable = []
    for summary in summaries:
        model_id = summary["modelId"]
        types = summary.get("inferenceTypesSupported", [])
        lifecycle = (summary.get("modelLifecycle") or {}).get("status")

        if lifecycle == "LEGACY":
            continue

        if "ON_DEMAND" in types:
            usable.append((model_id, summary))
        elif "INFERENCE_PROFILE" in types and model_id in profiles:
            # Invoke through the profile, not the bare id.
            usable.append((profiles[model_id], summary))

    return usable


def _choose_in(region, preference=None):
    """
    The best model id in ONE region, or (None, why-not).

    Verify-then-fall-back rather than pure discovery: we know which model this
    lab wants, so the job is confirming the account can invoke it here and
    choosing sensibly when it cannot.
    """
    wanted = preferred_for(region)

    try:
        usable = available_models(region)
    except Exception as exc:                            # noqa: BLE001
        return None, f"could not query Bedrock in {region} ({exc})"

    if not usable:
        return None, f"no invokable Anthropic models in {region}"

    ids = [invoke_id for invoke_id, _ in usable]

    if wanted in ids:
        return wanted, f"{wanted} is invokable in {region}"

    families = [p.strip().lower() for p in
                (preference or DEFAULT_PREFERENCE).split(",") if p.strip()]
    for family in families:
        matches = [i for i in ids if family in i.lower()]
        if matches:
            best = sorted(matches, key=_version_key, reverse=True)[0]
            return best, (f"{wanted} is not invokable in {region}; using the "
                          f"newest '{family}' there instead: {best}")

    best = sorted(ids, key=_version_key, reverse=True)[0]
    return best, (f"no {families} model in {region}; using the newest "
                  f"Anthropic model available there: {best}")


def choose(preference=None):
    """
    The model id to pin, the region to invoke it in, and why.

    Tries the configured Bedrock region first, then FALLBACK_REGION. The
    fallback exists because Bedrock's region is independent of everything else
    the lab does — the model calls leave from the lab container and care
    nothing about where the VPC is — so an account with no EU model access
    should quietly use a US one rather than leave the participant with an
    assistant that returns 400 on every prompt.
    """
    explicit = os.environ.get("BEDROCK_MODEL_ID")
    if explicit:
        return explicit, REGION, "BEDROCK_MODEL_ID is set explicitly"

    model, why = _choose_in(REGION, preference)
    if model:
        return model, REGION, why

    if FALLBACK_REGION and FALLBACK_REGION != REGION:
        print(f"⚠️  {why}; trying {FALLBACK_REGION}", file=sys.stderr)
        model, fallback_why = _choose_in(FALLBACK_REGION, preference)
        if model:
            return model, FALLBACK_REGION, (
                f"{why}, so Bedrock will run in {FALLBACK_REGION} instead "
                f"— {fallback_why}"
            )
        why = f"{why}; and {fallback_why}"

    return FALLBACK_MODEL_ID, REGION, f"{why}. Using the pin unverified."


def main():
    parser = argparse.ArgumentParser(
        description="Pick the Bedrock model the assistant should run on."
    )
    parser.add_argument("--list", action="store_true",
                        help="show every invokable Anthropic model and exit")
    args = parser.parse_args()

    if args.list:
        try:
            usable = available_models()
        except Exception as exc:                        # noqa: BLE001
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print(f"Anthropic models invokable in {REGION}:\n")
        for invoke_id, summary in sorted(usable, key=lambda p: _version_key(p[0]),
                                         reverse=True):
            via = "" if invoke_id == summary["modelId"] else "  (via inference profile)"
            print(f"  {invoke_id}{via}")
        return 0

    model_id, region, why = choose()
    # stdout is "<model id> <region>" and nothing else, so a caller can read
    # both with `read`. Everything explanatory goes to stderr.
    print(f"{model_id} {region}")
    print(f"   {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
