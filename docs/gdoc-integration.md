Google Docs API integration — repeatable runbook

How to give a headless host the ability to read and rewrite Google Docs in place. Written after doing it once; the dead ends below were each tested, not assumed, so you do not have to repeat them.

Tool: ~/aws/scripts/gdoc.py on the jumpbox.

WHAT DOES NOT WORK (do not retry these)

1. The claude.ai Google Drive connector. It reads documents fine, but its update_file call accepts only `title` and `parentId`. There is no content field, so it cannot edit a body. Creating a second doc for someone to paste over the first is not an update.

2. OAuth device flow ("TVs and Limited Input devices"). Convenient, and wrong. The device endpoint refuses the scopes that matter:
     documents    -> "Invalid device flow scope"
     drive        -> "Invalid device flow scope"
     drive.file   -> accepted
   drive.file is a legitimate Docs API scope, but it only reaches files the OAuth client itself created. An existing user-authored document answers 404, not 403, because Google hides the existence of files outside the grant. Verified against a real doc.

3. Programmatic OAuth client creation. `gcloud iap oauth-clients create` exists but is locked to IAP usage and internal Workspace brands. It cannot mint a client for Docs. Creating the client is the one genuinely manual step.

4. Service accounts, for personal Gmail. A service account cannot reach a personal account's files without a per-file share, and domain-wide delegation requires Workspace.

SETUP (once per Google account)

Step 1 — create the OAuth client. GCP console, the project that already holds your other OAuth clients:
    console.cloud.google.com/apis/credentials
    Create credentials -> OAuth client ID -> Application type: DESKTOP APP
Enable the Docs API while you are there:
    console.cloud.google.com/apis/library/docs.googleapis.com
Skipping this yields a SERVICE_DISABLED error much later, at first write, which is a confusing place to discover it.

Step 2 — store the client:
    aws secretsmanager create-secret --name google-docs-oauth-desktop \
      --region us-west-1 \
      --secret-string '{"client_id":"...","client_secret":"..."}'

Step 3 — authorize. Two commands, one human click:
    python3 scripts/gdoc.py auth-url
Open the printed URL, approve, and the browser lands on a localhost page that FAILS TO LOAD. That is expected, not an error: nothing is listening, and the authorization code is in the address bar. Copy the value between `code=` and `&`, then:
    python3 scripts/gdoc.py auth-code '<code>'
The refresh token is stored as google-docs-oauth-token and reused indefinitely. Codes are single-use and expire in minutes; if the exchange fails, just re-run auth-url.

THE WRITE PROCEDURE

Read, replace, append, create:

    python3 scripts/gdoc.py read   <docId>
    python3 scripts/gdoc.py write  <docId> <file>
    python3 scripts/gdoc.py append <docId> <file>
    python3 scripts/gdoc.py create <title> [file]

The docId is the long string in the document URL, between /d/ and /edit.

Always back up before overwriting, because write replaces the entire body:

    python3 scripts/gdoc.py read <docId> > backup.txt

Two traps in the Docs content model, both handled inside the tool but worth knowing if you write your own: the body's trailing newline cannot be deleted, so a full replace deletes the range [1, endIndex-1) rather than [1, endIndex); and an empty document has endIndex 2 with nothing to delete at all, where issuing the delete anyway returns 400. Appending inserts at endIndex-1, before that same final newline.

Writes are verified by reading the document back and comparing, rather than trusting the 200.

FORMATTING

insertText inserts plain text. Markdown is not interpreted: "# Heading" stays literal. For real headings, bold, or tables, send additional batchUpdate requests (updateParagraphStyle with NAMED_STYLE, updateTextStyle, insertTable) against index ranges after the text is in. Plain text is usually the right trade.

ADDING ANOTHER GOOGLE API LATER

The stored refresh token is scoped to documents only. Another API (Sheets, Calendar) needs its own consent covering that scope. Re-run auth-url with the extra scope appended and approve again; the new refresh token supersedes the old. Keep the scope list minimal rather than requesting drive wholesale, so a leaked token cannot read every file in the account.

SECURITY

The refresh token can edit any document the authorizing account can. It lives in Secrets Manager, never on disk or in source control. The client secret stays on the jumpbox. Revoke at myaccount.google.com/permissions, which invalidates the refresh token immediately; rotating the client secret in the console does the same.
