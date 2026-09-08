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


# What we want, unless the account cannot invoke it. The `us.` prefix is the
# cross-region inference profile — Claude Code needs a profile id here, not a
# bare `anthropic.…` model id, which fails with an on-demand-throughput error.
#
# Pinned rather than left to discovery because Claude Code's own default on
# Bedrock is Opus 5 for the primary model and Sonnet 4.5 for the `sonnet` alias.
# Unpinned, this lab would silently run a different model at a higher rate.
PREFERRED_MODEL_ID = os.environ.get(
    "BEDROCK_PREFERRED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)

# Family to fall back to if the preferred id is not invokable here.
DEFAULT_PREFERENCE = os.environ.get("BEDROCK_MODEL_PREFERENCE", "sonnet")

# Last resort when Bedrock cannot be queried at all.
FALLBACK_MODEL_ID = os.environ.get(
    "BEDROCK_FALLBACK_MODEL_ID", PREFERRED_MODEL_ID
)

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _client(service):
    import boto3
    return boto3.client(service, region_name=REGION)


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


def available_models():
    """
    Every Anthropic text model this account can invoke in this region, as
    (invoke_id, summary) — where invoke_id is already the inference profile id
    when the model requires one.
    """
    bedrock = _client("bedrock")

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


def choose(preference=None):
    """
    The model id to pin, and a one-line explanation.

    Verify-then-fall-back, rather than pure discovery: we know which model this
    lab wants, so the job is confirming the account can invoke it and choosing
    sensibly when it cannot.
    """
    explicit = os.environ.get("BEDROCK_MODEL_ID")
    if explicit:
        return explicit, "BEDROCK_MODEL_ID is set explicitly"

    try:
        usable = available_models()
    except Exception as exc:                            # noqa: BLE001
        return FALLBACK_MODEL_ID, f"could not query Bedrock ({exc}); using the pin unverified"

    if not usable:
        return FALLBACK_MODEL_ID, "no invokable Anthropic models found; using the pin unverified"

    ids = [invoke_id for invoke_id, _ in usable]

    if PREFERRED_MODEL_ID in ids:
        return PREFERRED_MODEL_ID, f"preferred model is invokable in {REGION}"

    preference = [p.strip().lower() for p in
                  (preference or DEFAULT_PREFERENCE).split(",") if p.strip()]
    for family in preference:
        matches = [i for i in ids if family in i.lower()]
        if matches:
            best = sorted(matches, key=_version_key, reverse=True)[0]
            return best, (f"{PREFERRED_MODEL_ID} is not invokable here; using "
                          f"the newest '{family}' instead")

    best = sorted(ids, key=_version_key, reverse=True)[0]
    return best, (f"neither {PREFERRED_MODEL_ID} nor {preference} is available; "
                  f"using the newest Anthropic model in the account")


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

    model_id, why = choose()
    # stdout is the id and nothing else — setup-shell captures it directly.
    print(model_id)
    print(f"   {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
