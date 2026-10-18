#!/usr/bin/env python3
"""Offline tests for the container layer of the containerized nextjs box (nextjs-ct-user-data.tf, scripts/nextjs-ct/).

Every host and container script runs here for real against fakes on PATH: the sshd relay, the credential
server, the router from `ndev` records to cloudflared ingress, the firewall and sshd rule generators."""

from pathlib import Path
import http.client
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "nextjs-ct-user-data.tf").read_text()
HOST = ROOT / "scripts" / "nextjs-ct" / "host"
CT = ROOT / "scripts" / "nextjs-ct" / "ct"
SCRIPT = TF.split("nextjs_ct_user_data = <<-SCRIPT\n", 1)[1].split("\nSCRIPT\n", 1)[0]


def fake(bin_dir, name, body):
    p = Path(bin_dir) / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)
    return p


CONFIG = {
    "role_arn": "arn:aws:iam::111111111111:role/nextjs-dev-role",
    "serving": False,
    "zones": {"cc-games.dev": "tunnel-a", "dolphin-labs.dev": "tunnel-b"},
    "ssh_hostnames": {"dolphin-labs.dev": "ssh.dolphin-labs.dev"},
    "pinned_routes": {},
    "session_policy": {"Statement": [{"Sid": "everyone"}]},
    "session_policy_arns": {"alice": ["arn:aws:iam::111111111111:policy/nextjs-ct-member-alice"],
                            "carol": ["arn:aws:iam::111111111111:policy/nextjs-ct-member-carol"]},
    "io_box_ip": "172.31.48.10",
    "member_box_ips": ["10.0.69.10", "10.12.2.10", "172.31.47.226", "10.0.69.11", "10.12.2.11", "172.31.47.227"],
    "users": {
        "alice": {"uid": 1001, "zone": "cc-games.dev", "owner": False, "box_ips": ["10.0.69.10", "10.12.2.10", "172.31.47.226"]},
        "carol": {"uid": 1002, "zone": "cc-games.dev", "owner": False, "box_ips": ["10.0.69.11", "10.12.2.11", "172.31.47.227"]},
        "boss": {"uid": 1003, "zone": "dolphin-labs.dev", "owner": True, "box_ips": []},
    },
}


class HostScriptTests(unittest.TestCase):
    def test_rendered_script_parses(self):
        # Placeholders for every interpolation and directive line: what is left must be valid bash.
        text = re.sub(r"^%\{[^}]*\}\n", "", SCRIPT.replace("$${", "${"), flags=re.M)
        text = re.sub(r"(?<!\$)\$\{(local|var)\.[^}]*\}|\$\{join\([^\n]*\)\}|\$\{[uz]\}", "PLACEHOLDER", text)
        subprocess.run(["bash", "-n"], input=text, text=True, check=True)

    def test_nobody_gets_a_host_shell_or_host_sudo(self):
        # The host is an admin and routing layer: no per-person sudo, no per-person agent units, no second
        # way in (Eternal Terminal), and the tunnels stay down until the cutover.
        for gone in ("NOPASSWD", "etserver", "claude-rc@", "codex-rc@", "ndev@", "agents-enable"):
            self.assertNotIn(gone, SCRIPT, gone)
        self.assertIn("nextjs_ct_serving = false", TF)
        self.assertIn("systemctl disable --now cloudflared@${z}.service", SCRIPT)

    def test_every_account_gets_a_container_including_the_owner(self):
        self.assertIn("%{for u in local.nextjs_users~}\n/usr/local/sbin/nextjs-ct-machine ${u}", SCRIPT)
        self.assertIn('nextjs_ct_owners = ["ejc3"]', TF)

    def test_session_policies_narrow_members_and_the_host_only_set(self):
        member = TF.split("nextjs_ct_deny_member = [", 1)[1].split("\n  ]\n", 1)[0]
        host = TF.split("nextjs_ct_deny_host_only = [", 1)[1].split("\n  ]\n", 1)[0]
        self.assertIn('Action   = "sts:AssumeRole"', member)
        self.assertIn("aws_secretsmanager_secret.dev_hop.arn", member)
        self.assertIn("data.aws_secretsmanager_secret.nextjs_gdocs", member)
        for action in ("ec2:GetPasswordData", "ssm:SendCommand", "ec2:CreateVolume", "ec2:DeleteVolume"):
            self.assertIn(f'"{action}"', member, action)
        self.assertIn("data.aws_secretsmanager_secret.nextjs_connector", host)
        self.assertIn("ssmmessages:*", host)
        self.assertIn("security_config.arn}/sessions/*", host)
        self.assertIn("aws_secretsmanager_secret.wbox_admin.arn", member)  # the owner's own wbox password
        self.assertIn(":secret:macbook-ssh-access-nextjs-ejc3-*", member)  # the owner's Access token for the Mac

    def test_each_member_has_a_managed_session_policy_naming_their_own_boxes(self):
        # An inline session policy is capped at 2,048 characters; a member's denials name that member's own three
        # boxes, so each member gets a managed policy, attached to nothing and passed with the member's session only.
        res = TF.split('resource "aws_iam_policy" "nextjs_ct_member" {', 1)[1].split("\n}\n", 1)[0]
        self.assertIn("for_each    = toset(local.nextjs_ct_member_users)", res)
        self.assertIn("concat(local.nextjs_ct_allow_all, local.nextjs_ct_deny_member, try(local.nextjs_ct_own_boxes[each.key], local.nextjs_ct_no_boxes))", res)
        self.assertIn("nextjs_ct_member_users = [for u in local.nextjs_users : u if !contains(local.nextjs_ct_owners, u)]", TF)
        self.assertIn("session_policy_arns = { for u, pol in aws_iam_policy.nextjs_ct_member : u => [pol.arn] }", TF)
        self.assertNotIn("aws_iam_role_policy_attachment\" \"nextjs_ct_member", TF)
        own = TF.split("nextjs_ct_own_boxes = {", 1)[1].split("\n  ] }\n", 1)[0]
        box = TF.split("nextjs_ct_member_box_all = {", 1)[1].split("\n  } }\n", 1)[0]
        # Their own: names, launch templates, volumes by explicit ARN, the wbox role, the two secrets.
        for ref in ("local.member_wbox_slots[p]", "local.member_pbox_slots[p]", "local.member_gbox_slots[p]",
                    "aws_launch_template.member_wbox[p].arn", "aws_launch_template.member_pbox[p].arn",
                    "aws_launch_template.member_gbox[p].arn", "aws_ebs_volume.member_wbox_data[p].arn",
                    "aws_ebs_volume.member_pbox_work[p].arn", "aws_iam_role.member_wbox[p].arn",
                    "aws_secretsmanager_secret.member_box_ssh[p].arn", "aws_secretsmanager_secret.member_wbox_admin[p].arn"):
            self.assertIn(ref, box, ref)
        def stmt(sid):
            return re.split(r'Sid +="' + sid + '"', own.replace('= "', '="'), maxsplit=1)[1].split("},\n    {", 1)[0].replace('="', '= "')
        # By the secret's own ARN, never a condition on secretsmanager:SecretId (the caller's text: a name or an ARN).
        self.assertIn("Resource = flatten([for o, ob in local.nextjs_ct_member_box_all : ob.secrets if o != p])", stmt("OtherMembersBoxSecrets"))
        self.assertFalse('"secretsmanager:SecretId" =' in TF)
        self.assertIn('StringNotEquals = { "ec2:CreateAction" = "RunInstances" }', stmt("TagOnlyAtLaunch"))
        self.assertIn('StringNotEquals = { "aws:RequestTag/Name" = b.names }', stmt("OwnBoxNamesOnly"))
        self.assertIn('"ec2:RunInstances", "ec2:CreateTags"', stmt("OwnBoxNamesOnly"))
        self.assertIn('ArnNotEquals = { "ec2:LaunchTemplate" = b.templates }', stmt("OwnLaunchTemplatesOnly"))
        for action in ("TerminateInstances", "StartInstances", "StopInstances", "RebootInstances", "AttachVolume", "DetachVolume"):
            self.assertIn(f'"ec2:{action}"', stmt("OwnBoxesOnly"), action)
        self.assertIn('StringNotEquals = { "ec2:ResourceTag/Name" = b.names }', stmt("OwnBoxesOnly"))
        self.assertIn('NotResource = concat(b.volumes, ["arn:aws:ec2:*:*:instance/*"])', stmt("OwnVolumesOnly"))
        self.assertIn('NotResource = [b.role]', stmt("OwnBoxRoleOnly"))
        self.assertIn('Action      = "iam:PassRole"', stmt("OwnBoxRoleOnly"))

    def test_imds_passes_the_members_managed_policy(self):
        imds = (HOST / "nextjs-ct-imds").read_text()
        self.assertIn('json.dumps(self.config["session_policy"]', imds)
        self.assertIn('cmd += ["--policy-arns"] + [f"arn={a}" for a in arns]', imds)
        self.assertNotIn("session_policies", imds)

    def test_the_owner_can_attach_the_volumes_pbox_launches_with(self):
        # #490 put the EBS actions in the host-only set, so `pbox up` from ejc3's container launched a box and then
        # failed to attach its work volume. A member may attach only their own volumes; the owner is not narrowed.
        host = TF.split("nextjs_ct_deny_host_only = [", 1)[1].split("\n  ]\n", 1)[0]
        for action in ("ec2:AttachVolume", "ec2:CreateVolume", "ec2:DetachVolume", "ec2:DeleteVolume", "ec2:RunInstances"):
            self.assertNotIn(f'"{action}"', host, action)
        self.assertIn("nextjs_ct_session_policy = { Version = \"2012-10-17\", Statement = concat(local.nextjs_ct_allow_all, local.nextjs_ct_deny_host_only) }", TF)

    def test_no_container_can_assume_the_box_role_without_its_session_policy(self):
        # Review of #490 (Codex P1): the owner keeps sts:AssumeRole for the Dolphin role, and the role trusts
        # itself, so without this denial the owner's container could mint an unnarrowed session.
        host = TF.split("nextjs_ct_deny_host_only = [", 1)[1].split("\n  ]\n", 1)[0]
        stmt = host.split('Sid      = "NoSessionWithoutThisPolicy"', 1)[1].split("}", 1)[0]
        self.assertIn('Effect   = "Deny"', stmt)
        self.assertIn('Action   = "sts:AssumeRole"', stmt)
        self.assertIn("Resource = local.nextjs_dev_role_arn", stmt)

    def test_session_policies_fit_the_limits(self):
        # AssumeRole refuses an inline session policy over 2,048 characters; IAM refuses a managed policy over 6,144
        # (whitespace not counted). Render both with realistic ARNs.
        arn = "arn:aws:secretsmanager:us-west-1:111111111111:secret:{}-AbCdEf"
        acts = lambda sid: re.findall(r'"([a-z0-9]+:[A-Za-z*]+)"', TF.split(sid, 1)[1].split("Resource", 1)[0])
        allow = [{"Sid": "UpToTheRole", "Effect": "Allow", "Action": "*", "Resource": "*"}]
        inline = allow + [
            {"Sid": "HostOnlyAgents", "Effect": "Deny", "Action": acts("HostOnlyAgents"), "Resource": "*"},
            {"Sid": "NoSessionWithoutThisPolicy", "Effect": "Deny", "Action": "sts:AssumeRole",
             "Resource": "arn:aws:iam::111111111111:role/nextjs-dev-role"},
            {"Sid": "HostOnlyData", "Effect": "Deny", "Action": ["secretsmanager:GetSecretValue", "s3:GetObject", "s3:PutObject"],
             "Resource": [arn.format("cloudflare-tunnel-credentials"), arn.format("cloudflare-dolphin-tunnel-credentials"),
                          "arn:aws:s3:::ejc3-dev-scripts/*", "arn:aws:s3:::security-config-111111111111-us-west-1/sessions/*"]},
        ]
        size = len(json.dumps({"Version": "2012-10-17", "Statement": inline}, separators=(",", ":")))
        self.assertLess(size, 1900, size)
        sp = "arn:aws:secretsmanager:us-west-1:111111111111:secret:"
        names = ["wbox-colton", "pbox-colton", "gbox-colton"]
        lt = "arn:aws:ec2:{}:111111111111:launch-template/lt-0123456789abcdef0"
        launched = ["arn:aws:ec2:*:*:instance/*", "arn:aws:ec2:*:*:volume/*", "arn:aws:ec2:*:*:network-interface/*"]
        managed = allow + [
            {"Sid": "OwnerOnlyRoles", "Effect": "Deny", "Action": "sts:AssumeRole", "Resource": "*"},
            {"Sid": "OwnerOnlySecrets", "Effect": "Deny", "Action": "secretsmanager:GetSecretValue",
             "Resource": [arn.format("dev-hop-ssh-key"), arn.format("wbox/administrator-password"),
                          arn.format("google-docs-oauth-desktop"), arn.format("google-docs-oauth-token"),
                          sp + "macbook-ssh-access-nextjs-ejc3-*"]},
            {"Sid": "NoMemberNeedsThese", "Effect": "Deny", "Action": acts("NoMemberNeedsThese"), "Resource": "*"},
            {"Sid": "OtherMembersBoxSecrets", "Effect": "Deny", "Action": "secretsmanager:GetSecretValue",
             "Resource": [sp + f"{k}-AbCdEf" for o in ("connor", "colin", "skevh") for k in (f"member-box/{o}/ssh-key", f"wbox-{o}/administrator-password")]},
            {"Sid": "TagOnlyAtLaunch", "Effect": "Deny", "Action": "ec2:CreateTags", "Resource": launched,
             "Condition": {"StringNotEquals": {"ec2:CreateAction": "RunInstances"}}},
            {"Sid": "OwnBoxNamesOnly", "Effect": "Deny", "Action": ["ec2:RunInstances", "ec2:CreateTags"], "Resource": launched,
             "Condition": {"StringNotEquals": {"aws:RequestTag/Name": names}}},
            {"Sid": "OwnLaunchTemplatesOnly", "Effect": "Deny", "Action": "ec2:RunInstances", "Resource": launched[0],
             "Condition": {"ArnNotEquals": {"ec2:LaunchTemplate": [lt.format("us-west-1"), lt.format("us-east-2"), lt.format("us-west-1")]}}},
            {"Sid": "OwnBoxesOnly", "Effect": "Deny", "Action": acts("Sid       = \"OwnBoxesOnly\""), "Resource": launched[0],
             "Condition": {"StringNotEquals": {"ec2:ResourceTag/Name": names}}},
            {"Sid": "OwnVolumesOnly", "Effect": "Deny", "Action": ["ec2:AttachVolume", "ec2:DetachVolume"],
             "NotResource": ["arn:aws:ec2:us-west-1:111111111111:volume/vol-0123456789abcdef0",
                             "arn:aws:ec2:us-east-2:111111111111:volume/vol-0123456789abcdef0", launched[0]]},
            {"Sid": "OwnBoxRoleOnly", "Effect": "Deny", "Action": "iam:PassRole",
             "NotResource": ["arn:aws:iam::111111111111:role/wbox-colton-role"]},
        ]
        self.assertEqual(len(acts("Sid       = \"OwnBoxesOnly\"")), 6)
        size = len(json.dumps({"Version": "2012-10-17", "Statement": managed}, separators=(",", ":")))
        self.assertLess(size, 5500, size)


class MigrateTests(unittest.TestCase):
    def test_only_the_final_pass_deletes(self):
        # Review of #490 (Codex P2): the seed must never delete what exists in a home on the new box.
        m = (HOST / "nextjs-ct-migrate").read_text()
        self.assertIn('BW=(--bwlimit=25000); [ -z "$FINAL" ] || BW=(--delete)', m)
        rsync_home = next(l for l in m.splitlines() if "rsync -aHAX" in l)
        self.assertNotIn("--delete", rsync_home)

    def test_seed_is_one_stream_final_is_parallel_and_any_failure_fails(self):
        m = (HOST / "nextjs-ct-migrate").read_text()
        final = m.split('if [ -n "$FINAL" ]; then\n    rsync', 1)[1].split("else", 1)[0]
        self.assertIn('"/var/log/nextjs-ct-migrate-$U.log" 2>&1 &', final)
        seed = m.split('if [ -n "$FINAL" ]; then\n    rsync', 1)[1].split("else", 1)[1].split("fi", 1)[0]
        self.assertNotIn("&\n", seed)
        self.assertIn('wait "${p%%:*}" || FAILED="$FAILED ${p#*:}"', m)
        self.assertIn('[ -z "$FAILED" ] || {', m)

    def test_an_interrupted_final_pass_stops_its_copies(self):
        # Review of #491 (CodeRabbit): SIGTERM while the parallel copies run must kill and reap every one of
        # them and fail, never leave rsync --delete mirroring homes behind the operator's back.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps({"users": {"alice": {}, "boss": {}}}))
            fake(d, "rsync", 'echo $$ >> "$PIDFILE"; exec sleep 60\n')
            fake(d, "ssh", "exit 0\n")
            fake(d, "systemctl", "exit 0\n")
            fake(d, "id", "echo 1001\n")
            text = (HOST / "nextjs-ct-migrate").read_text().replace("/etc/nextjs-ct/config.json", str(d / "config.json")) \
                .replace("/var/log/nextjs-ct-migrate-", str(d / "log-"))
            (d / "m.sh").write_text(text)
            env = {"PATH": f"{d}:/usr/bin:/bin", "PIDFILE": str(d / "pids")}
            proc = subprocess.Popen(["bash", str(d / "m.sh"), "10.0.0.1", "--final"], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(100):
                if (d / "pids").exists() and len((d / "pids").read_text().split()) == 2:
                    break
                time.sleep(0.05)
            pids = [int(p) for p in (d / "pids").read_text().split()]
            self.assertEqual(len(pids), 2)
            proc.terminate()
            out, err = proc.communicate(timeout=20)
            time.sleep(0.2)
            alive = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                    os.kill(pid, 9)  # do not leave the fake behind if the script did
                except ProcessLookupError:
                    pass
            self.assertEqual(alive, [], "copies left running after the interrupt")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("interrupted; copies stopped", err)

    def test_final_pass_fails_when_published_projects_cannot_be_copied(self):
        # Review of #490 (Codex P2).
        m = (HOST / "nextjs-ct-migrate").read_text()
        self.assertNotIn('"$T/" || true', m)
        self.assertIn('could not copy the published projects" >&2; [ -z "$FINAL" ] || exit 1; }', m)
        self.assertIn('[ "$HAVE_NDEV" = 255 ]', m)

    def test_final_pass_carries_the_old_host_keys_and_stops_containers_first(self):
        m = (HOST / "nextjs-ct-migrate").read_text()
        self.assertIn("ubuntu@$OLD:/etc/ssh/ssh_host_*", m)
        self.assertLess(m.index('systemctl stop "systemd-nspawn@$U.service"'), m.index("rsync -aHAX"))


class EnterTests(unittest.TestCase):
    """sshd's ForceCommand: every kind of session is relayed to the person's own container."""

    def run_enter(self, env_extra):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            enter_dir = d / "enter" / os.environ.get("USER", "me")
            user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
            enter_dir = d / "enter" / user
            enter_dir.mkdir(parents=True)
            (enter_dir / "ip").write_text("10.88.1.2\n")
            fake(d, "ssh", 'printf "%s\\n" "$@" > "$OUT"\n')
            text = (HOST / "nextjs-ct-enter").read_text().replace("/var/lib/nextjs-ct/enter", str(d / "enter"))
            script = d / "enter.sh"
            script.write_text(text)
            env = {"PATH": f"{d}:/usr/bin:/bin", "OUT": str(d / "args")}
            env.update(env_extra)
            r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return (d / "args").read_text().splitlines(), user

    def test_interactive_login_gets_a_pty(self):
        args, user = self.run_enter({"SSH_TTY": "/dev/pts/3"})
        self.assertEqual(args[0], "-t")
        self.assertEqual(args[-1], f"{user}@10.88.1.2")
        self.assertIn("StrictHostKeyChecking=yes", args)

    def test_a_command_is_passed_whole(self):
        args, user = self.run_enter({"SSH_ORIGINAL_COMMAND": "git-upload-pack 'repo.git'"})
        self.assertEqual(args[0], "-T")
        self.assertEqual(args[-2:], ["--", "git-upload-pack 'repo.git'"])

    def test_sftp_subsystem_becomes_the_container_sftp(self):
        args, user = self.run_enter({"SSH_ORIGINAL_COMMAND": "/usr/lib/openssh/sftp-server"})
        self.assertEqual(args[-3:], ["-s", f"{user}@10.88.1.2", "sftp"])

    def test_agent_is_forwarded_only_when_the_client_forwarded_it(self):
        args, _ = self.run_enter({"SSH_AUTH_SOCK": "/tmp/agent"})
        self.assertIn("-A", args)
        args, _ = self.run_enter({})
        self.assertNotIn("-A", args)


class ImdsTests(unittest.TestCase):
    """The credential server: IMDSv2 only, one container per local address, its own session only."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        (d / "config.json").write_text(json.dumps(CONFIG))
        fake(d, "aws", """{ printf '%s\\n' "$@"; echo ---; } >> "$CALLS"
cat <<JSON
{"Credentials": {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "secret", "SessionToken": "tok",
 "Expiration": "2099-01-01T00:00:00Z"}}
JSON
""")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.calls = d / "calls"
        env = dict(os.environ, NEXTJS_CT_CONFIG=str(d / "config.json"), NEXTJS_CT_IMDS_PORT=str(cls.port),
                   NEXTJS_CT_AWS=str(d / "aws"), NEXTJS_CT_REAL_IMDS="http://127.0.0.1:9",
                   NEXTJS_CT_IMDS_USERS=json.dumps({"127.0.0.1": "alice", "127.0.0.2": "boss"}), CALLS=str(cls.calls))
        cls.proc = subprocess.Popen(["python3", "-I", str(HOST / "nextjs-ct-imds")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()
        cls.tmp.cleanup()

    def req(self, method, path, headers=None, host="127.0.0.1"):
        c = http.client.HTTPConnection(host, self.port, timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read().decode()

    def token(self, host="127.0.0.1"):
        status, tok = self.req("PUT", "/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "60"}, host)
        self.assertEqual(status, 200)
        return tok

    def test_imdsv2_is_required(self):
        self.assertEqual(self.req("GET", "/latest/meta-data/iam/security-credentials/")[0], 401)
        self.assertEqual(self.req("PUT", "/latest/api/token")[0], 400)

    def test_credentials_are_this_containers_session(self):
        tok = self.token()
        status, role = self.req("GET", "/latest/meta-data/iam/security-credentials/", {"X-aws-ec2-metadata-token": tok})
        self.assertEqual((status, role), (200, "nextjs-dev-role"))
        status, body = self.req("GET", "/latest/meta-data/iam/security-credentials/nextjs-dev-role", {"X-aws-ec2-metadata-token": tok})
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual((doc["Code"], doc["AccessKeyId"], doc["Token"]), ("Success", "AKIAFAKE", "tok"))
        call = next(c for c in self.calls.read_text().split("---\n") if "ct-alice" in c)
        self.assertIn('"Sid":"everyone"', call)
        self.assertIn("--policy-arns\narn=arn:aws:iam::111111111111:policy/nextjs-ct-member-alice\n", call)

    def test_owner_gets_no_member_policy(self):
        tok = self.token("127.0.0.2")
        status, _ = self.req("GET", "/latest/meta-data/iam/security-credentials/nextjs-dev-role", {"X-aws-ec2-metadata-token": tok}, "127.0.0.2")
        self.assertEqual(status, 200)
        call = next(c for c in self.calls.read_text().split("---\n") if "ct-boss" in c)
        self.assertIn('"Sid":"everyone"', call)
        self.assertNotIn("--policy-arns", call)  # the owner is not narrowed to any member's boxes

    def test_a_token_is_good_only_where_it_was_issued(self):
        tok = self.token("127.0.0.2")
        self.assertEqual(self.req("GET", "/latest/meta-data/iam/security-credentials/", {"X-aws-ec2-metadata-token": tok})[0], 401)

    def test_unknown_address_and_proxied_requests_are_refused(self):
        self.assertEqual(self.req("PUT", "/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "60"}, "127.0.0.3")[0], 403)
        self.assertEqual(self.req("PUT", "/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "60", "X-Forwarded-For": "1.2.3.4"})[0], 403)

    def test_one_container_cannot_tie_up_the_server(self):
        # Review of #490 (Codex P1): stalled connections are capped per container and time out, and other
        # containers are still served meanwhile.
        stalled = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(8)]
        try:
            time.sleep(0.3)
            extra = socket.create_connection(("127.0.0.1", self.port))
            extra.settimeout(3)
            self.assertEqual(extra.recv(1), b"")  # closed at once: over the per-container cap
            extra.close()
            self.assertEqual(self.req("PUT", "/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "60"}, "127.0.0.2")[0], 200)
        finally:
            for s in stalled:
                s.close()
        time.sleep(0.5)
        self.assertEqual(self.req("PUT", "/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "60"})[0], 200)

    def test_a_stalled_client_is_dropped(self):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.settimeout(10)
        began = time.monotonic()
        self.assertEqual(s.recv(1), b"")
        self.assertLess(time.monotonic() - began, 8)
        s.close()

    def test_nothing_else_is_served(self):
        tok = self.token()
        for path in ("/latest/user-data", "/latest/meta-data/identity-credentials/ec2/security-credentials/ec2-instance",
                     "/latest/meta-data/tags/instance/Name", "/latest/meta-data/iam/security-credentials/other-role"):
            self.assertEqual(self.req("GET", path, {"X-aws-ec2-metadata-token": tok})[0], 404, path)


class NdevSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.d = Path(self.tmp.name)
        self.config = json.loads(json.dumps(CONFIG))
        for sub in ("ndev/alice/instances", "ndev/boss/instances", "cf", "home/alice/app", "home/boss/site", "bin"):
            (d / sub).mkdir(parents=True)
        fake(d / "bin", "systemctl", 'printf "%s\\n" "$*" >> "$CALLS"\n')

    def env_file(self, user, label, host, port, d):
        (self.d / "ndev" / user / "instances" / f"{label}.env").write_text(f"HOST={host}\nPORT={port}\nDIR={d}\nWHO={user}\n")

    def sync(self):
        (self.d / "config.json").write_text(json.dumps(self.config))
        env = dict(os.environ, NEXTJS_CT_CONFIG=str(self.d / "config.json"), NEXTJS_CT_NDEV_ROOT=str(self.d / "ndev"),
                   NEXTJS_CT_CLOUDFLARED_DIR=str(self.d / "cf"), NEXTJS_CT_HOME_ROOT=str(self.d / "home"),
                   NEXTJS_CT_SYSTEMCTL=str(self.d / "bin" / "systemctl"), CALLS=str(self.d / "calls"),
                   NEXTJS_CT_UIDS=json.dumps({"alice": 1001, "boss": 1003}))
        r = subprocess.run(["python3", str(HOST / "nextjs-ct-ndev-sync")], env=env, capture_output=True, text=True)
        return r, (self.d / "cf" / "config-cc-games.dev.yml").read_text(), (self.d / "cf" / "config-dolphin-labs.dev.yml").read_text()

    def test_routes_to_the_container_address(self):
        self.env_file("alice", "alice", "alice.cc-games.dev", 3729, self.d / "home/alice/app")
        self.env_file("boss", "boss-site", "boss-site.dolphin-labs.dev", 3203, self.d / "home/boss/site")
        r, games, dolphin = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("  - hostname: alice.cc-games.dev\n    service: http://10.88.1.2:3729", games)
        self.assertIn("  - hostname: boss-site.dolphin-labs.dev\n    service: http://10.88.3.2:3203", dolphin)
        self.assertTrue(dolphin.index("ssh.dolphin-labs.dev") < dolphin.index("boss-site"))
        self.assertTrue(games.endswith("  - service: http_status:404\n"))

    def test_untrusted_records_are_not_routed(self):
        home = self.d / "home/alice/app"
        self.env_file("alice", "boss", "boss.cc-games.dev", 3000, home)              # another account's label
        self.env_file("alice", "alice-x", "alice-x.dolphin-labs.dev", 3000, home)     # another zone
        self.env_file("alice", "alice-p", "alice-p.cc-games.dev", 80, home)           # privileged port
        self.env_file("alice", "alice-o", "alice-o.cc-games.dev", 3000, self.d / "home/boss/site")  # outside home
        (self.d / "home/alice/escape").symlink_to(self.d / "home/boss/site")
        self.env_file("alice", "alice-l", "alice-l.cc-games.dev", 3000, self.d / "home/alice/escape")  # symlinked out
        self.env_file("alice", "alice-gone", "alice-gone.cc-games.dev", 3000, self.d / "home/alice/deleted")
        (self.d / "ndev/alice/instances/alice-s.env").symlink_to(self.d / "config.json")  # a symlinked record
        r, games, dolphin = self.sync()
        self.assertNotIn("hostname: boss", games)
        self.assertNotIn("alice-", games + dolphin)

    def test_serving_restarts_only_a_changed_zone(self):
        self.config["serving"] = True
        self.env_file("alice", "alice", "alice.cc-games.dev", 3729, self.d / "home/alice/app")
        self.sync()
        (self.d / "calls").unlink()
        self.env_file("alice", "alice-two", "alice-two.cc-games.dev", 3730, self.d / "home/alice/app")
        self.sync()
        calls = (self.d / "calls").read_text().splitlines()
        self.assertIn("restart cloudflared@cc-games.dev.service", calls)
        self.assertNotIn("restart cloudflared@dolphin-labs.dev.service", calls)

    def test_not_serving_starts_nothing(self):
        self.env_file("alice", "alice", "alice.cc-games.dev", 3729, self.d / "home/alice/app")
        self.sync()
        self.assertFalse((self.d / "calls").exists())


class GeneratorTests(unittest.TestCase):
    """The firewall and sshd rules, generated from config.json with fakes for id, nft and sshd."""

    def generate(self, script, out_name):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps(CONFIG))
            fake(d, "id", 'case "$2" in alice) echo 1001 ;; carol) echo 1002 ;; boss) echo 1003 ;; *) exit 1 ;; esac\n')
            for tool in ("nft", "sysctl", "sshd", "systemctl"):
                fake(d, tool, "exit 0\n")
            text = (HOST / script).read_text().replace("/etc/nextjs-ct/config.json", str(d / "config.json")) \
                .replace("/etc/nextjs-ct/firewall.nft", str(d / "out")).replace("/etc/ssh/sshd_config.d/90-nextjs-ct.conf", str(d / "out"))
            (d / "s.sh").write_text(text)
            r = subprocess.run(["bash", str(d / "s.sh")], env={"PATH": f"{d}:/usr/bin:/bin"}, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return (d / "out").read_text()

    def test_firewall(self):
        nft = self.generate("nextjs-ct-firewall", "out")
        self.assertIn('iifname "ve-alice" ip saddr 10.88.1.2 ip daddr 169.254.169.254 tcp dport 80 dnat to 10.88.1.1:8169', nft)
        self.assertIn('iifname "ve-alice" ip saddr != 10.88.1.2 drop', nft)
        self.assertLess(nft.index('iifname "ve-alice" tcp dport 8169 ct count over 16 drop'),
                        nft.index('iifname "ve-alice" ip saddr 10.88.1.2 ip daddr 10.88.1.1 tcp dport 8169 ct status dnat accept'))
        self.assertIn('iifname "ve-*" oifname "ve-*" drop', nft)
        self.assertIn('iifname "ve-*" meta nfproto ipv6 drop', nft)
        self.assertNotIn("2222", nft)  # no Mac relay: the Mac is reached through Cloudflare only
        self.assertIn("type filter hook forward priority filter; policy drop;", nft)
        # The io-box scratch: refused to a member, not to the owner.
        self.assertIn('iifname "ve-alice" ip daddr 172.31.48.10 drop', nft)
        self.assertNotIn('iifname "ve-boss" ip daddr 172.31.48.10 drop', nft)
        # Before the established/related accept, so a connection open at reload is cut too (review of #499).
        forward = nft.split("chain forward {", 1)[1]
        self.assertLess(forward.index('iifname "ve-alice" ip daddr 172.31.48.10 drop'), forward.index("ct state established,related accept"))
        self.assertLess(forward.index('iifname "ve-alice" ip saddr != 10.88.1.2 drop'), forward.index("ct state established,related accept"))
        # Another member's boxes: refused to each member, their own boxes and the owner untouched.
        self.assertIn('iifname "ve-alice" ip daddr { 10.0.69.11, 10.12.2.11, 172.31.47.227 } drop', nft)
        self.assertIn('iifname "ve-carol" ip daddr { 10.0.69.10, 10.12.2.10, 172.31.47.226 } drop', nft)
        self.assertNotIn('iifname "ve-boss" ip daddr {', nft)
        self.assertLess(forward.index('iifname "ve-alice" ip daddr { 10.0.69.11'), forward.index("ct state established,related accept"))

    def test_sshd(self):
        conf = self.generate("nextjs-ct-sshd", "out")
        alice = conf.split("Match User alice\n", 1)[1].split("\nMatch", 1)[0]
        boss = conf.split("Match User boss\n", 1)[1].split("\nMatch", 1)[0]
        self.assertIn("ForceCommand /usr/local/sbin/nextjs-ct-enter", alice)
        carol = conf.split("Match User carol\n", 1)[1].split("\nMatch", 1)[0]
        # A member may forward to their own container and their own three boxes, nothing else.
        self.assertIn("PermitOpen 10.88.1.2:* 10.0.69.10:* 10.12.2.10:* 172.31.47.226:*\n", alice + "\n")
        self.assertIn("PermitOpen 10.88.2.2:* 10.0.69.11:* 10.12.2.11:* 172.31.47.227:*\n", carol + "\n")
        self.assertIn("PermitOpen 10.88.3.2:*\n", boss + "\n")
        self.assertIn("AllowTcpForwarding local", alice)
        self.assertIn("PermitListen none", alice)
        self.assertIn("PermitListen none", boss)  # the owner too: no reverse tunnel lands on this box
        self.assertIn("AllowTcpForwarding local", boss)
        self.assertTrue(conf.rstrip().endswith("Match all"))


class HostAccountTests(unittest.TestCase):
    def test_host_accounts_get_a_shell_that_reads_nothing_from_the_home(self):
        # sshd runs the ForceCommand through the login shell; bash would source ~/.bashrc (written from inside
        # the container) on the host first. Found on the box: a planted ~/.bashrc ran in the host namespace.
        machine = (HOST / "nextjs-ct-machine").read_text()
        self.assertIn('usermod -s /bin/sh "$U"', machine)
        self.assertEqual(len(re.findall(r"useradd [^\n]*-s /bin/sh", machine)), 2)
        self.assertNotRegex(machine, r"(useradd|usermod)[^\n]*-s /bin/(ba|z)sh")

    def test_sshd_never_runs_the_persons_rc(self):
        self.assertIn('echo "    PermitUserRC no"', (HOST / "nextjs-ct-sshd").read_text())

    def test_home_is_nosuid_on_the_host_and_idmapped_in_the_container(self):
        machine = (HOST / "nextjs-ct-machine").read_text()
        self.assertIn("Options=bind,nosuid,nodev", machine)
        self.assertIn("Bind=/home/$U:/home/$U:idmap", machine)
        self.assertIn("PrivateUsers=$SHIFT:65536", machine)
        files = machine.split("[Files]", 1)[1].split("[Network]", 1)[0]
        self.assertIn("PrivateUsersOwnership=chown", files)  # a [Files] key: in [Exec] systemd ignores it


class MacTests(unittest.TestCase):
    """ejc3's container reaches the MacBook the way every box does (owner, 2026-10-18: "cloudflare only")."""

    def test_the_owner_gets_the_shared_cloudflare_client_with_their_own_token(self):
        body = TF.split("nextjs_ct_macbook_ssh = <<-MACBOOK\n", 1)[1].split("\nMACBOOK\n", 1)[0]
        self.assertIn('MAC_USER="$1"', body)
        self.assertIn("MAC_SECRET=macbook-ssh-access-nextjs-ejc3", body)
        self.assertLess(body.index("${local.mac_cloudflared_install}"), body.index("${local.mac_ssh_client_setup}"))
        self.assertIn('bash "$SHARED/snippets/macbook-ssh.sh" "$U"', (CT / "provision").read_text().split('if [ "$OWNER" = true ]; then', 1)[1])

    def test_no_reverse_tunnel_relay_is_left(self):
        for f in ("nextjs-ct-machine", "nextjs-ct-sshd", "nextjs-ct-firewall"):
            text = (HOST / f).read_text()
            self.assertNotIn("2222", text, f)
            self.assertNotIn("mac-tunnel", text, f)
            self.assertNotIn(".mac //", text, f)
        self.assertNotRegex(TF, r"\bmac\s+=\s+contains")


class ContainerScriptTests(unittest.TestCase):
    def test_every_container_script_parses(self):
        for f in CT.iterdir():
            if f.is_file() and f.read_text().startswith("#!/bin/bash"):
                subprocess.run(["bash", "-n", str(f)], check=True)

    def test_dev_servers_listen_on_the_container_not_loopback(self):
        run = (CT / "ndev-run").read_text()
        self.assertEqual(run.count("--hostname 0.0.0.0"), 2)
        self.assertNotIn("127.0.0.1", run)

    def test_agents_wait_for_the_cutover(self):
        enable = (CT / "agents-enable").read_text()
        self.assertIn("[ \"$(jq -r '.serving' /opt/nextjs-ct/config.json 2>/dev/null)\" = true ] || exit 0", enable)
        self.assertLess(enable.index("|| exit 0"), enable.index("systemctl enable"))

    def test_no_terraform_left_in_plain_files(self):
        for f in list(CT.rglob("*")) + list(HOST.iterdir()):
            if f.is_file():
                self.assertNotRegex(f.read_text(), r"\$\{(local|var)\.|\$\$\{", str(f))

    def test_container_ndev_register_refuses_another_account(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            fake(d, "cat", 'echo alice\n')
            fake(d, "jq", 'echo cc-games.dev\n')
            text = (CT / "ndev-register").read_text().replace("/usr/local/bin/ndev-zone", "jq")
            (d / "r.sh").write_text(text)
            r = subprocess.run(["bash", str(d / "r.sh"), "boss.cc-games.dev", "3000", "boss", "/home/boss/x"],
                               env={"PATH": f"{d}:/usr/bin:/bin"}, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("belongs to alice", r.stderr)


if __name__ == "__main__":
    unittest.main()
