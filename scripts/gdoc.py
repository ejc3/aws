#!/usr/bin/env python3
"""gdoc.py -- read and REWRITE Google Docs in place, from a headless host.

WHY THIS EXISTS. The claude.ai Google Drive connector can read a document but its
update_file call takes only `title` and `parentId` -- there is no content field, so it
cannot edit a document's body. Creating a second document and asking someone to paste it
over the first is not an update. This talks to the Google Docs API directly
(documents.batchUpdate), which does edit in place.

AUTH, AND WHY IT IS A DEVICE FLOW. The documents belong to a personal Google account, so a
service account cannot reach them without being granted access per file, and domain-wide
delegation needs Workspace. That leaves a user credential. A device flow is the only
browserless-for-the-agent option: the human opens one short URL once, approves, and the
resulting refresh token lives in Secrets Manager and is reused forever after.

The one manual step is creating the OAuth client, and it is manual because Google offers no
way around it -- verified, not assumed:

  * the existing cloudflare-google-idp client is a Web client, and the device endpoint
    answers: "Only clients of type 'TVs and Limited Input devices' can use the OAuth 2.0
    flow for TV and Limited-Input Device Applications"
  * gcloud iap oauth-clients create is locked to IAP usage and internal brands, so it
    cannot mint a device client for Docs

  gdoc.py auth                      one-time: device flow, stores the refresh token
  gdoc.py read   <docId>            print the document as plain text
  gdoc.py write  <docId> <file>     REPLACE the whole body with the file's contents
  gdoc.py append <docId> <file>     append the file's contents to the end
  gdoc.py create <title> [file]     create a document, optionally filled
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REGION = "us-west-1"
CLIENT_SECRET = "google-docs-oauth-client"  # {client_id, client_secret}
TOKEN_SECRET = "google-docs-oauth-token"    # {refresh_token}
# Device flow will NOT accept `documents` or `drive` -- both return
# "Invalid device flow scope". drive.file is accepted and IS a valid Docs API scope, but it
# only reaches files this OAuth client created, so an existing user-authored document may
# be invisible to it. Verified by testing each scope against the device endpoint.
SCOPE = "https://www.googleapis.com/auth/drive.file"
DESKTOP_SECRET = "google-docs-oauth-desktop"  # {client_id, client_secret}
DESKTOP_SCOPE = "https://www.googleapis.com/auth/documents"
LOOPBACK = "http://localhost"
PKCE_PATH = "/home/ubuntu/.gdoc-pkce"


def sm_get(name):
    """Read a Secrets Manager secret, or None when it does not exist yet."""
    p = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", name,
         "--region", REGION, "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return json.loads(p.stdout.strip())


def sm_put(name, payload, description):
    """Create or update a secret. Create first, fall back to put on AlreadyExists."""
    body = json.dumps(payload)
    p = subprocess.run(
        ["aws", "secretsmanager", "create-secret", "--name", name, "--region", REGION,
         "--description", description, "--secret-string", body],
        capture_output=True, text=True)
    if p.returncode == 0:
        return
    p = subprocess.run(
        ["aws", "secretsmanager", "put-secret-value", "--secret-id", name,
         "--region", REGION, "--secret-string", body],
        capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"could not store {name}: {p.stderr.strip()[:200]}")


def post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def api(method, url, token, payload=None):
    """Call a Google API with a bearer token; exit with the server's own message on error."""
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        sys.exit(f"{method} {url.split('?')[0]} -> {e.code}\n{detail}")


def access_token():
    """Exchange the stored refresh token for a short-lived access token."""
    tok = sm_get(TOKEN_SECRET)
    # Refresh with the SAME client that minted the token, or Google answers invalid_grant.
    client = sm_get(DESKTOP_SECRET if (tok or {}).get("kind") == "desktop" else CLIENT_SECRET)
    if not client:
        sys.exit("missing OAuth client secret; see the header of this file")
    if not tok:
        sys.exit(f"missing secret {TOKEN_SECRET}; run: gdoc.py auth")
    r = post("https://oauth2.googleapis.com/token", {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    })
    if "access_token" not in r:
        sys.exit(f"token refresh failed: {json.dumps(r)[:300]}\nre-run: gdoc.py auth")
    return r["access_token"]


def cmd_auth():
    client = sm_get(CLIENT_SECRET)
    if not client:
        sys.exit(
            f"Create the OAuth client first, then store it:\n"
            f"  GCP console -> APIs & Services -> Credentials -> Create credentials\n"
            f"  -> OAuth client ID -> type 'TVs and Limited Input devices'\n"
            f"  aws secretsmanager create-secret --name {CLIENT_SECRET} --region {REGION} \\\n"
            f"    --secret-string '{{\"client_id\":\"...\",\"client_secret\":\"...\"}}'")
    r = post("https://oauth2.googleapis.com/device/code",
             {"client_id": client["client_id"], "scope": SCOPE})
    if "device_code" not in r:
        sys.exit(f"device code request failed: {json.dumps(r)[:300]}")
    print(f"\n  Open: {r['verification_url']}\n  Code: {r['user_code']}\n", flush=True)

    # Poll until approved. slow_down means Google wants a longer interval, and ignoring it
    # gets the request rejected outright.
    interval = r.get("interval", 5)
    deadline = time.time() + r.get("expires_in", 1800)
    while time.time() < deadline:
        time.sleep(interval)
        t = post("https://oauth2.googleapis.com/token", {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "device_code": r["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        err = t.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            sys.exit(f"authorization failed: {err} {t.get('error_description','')}")
        if "refresh_token" not in t:
            sys.exit("no refresh_token returned; ensure the client is a device client")
        sm_put(TOKEN_SECRET, {"refresh_token": t["refresh_token"]},
               "Google Docs API refresh token (device flow, scope: documents)")
        print(f"  stored refresh token in {TOKEN_SECRET}")
        return
    sys.exit("timed out waiting for approval")


def cmd_auth_url():
    """Print the consent URL for a DESKTOP client (loopback redirect).

    Device flow cannot be used for real document access: Google refuses both `documents`
    and `drive` as device-flow scopes, and the one Drive scope it does accept,
    drive.file, only reaches files this client itself created -- an existing user-authored
    document answers 404, because Google hides the existence of files outside the grant.

    The desktop client accepts the full `documents` scope. Its redirect goes to
    http://localhost, which will not load for a browser on another machine -- that is
    fine and expected. The authorization code is in the address bar, and the caller
    pastes it back via `auth-code`. PKCE is included because Google requires it for
    newer installed-app clients and it costs nothing when it is merely recommended.
    """
    client = sm_get(DESKTOP_SECRET)
    if not client:
        sys.exit(
            f"Create a DESKTOP client, then store it:\n"
            f"  GCP console -> Credentials -> Create credentials -> OAuth client ID\n"
            f"  -> Application type: 'Desktop app'\n"
            f"  aws secretsmanager create-secret --name {DESKTOP_SECRET} --region {REGION} \\\n"
            f"    --secret-string '{{\"client_id\":\"...\",\"client_secret\":\"...\"}}'")
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with open(PKCE_PATH, "w") as fh:
        fh.write(verifier)
    os.chmod(PKCE_PATH, 0o600)
    q = urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": LOOPBACK,
        "response_type": "code",
        "scope": DESKTOP_SCOPE,
        "access_type": "offline",
        "prompt": "consent",           # force a refresh_token even on re-authorization
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print(f"\n  Open:\n  https://accounts.google.com/o/oauth2/v2/auth?{q}\n")
    print("  Approve, then copy the `code=` value from the address bar of the page")
    print("  that fails to load, and run:  gdoc.py auth-code <code>\n")


def cmd_auth_code(code):
    client = sm_get(DESKTOP_SECRET)
    if not client:
        sys.exit(f"missing secret {DESKTOP_SECRET}")
    try:
        with open(PKCE_PATH) as fh:
            verifier = fh.read().strip()
    except OSError:
        sys.exit("no pending PKCE verifier; run: gdoc.py auth-url")
    # The browser percent-encodes the code; undo that or the exchange fails as invalid_grant.
    t = post("https://oauth2.googleapis.com/token", {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "code": urllib.parse.unquote(code),
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": LOOPBACK,
    })
    if "refresh_token" not in t:
        sys.exit(f"exchange failed: {json.dumps(t)[:300]}\n"
                 "codes are single-use and short-lived; re-run auth-url for a fresh one")
    sm_put(TOKEN_SECRET, {"refresh_token": t["refresh_token"], "kind": "desktop"},
           "Google Docs API refresh token (desktop loopback flow, scope: documents)")
    os.unlink(PKCE_PATH)
    print(f"  stored refresh token in {TOKEN_SECRET}")


def cmd_create(title, path=None):
    """Create a document and optionally fill it. Needs the `documents` scope.

    documents.create takes only a title -- there is no way to supply a body in the same
    call -- so content always arrives via a second batchUpdate.
    """
    token = access_token()
    doc = api("POST", "https://docs.googleapis.com/v1/documents", token, {"title": title})
    doc_id = doc["documentId"]
    print(f"  created {doc_id}")
    print(f"  https://docs.google.com/document/d/{doc_id}/edit")
    if path:
        cmd_write(doc_id, path)


def doc_text(doc):
    """Flatten a Docs document into plain text."""
    out = []
    for el in doc.get("body", {}).get("content", []):
        for run in el.get("paragraph", {}).get("elements", []):
            out.append(run.get("textRun", {}).get("content", ""))
    return "".join(out)


def cmd_read(doc_id):
    doc = api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}", access_token())
    sys.stdout.write(doc_text(doc))


def body_end(doc):
    """Index just past the last character, per the Docs content model."""
    content = doc.get("body", {}).get("content", [])
    return content[-1].get("endIndex", 1) if content else 1


def cmd_write(doc_id, path, append=False):
    with open(path) as fh:
        text = fh.read()
    token = access_token()
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    doc = api("GET", url, token)
    end = body_end(doc)

    requests = []
    if append:
        # Insert before the body's final newline, which is not a deletable position.
        requests.append({"insertText": {"location": {"index": max(1, end - 1)}, "text": text}})
    else:
        # The trailing newline of the body segment cannot be deleted, so the deletable
        # range stops one short of endIndex. A document with only that newline has
        # end == 2 and nothing to delete; issuing the delete anyway is a 400.
        if end > 2:
            requests.append({"deleteContentRange":
                             {"range": {"startIndex": 1, "endIndex": end - 1}}})
        requests.append({"insertText": {"location": {"index": 1}, "text": text}})

    api("POST", f"{url}:batchUpdate", token, {"requests": requests})

    # Verify rather than trust the 200: read it back and compare length.
    after = doc_text(api("GET", url, token))
    print(f"  wrote {len(text)} chars; document now {len(after)} chars")
    if not append and text.strip() and text.strip()[:40] not in after:
        print("  WARNING: readback does not contain the start of what was written")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "auth":
        cmd_auth()
    elif cmd == "create" and len(sys.argv) in (3, 4):
        cmd_create(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif cmd == "auth-url":
        cmd_auth_url()
    elif cmd == "auth-code" and len(sys.argv) == 3:
        cmd_auth_code(sys.argv[2])
    elif cmd == "read" and len(sys.argv) == 3:
        cmd_read(sys.argv[2])
    elif cmd in ("write", "append") and len(sys.argv) == 4:
        cmd_write(sys.argv[2], sys.argv[3], append=(cmd == "append"))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
