#!/usr/bin/env python3
"""
Thin CSP REST client shared by seeding, breaking, checking and teardown.

The auth dance is the one every script in this estate uses (user_provision.py,
deploy_api_key.py):

    POST /v2/session/users/sign_in      -> JWT for the admin's home account
    POST /v2/session/account_switch     -> JWT scoped to the participant sandbox

Everything after that carries `Authorization: Bearer <jwt>`.

The lab ALSO needs to make calls as the participant's Service API key, because
Part 4 has the learner attempt a write with the read-only key and we want the
check to be able to reproduce that. Those calls use
`Authorization: Token <key>` — the same header the Infoblox MCP Server uses.
`CspClient.from_service_key()` builds that variant.
"""

import json
import os
import random
import re
import sys
import time

import requests

import lab_config as cfg
from lab_config import LabTodo  # re-exported so callers import it from one place


RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# CSP rejects a create that includes a server-derived field with, verbatim:
#   {"error":[{"message":"The 'parent' field is read-only and cannot be provided."}]}
#
# This matters more than it looks. Infoblox's OpenAPI-generated Go client — the
# thing this lab uses as its reference for payload shapes — marks several such
# fields as ordinary optional writables. `parent` on both FixedAddress and Range
# is documented that way and is NOT accepted by the live API. So the generated
# client is a guide to what fields EXIST, not to which ones you may send.
#
# Rather than discover each one on a separate failed track start, a request that
# trips this strips the offending field and retries. The field is server-derived
# by definition — that is what read-only means here — so dropping it cannot
# change where the object lands.
READONLY_FIELD_RE = re.compile(
    r"The '([^']+)' field is read-only", re.IGNORECASE
)
MAX_READONLY_STRIPS = 6


class CspError(RuntimeError):
    """A CSP call failed in a way retrying will not fix."""

    def __init__(self, method, url, status, body):
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:400]}")


def object_url(collection, obj_id):
    """
    The URL for one object, given its collection path and its id.

    ALWAYS USE THIS. Never write `cfg.path("dns_auth_zone") + f"/{obj['id']}"`.

    CSP does not return bare UUIDs. An object's `id` is a *resource identifier*
    — a relative path like `dns/auth_zone/d5c39dcd-...` — which is exactly what
    you must send back in a request BODY when referencing it (`{"zone": <id>}`,
    `{"host": <id>}`). But concatenating it onto its own collection path
    produces `/api/ddi/v1/dns/auth_zone/dns/auth_zone/d5c39dcd-...`, and CSP
    answers that with **HTTP 501 Not Implemented** — which reads like an
    unsupported verb rather than a malformed path, and cost a track start.

    So: full resource identifier in bodies, last segment in URLs. This function
    is the second half of that rule, and it accepts either form so it is safe to
    call on an id you have already stripped.
    """
    return f"{collection.rstrip('/')}/{str(obj_id).rstrip('/').rsplit('/', 1)[-1]}"


def read_state(key, required=True):
    """Read one of the state files setup-shell wrote into SCRIPT_DIR."""
    filename = cfg.STATE_FILES[key]
    for directory in (os.getcwd(), cfg.SCRIPT_DIR):
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            with open(candidate) as handle:
                value = handle.read().strip()
            if value:
                return value
    if required:
        raise SystemExit(
            f"❌ {filename} not found or empty. The track setup did not complete — "
            f"restart the track."
        )
    return None


def write_state(key, value, secret=False):
    """Write a state file next to the other lifecycle artefacts."""
    filename = cfg.STATE_FILES[key]
    target = os.path.join(cfg.SCRIPT_DIR if os.path.isdir(cfg.SCRIPT_DIR) else ".",
                          filename)
    with open(target, "w") as handle:
        handle.write(str(value).strip())
    os.chmod(target, 0o600 if secret else 0o644)
    return target


class CspClient:
    """Authenticated CSP session, scoped to one account."""

    def __init__(self, base_url=None, headers=None):
        self.base_url = (base_url or cfg.CSP_URL).rstrip("/")
        self.session = requests.Session()
        self.headers = headers or {"Content-Type": "application/json"}

    # ---------------------------------------------------------------- auth ---

    @classmethod
    def as_admin(cls, account_id=None):
        """
        Sign in with the Instruqt-secret admin credentials, then switch into the
        participant's sandbox account. This is the identity that seeds, breaks
        and verifies — never the learner's.
        """
        cfg.require_env()
        client = cls()

        resp = client.session.post(
            f"{client.base_url}{cfg.path('signin')}",
            json={"email": cfg.INFOBLOX_EMAIL, "password": cfg.INFOBLOX_PASSWORD},
            timeout=(5, 30),
        )
        resp.raise_for_status()
        client._set_jwt(resp.json()["jwt"])

        account_id = account_id or read_state("sandbox_id")
        client.switch_account(account_id)
        return client

    def switch_account(self, account_id):
        resp = self.session.post(
            f"{self.base_url}{cfg.path('account_switch')}",
            headers=self.headers,
            json={"id": f"identity/accounts/{account_id}"},
            timeout=(5, 30),
        )
        resp.raise_for_status()
        self._set_jwt(resp.json()["jwt"])
        self.account_id = account_id
        return self

    @classmethod
    def from_service_key(cls, api_key, base_url=None):
        """
        Build a client that authenticates the way the Infoblox MCP Server does:
        `Authorization: Token <service api key>`.

        Used by the Part 4 check to confirm the read-only key really is refused
        a write, and by the Part 1 check to confirm the key works at all.
        """
        return cls(base_url=base_url, headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        })

    def _set_jwt(self, jwt):
        self.jwt = jwt
        self.headers["Authorization"] = f"Bearer {jwt}"

    # ------------------------------------------------------------ requests ---

    def request(self, method, path, *, params=None, json_body=None,
                expect=None, raw=False):
        """
        One CSP call, with retry on the transient statuses.

        `expect` is an iterable of acceptable status codes. Anything outside it
        raises CspError. Pass expect={200, 404} when a 404 is a legitimate
        answer — teardown relies on that.
        """
        url = f"{self.base_url}{path}"
        expect = set(expect) if expect else {200, 201, 204}
        last = None
        stripped = []

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(
                    method, url, headers=self.headers, params=params,
                    json=json_body, timeout=(5, 60),
                )
            except requests.RequestException as exc:
                last = str(exc)
                time.sleep(min(2 ** attempt + random.random(), 20))
                continue

            if resp.status_code in expect:
                if raw or not resp.content:
                    return resp
                try:
                    return resp.json()
                except ValueError:
                    return resp

            if resp.status_code in RETRY_STATUSES:
                last = f"HTTP {resp.status_code}"
                time.sleep(min(2 ** attempt + random.random(), 20))
                continue

            # A server-derived field we should not have sent. Drop it and retry
            # rather than failing the whole track start over a payload the
            # generated client said was fine. See READONLY_FIELD_RE.
            if resp.status_code == 400 and isinstance(json_body, dict):
                match = READONLY_FIELD_RE.search(resp.text)
                field = match.group(1) if match else None
                if (field and field in json_body
                        and len(stripped) < MAX_READONLY_STRIPS):
                    json_body.pop(field)
                    stripped.append(field)
                    print(f"⚠️  CSP rejected {method} {path}: '{field}' is "
                          f"read-only. Dropping it and retrying.\n"
                          f"    Remove it from the payload permanently — the "
                          f"OpenAPI client documents it as writable, and it is "
                          f"not.", flush=True)
                    continue

            # CSP answers an unrecognised path with 501 Not Implemented, which
            # reads like an unsupported HTTP verb rather than a bad URL. The
            # usual cause is a doubled collection segment from concatenating a
            # resource identifier onto its own collection path.
            if resp.status_code == 501:
                segments = [s for s in url.split("?")[0].split("/") if s]
                doubled = next(
                    (segments[i] for i in range(len(segments) - 2)
                     if segments[i] == segments[i + 2]), None)
                if doubled:
                    raise CspError(
                        method, url, resp.status_code,
                        resp.text + f"  <-- the path repeats '{doubled}', so it "
                        "is malformed rather than unimplemented. A CSP `id` is "
                        "a resource identifier like 'dns/auth_zone/<uuid>', not "
                        "a bare UUID: send it whole in a request BODY, but use "
                        "csp_client.object_url() to build a URL from it.",
                    )

            # CSP's 400 for a missing or malformed timestamp is
            # `HTTP interceptor error: invalid datetime or duration` — it names
            # neither the field nor the endpoint's expectation, and it reads
            # like a transport fault rather than a payload one. It cost a track
            # start once; annotate it so it never costs another.
            if (resp.status_code == 400
                    and "invalid datetime or duration" in resp.text):
                raise CspError(
                    method, url, resp.status_code,
                    resp.text + "  <-- a required RFC3339 timestamp is missing "
                    "or malformed in this request body (e.g. expires_at on "
                    "/v2/current_api_keys). CSP wants "
                    "YYYY-MM-DDTHH:MM:SS.000Z — note the literal Z and the "
                    "millisecond precision; datetime.isoformat() gives neither.",
                )

            raise CspError(method, url, resp.status_code, resp.text)

        raise CspError(method, url, 0, f"gave up after {MAX_RETRIES} attempts: {last}")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json_body=None, **kw):
        return self.request("POST", path, json_body=json_body, **kw)

    def patch(self, path, json_body=None, **kw):
        return self.request("PATCH", path, json_body=json_body, **kw)

    def put(self, path, json_body=None, **kw):
        return self.request("PUT", path, json_body=json_body, **kw)

    def delete(self, path, **kw):
        kw.setdefault("expect", {200, 204, 404})
        return self.request("DELETE", path, **kw)

    # ------------------------------------------------------------ helpers ---

    def list_results(self, path, params=None):
        """
        GET a collection and return its rows.

        CSP is inconsistent about the envelope: identity endpoints use
        `results`, DDI endpoints use `results` too but singular reads use
        `result`. Handle all three rather than making every caller do it.
        """
        payload = self.get(path, params=params)
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        if isinstance(payload.get("results"), list):
            return payload["results"]
        if isinstance(payload.get("result"), list):
            return payload["result"]
        if isinstance(payload.get("result"), dict):
            return [payload["result"]]
        return []

    def find_by_name(self, path, name, field="name", params=None):
        """First row in a collection whose `field` equals `name`, else None."""
        for row in self.list_results(path, params=params):
            if row.get(field) == name:
                return row
        return None

    def group_id(self, group_name):
        """Resolve a CSP group name to its id. Matches user_provision.py."""
        group = self.find_by_name(cfg.path("groups"), group_name)
        return group["id"] if group else None


# --------------------------------------------------------------------------- #
# Reporting helpers used by every stage script
# --------------------------------------------------------------------------- #

REASON_FILE = "/tmp/cloud_clarity_check_reason.txt"


def clear_reason():
    try:
        os.unlink(REASON_FILE)
    except FileNotFoundError:
        pass


def fail(reason):
    """
    Record a one-sentence, learner-readable reason and exit non-zero.

    check-shell cats this file straight into Instruqt's `fail-message`, so it
    must read as advice, not as a stack trace.
    """
    with open(REASON_FILE, "w") as handle:
        handle.write(reason.strip() + "\n")
    print(f"❌ {reason}", flush=True)
    sys.exit(1)


def ok(message):
    print(f"✅ {message}", flush=True)


def info(message):
    print(f"   {message}", flush=True)


def dump(label, obj):
    """Verbose object dump — only shown in setup logs, never to the learner."""
    print(f"── {label}\n{json.dumps(obj, indent=2)[:2000]}", flush=True)
