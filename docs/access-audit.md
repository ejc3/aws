# Host access audit

`access-audit.tf` and `scripts/access-audit.py` report two kinds of activity to the operator:

- **Registered paths opened on a host.** The agent watches a private list of files and
  reports each open by another process, with the reader's executable, pid, user, command
  line, its process ancestry (pid and start time of each parent) and, when the reader's
  environment carries one, its `SSH_CONNECTION`. That variable is whatever the reader's
  environment says, so treat it as a lead, not proof of the source. It also reports when a
  registered file is modified, replaced or removed, and when its own sensor degrades.
- **Registered principals used anywhere.** Each registered host has an IAM principal whose
  policy denies everything. CloudTrail still records every call it makes, and a rule in each
  of the 17 enabled Regions forwards those calls to the `access-audit` bus in us-west-1.
  This covers every management event and the S3 data events the account trail already
  selects. Data-plane calls the trail does not record (an S3 object read in another bucket,
  a Lambda invoke) are not seen.

Everything that says *which* hosts, files and principals are registered is private. It is
held only in the `access-audit/config` secret and in Terraform state. Nothing in this
repository, its tests or its pull requests names it, and nothing should.

## What fires

| Signal | Where it shows | Alarm |
| --- | --- | --- |
| Registered path opened | operator email (per report, at most 20 per 10 minutes per host), `/security/access-audit` log stream for the host, `AccessAudit/FileRead` | `access-audit-file-read` |
| Registered path changed, sensor degraded or silent | the same three | `access-audit-tamper` |
| Registered principal made an API call | operator email naming caller, action, source IP and user agent; `AWS/Events TriggeredRules` for `access-audit-notify` | `access-audit-principal-use` |
| Agent stopped reporting (stopped, killed, host down) | no heartbeat for 15 minutes | `access-audit-heartbeat-<host>` |
| Principal notification could not be delivered | `AWS/Events FailedInvocations` | `access-audit-delivery` |

The `access-audit` CloudWatch dashboard shows all of them and the latest host reports.

## Sensors

- **Linux:** fanotify permission events. The opener is held only while the agent reads its
  `/proc` entry, then always allowed, so even a reader that exits at once is identified.
  Opens are matched by inode, so a read through a hard link or another mount of the same
  file is reported under the registered path, with `opened_path` naming the one used.
  Files replaced or recreated are re-marked by the integrity check.
- **macOS:** `eslogger open` runs as its own launchd job (`<label>.helper`; the label is
  `local.<service name>` unless the registry sets `launchd_label`) and
  writes into a root-only FIFO the agent reads, so only `/usr/bin/eslogger` needs Full Disk
  Access. Give it that once: System Settings, Privacy & Security, Full Disk Access, **+**,
  Cmd-Shift-G, `/usr/bin/eslogger`. Until it has it, or whenever it stops, atime polling still
  reports reads (without the reader's identity) and the agent sends one `sensor silent`
  report. eslogger does not report processes in its own login session, which matters only
  when testing by hand. eslogger events are matched by path, so a read through a hard link
  under another name is caught by the atime poll (atime is per inode) without the reader's
  identity. Executables in `ignore_executables` are ignored by both.

## Rollout

The secrets are containers first, then values, then the full build, the same order as
other private values in this repository.

1. Merge, then on a jumpbox apply with the default `enable_access_audit = false`. This creates
   only the two empty secret containers and the always-present bus, roles, alarms and dashboard.
2. Write the registry. Read it from a file you then delete; never put it on a command line:

   ```bash
   aws secretsmanager put-secret-value --region us-west-1 --secret-id access-audit/config \
     --secret-string file://registry.json && shred -u registry.json
   ```

   Shape (placeholders):

   ```json
   {"hosts": {"example-host": {
     "principal": "example-principal-name",
     "files": [
       {"path": "/home/example/.config/example/settings", "owner": "example", "mode": "0600",
        "content": "key={{principal_access_key_id}}\nsecret={{principal_secret_access_key}}\n"},
       {"path": "/home/example/.example/notes", "owner": "example", "watch": false,
        "content": "{{random_hex:32}}\n"}
     ],
     "ignore_executables": [],
     "service_name": "example-name"
   }}}
   ```

   Templates: `{{principal_access_key_id}}`, `{{principal_secret_access_key}}`,
   `{{ssh_private_key}}` and `{{ssh_public_key}}` (one key pair generated per install), and
   `{{random_hex:N}}`. `service_name` (optional, default `hostmetricsd`) names the installed
   service and its paths; `launchd_label` (optional, `local.` prefix) overrides the macOS label.
   `watch: false` places a file without watching opens of it; use it for
   files that ordinary tools read on every run. `age_days` (default 120) back-dates each file.
   Choose paths no routine job reads: backup agents, indexers and file-sync clients would page
   you. The installer refuses to overwrite any file it did not place, and refuses a path whose
   directories include a symlink the file's owner could have made.
3. Set `enable_access_audit` to `true` in a commit, plan, read the plan, apply. Creating the
   principals and their keys trips the existing IAM-change security alerts; that is expected.
4. Install on each host. On the jumpbox, take that host's bundle from the generated secret and
   pipe it with the agent to the host (directly, or through the dev box that reaches it):

   ```bash
   aws secretsmanager get-secret-value --region us-west-1 --secret-id access-audit/bundles \
     --query SecretString --output text | jq -c '."example-host"' > bundle.json
   scp scripts/access-audit.py example-host:/tmp/access-audit.py
   ssh example-host 'sudo python3 -I /tmp/access-audit.py install && rm /tmp/access-audit.py' < bundle.json
   shred -u bundle.json
   ```

5. Prove the delivery path without touching a registered file:
   `sudo python3 -I /usr/local/libexec/hostmetricsd/hostmetricsd test-alert` sends one email and
   log line labelled TEST. With a `service_name`, the path is `/usr/local/libexec/<name>/<name>`.

Re-running the install replaces only files the agent placed and that are unchanged since.
`sudo python3 -I /usr/local/libexec/hostmetricsd/hostmetricsd uninstall` stops the service and
removes only those files, including any placed by an install that failed part-way. Removing a host from the registry and applying deletes its principal,
reporter, stream and heartbeat alarm.

## Tests

- `python3 -S -B scripts/test-access-audit.py`: offline (signing against the AWS-published
  SigV4 example, bundle validation, file placement and its refusal to overwrite, the eslogger
  stream and FIFO reader, reporting limits, integrity, and the Terraform boundaries).
- `sudo python3 -S -B scripts/test-access-audit-live.py`: the live Linux fanotify sensor.
