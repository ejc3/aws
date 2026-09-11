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
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REGION = "us-west-1"
CLIENT_SECRET = "google-docs-oauth-client"  # {client_id, client_secret}
TOKEN_SECRET = "google-docs-oauth-token"    # {refresh_token}
SCOPE = "https://www.googleapis.com/auth/documents"


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
    client = sm_get(CLIENT_SECRET)
    if not client:
        sys.exit(f"missing secret {CLIENT_SECRET}; see the header of this file")
    tok = sm_get(TOKEN_SECRET)
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
    elif cmd == "read" and len(sys.argv) == 3:
        cmd_read(sys.argv[2])
    elif cmd in ("write", "append") and len(sys.argv) == 4:
        cmd_write(sys.argv[2], sys.argv[3], append=(cmd == "append"))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
