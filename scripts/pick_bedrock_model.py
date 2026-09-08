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


# Families in preference order. Substring match against the model id, so
# "sonnet" matches whatever the newest Sonnet happens to be called.
DEFAULT_PREFERENCE = os.environ.get("BEDROCK_MODEL_PREFERENCE", "sonnet")

# Used only when discovery cannot run at all (no credentials, no bedrock
# permission). Deliberately a Sonnet: this track's work is tool-calling against
# two MCP servers, which Sonnet handles well and faster than a larger model.
FALLBACK_MODEL_ID = os.environ.get(
    "BEDROCK_FALLBACK_MODEL_ID", "anthropic.claude-sonnet-5"
)

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _client(service):
    import boto3
    return boto3.client(service, region_name=REGION)


def _date_key(model_id):
    """
    Sort key that puts the newest model first.

    Anthropic Bedrock ids carry a date (`...-20250929-v1:0`) often enough to be
    the most reliable ordering signal. Where there is no date, fall back to the
    trailing version digits, then to the string itself so the order is at least
    stable.
    """
    date = re.search(r"(20\d{6})", model_id)
    version = re.search(r"-v(\d+)", model_id)
    return (
        int(date.group(1)) if date else 0,
        int(version.group(1)) if version else 0,
        model_id,
    )


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
    """The best available model id, and a one-line explanation."""
    explicit = os.environ.get("BEDROCK_MODEL_ID")
    if explicit:
        return explicit, f"BEDROCK_MODEL_ID is set explicitly"

    preference = [p.strip().lower() for p in
                  (preference or DEFAULT_PREFERENCE).split(",") if p.strip()]

    try:
        usable = available_models()
    except Exception as exc:                            # noqa: BLE001
        return FALLBACK_MODEL_ID, f"discovery failed ({exc}); using the fallback"

    if not usable:
        return FALLBACK_MODEL_ID, "no invokable Anthropic models found; using the fallback"

    for family in preference:
        matches = [invoke_id for invoke_id, _ in usable
                   if family in invoke_id.lower()]
        if matches:
            best = sorted(matches, key=_date_key, reverse=True)[0]
            return best, f"newest '{family}' model invokable in {REGION}"

    best = sorted((i for i, _ in usable), key=_date_key, reverse=True)[0]
    return best, (f"no model matched {preference}; using the newest Anthropic "
                  f"model available")


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
        for invoke_id, summary in sorted(usable, key=lambda p: _date_key(p[0]),
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
