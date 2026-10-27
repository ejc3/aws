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
import types
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
    "member_box_ips": ["10.0.69.10", "172.31.47.226", "10.0.69.11", "172.31.47.227"],
    "users": {
        "alice": {"uid": 1001, "zone": "cc-games.dev", "owner": False, "box_ips": ["10.0.69.10", "172.31.47.226"]},
        "carol": {"uid": 1002, "zone": "cc-games.dev", "owner": False, "box_ips": ["10.0.69.11", "172.31.47.227"]},
        # A member whose managed policy is missing from session_policy_arns (a config written mid-change, say).
        "dave": {"uid": 1004, "zone": "cc-games.dev", "owner": False, "box_ips": []},
        "boss": {"uid": 1003, "zone": "dolphin-labs.dev", "owner": True, "box_ips": []},
    },
}


class HostScriptTests(unittest.TestCase):
    def test_rendered_script_parses(self):
        # Placeholders for every interpolation and directive line: what is left must be valid bash.
        text = re.sub(r"^%\{[^}]*\}\n", "", SCRIPT.replace("$${", "${"), flags=re.M)
        text = re.sub(r"(?<!\$)\$\{(local|var)\.[^}]*\}|\$\{join\([^\n]*\)\}|\$\{[uz]\}", "PLACEHOLDER", text)
        subprocess.run(["bash", "-n"], input=text, text=True, check=True)

    def test_the_host_allows_enough_inotify_instances_for_every_container(self):
        # Every container's user namespace is root's, so all their inotify instances count against the host's one
        # limit; 128 ran out at five containers and OpenCode 2's server hung on EMFILE (2026-10-22).
        conf = SCRIPT.split("cat > /etc/sysctl.d/60-nextjs-ct.conf <<'SYSCTL'\n", 1)[1].split("\nSYSCTL\n", 1)[0]
        value = int(re.search(r"^fs\.inotify\.max_user_instances = (\d+)$", conf, re.M).group(1))
        self.assertGreaterEqual(value, 1024)
        self.assertIn("sysctl -q --system", SCRIPT.split("60-nextjs-ct.conf", 1)[1].split("\n# ----", 1)[0])

    def test_nobody_gets_a_host_shell_or_host_sudo(self):
        # The host is an admin and routing layer: no per-person sudo, no per-person agent units, no second
        # way in (Eternal Terminal), and the tunnels stay down until the cutover.
        for gone in ("NOPASSWD", "etserver", "claude-rc@", "codex-rc@", "ndev@", "agents-enable"):
            self.assertNotIn(gone, SCRIPT, gone)
        self.assertIn("nextjs_ct_serving = true", TF)
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
        for action in ("ec2:GetPasswordData", "ssm:SendCommand", "ec2:CreateVolume", "ec2:DeleteVolume", "ec2:CancelSpotInstanceRequests"):
            self.assertIn(f'"{action}"', member, action)
        self.assertIn("data.aws_secretsmanager_secret.nextjs_connector", host)
        self.assertIn("ssmmessages:*", host)
        self.assertIn("security_config.arn}/sessions/*", host)
        self.assertIn("aws_secretsmanager_secret.wbox_admin.arn", member)  # the owner's own wbox password
        self.assertIn(":secret:macbook-ssh-access-nextjs-ejc3-*", member)  # the owner's Access token for the Mac
        self.assertIn(":secret:colton-mac-ssh-access-nextjs-ejc3-*", member)  # the owner's Access token for Colton's Mac
        # The owner's `wbox run` output (security review of #502): the bucket (ListBucket) and its objects.
        out = member.split('Sid      = "OwnerOnlyWboxOutput"', 1)[1].split("},", 1)[0]
        self.assertIn('Action   = ["s3:GetObject", "s3:ListBucket"]', out)
        self.assertIn('Resource = [aws_s3_bucket.wbox_output.arn, "${aws_s3_bucket.wbox_output.arn}/*"]', out)

    def test_each_member_has_a_managed_session_policy_naming_their_own_boxes(self):
        # An inline session policy is capped at 2,048 characters; a member's denials name that member's own three
        # boxes, so each member gets a managed policy, attached to nothing and passed with the member's session only.
        res = TF.split('resource "aws_iam_policy" "nextjs_ct_member" {', 1)[1].split("\n}\n", 1)[0]
        self.assertIn("for_each    = toset(local.nextjs_ct_member_users)", res)
        self.assertIn("concat(local.nextjs_ct_allow_all, local.nextjs_ct_deny_member, try(local.nextjs_ct_own_boxes[each.key], "
                      "local.nextjs_ct_no_boxes), local.nextjs_ct_member_machine_ssh[each.key])", res)
        self.assertIn("nextjs_ct_member_users = [for u in local.nextjs_users : u if !contains(local.nextjs_ct_owners, u)]", TF)
        self.assertIn("session_policy_arns = { for u, pol in aws_iam_policy.nextjs_ct_member : u => [pol.arn] }", TF)
        self.assertNotIn("aws_iam_role_policy_attachment\" \"nextjs_ct_member", TF)
        self.assertIn('tags        = { Purpose = "session-policy", AttachedTo = "none", Account = each.key }', res)
        own = TF.split("nextjs_ct_own_boxes = {", 1)[1].split("\n  ] }\n", 1)[0]
        box = TF.split("nextjs_ct_member_box_all = {", 1)[1].split("\n  } }\n", 1)[0]
        # A member pbox is in Ohio with no route from this VPC: no private address of one in PermitOpen or the firewall.
        self.assertIn("ips = [local.member_wbox_ips[p], local.member_gbox_ips[p]]", box)
        self.assertNotIn("member_pbox_ips", TF)
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
                          sp + "macbook-ssh-access-nextjs-ejc3-*", sp + "colton-mac-ssh-access-nextjs-ejc3-*"]},
            {"Sid": "OwnerOnlyWboxOutput", "Effect": "Deny", "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": ["arn:aws:s3:::wbox-output-111111111111", "arn:aws:s3:::wbox-output-111111111111/*"]},
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
        # Every other member also carries the deny on the members' own machine tokens (Colton's Mac, Colin's PC).
        other = managed + [{"Sid": "MemberMachineTokensOwnOnly", "Effect": "Deny", "Action": "secretsmanager:GetSecretValue",
                            "Resource": [sp + "colton-mac-ssh-access-nextjs-colton-*", sp + "colin-pc-ssh-access-nextjs-colin-*"]}]
        size = len(json.dumps({"Version": "2012-10-17", "Statement": other}, separators=(",", ":")))
        self.assertLess(size, 5800, size)

    def test_members_machine_tokens_are_readable_only_from_their_own_containers(self):
        # Security review of #510: every container's session is nextjs-dev-role, which reads every nextjs token for the
        # members' machines. ejc3's are owner-only (above); colton's (Colton's Mac) and colin's (Colin's PC) are each
        # denied to every member but its owner, boxes or not.
        tokens = TF.split("nextjs_ct_member_machine_tokens = {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('colton = "colton-mac-ssh-access-nextjs-colton"', tokens)
        self.assertIn('colin  = "colin-pc-ssh-access-nextjs-colin"', tokens)
        stmt = TF.split("nextjs_ct_member_machine_ssh = {", 1)[1].split("\n\n", 1)[0]
        self.assertIn("for u in local.nextjs_ct_member_users : u =>", stmt)
        self.assertIn(':secret:${secret}-*" if owner != u]', stmt)
        self.assertIn(": s if length(s.Resource) > 0] }", stmt)
        self.assertIn('Action   = "secretsmanager:GetSecretValue"', stmt)
        self.assertIn('Effect   = "Deny"', stmt)
        res = TF.split('resource "aws_iam_policy" "nextjs_ct_member" {', 1)[1].split("\n}\n", 1)[0]
        self.assertIn("local.nextjs_ct_member_machine_ssh[each.key])", res)
        self.assertNotIn("nextjs_ct_colton_mac_ssh", TF)


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
                   NEXTJS_CT_IMDS_USERS=json.dumps({"127.0.0.1": "alice", "127.0.0.2": "boss", "127.0.0.4": "dave"}), CALLS=str(cls.calls))
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

    def test_a_member_without_a_policy_arn_is_refused_never_given_the_owners_session(self):
        # Security review of #502: only the owner flag makes a session without a managed policy.
        tok = self.token("127.0.0.4")
        status, body = self.req("GET", "/latest/meta-data/iam/security-credentials/nextjs-dev-role", {"X-aws-ec2-metadata-token": tok}, "127.0.0.4")
        self.assertEqual(status, 500)
        self.assertNotIn("AKIAFAKE", body)
        self.assertNotIn("ct-dave", self.calls.read_text() if self.calls.exists() else "")

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

    def test_a_pinned_service_routes_to_its_owner_without_a_project_record(self):
        self.config["pinned_routes"]["opencode.cc-games.dev"] = {
            "zone": "cc-games.dev", "port": 49374, "user": "boss", "dir": str(self.d / "home/boss"),
        }
        result, games, _ = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  - hostname: opencode.cc-games.dev\n    service: http://10.88.3.2:49374", games)

    def test_protected_routes_require_their_access_audience_at_the_connector(self):
        self.config["pinned_routes"]["opencode.cc-games.dev"] = {
            "zone": "cc-games.dev", "port": 49375, "user": "boss", "dir": str(self.d / "home/boss"),
        }
        self.config["route_access"] = {"opencode.cc-games.dev": {"teamName": "example-team", "audTag": ["owner-audience"]}}
        result, games, _ = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('    originRequest:\n      access:\n        required: true\n        teamName: "example-team"\n        audTag:\n          - "owner-audience"', games)

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
        self.assertIn('iifname "ve-alice" ip daddr { 10.0.69.11, 172.31.47.227 } drop', nft)
        self.assertIn('iifname "ve-carol" ip daddr { 10.0.69.10, 172.31.47.226 } drop', nft)
        self.assertNotIn('iifname "ve-boss" ip daddr {', nft)
        self.assertLess(forward.index('iifname "ve-alice" ip daddr { 10.0.69.11'), forward.index("ct state established,related accept"))

    def test_sshd(self):
        conf = self.generate("nextjs-ct-sshd", "out")
        alice = conf.split("Match User alice\n", 1)[1].split("\nMatch", 1)[0]
        boss = conf.split("Match User boss\n", 1)[1].split("\nMatch", 1)[0]
        self.assertIn("ForceCommand /usr/local/sbin/nextjs-ct-enter", alice)
        carol = conf.split("Match User carol\n", 1)[1].split("\nMatch", 1)[0]
        # A member may forward to their own container and their own three boxes, nothing else.
        self.assertIn("PermitOpen 10.88.1.2:* 10.0.69.10:* 172.31.47.226:*\n", alice + "\n")
        self.assertIn("PermitOpen 10.88.2.2:* 10.0.69.11:* 172.31.47.227:*\n", carol + "\n")
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

    def test_tmp_is_a_disk_directory_per_container_not_nspawns_small_tmpfs(self):
        # nspawn's default /tmp is a tmpfs at 10% of RAM (1.6 GB); agents filled it in a day and opencode could not start.
        machine = (HOST / "nextjs-ct-machine").read_text()
        files = machine.split("[Files]", 1)[1].split("[Network]", 1)[0]
        self.assertIn("Bind=$TD:/tmp\n", files)
        self.assertNotIn("Bind=$TD:/tmp:idmap", files)
        self.assertIn('TD=/var/lib/nextjs-ct/tmp/$U', machine)
        self.assertIn('install -d -m 1777 -o "$SHIFT" -g "$SHIFT" "$TD"', machine)
        # its parent is nosuid,nodev on the host, and exec stays allowed (native code is loaded from /tmp)
        unit = machine.split("What=/var/lib/nextjs-ct/tmp", 1)[1].split("[Install]", 1)[0]
        self.assertIn("Options=bind,nosuid,nodev\n", unit)
        self.assertNotIn("noexec", unit)
        # cleared at boot and aged out after 7 days by the container's own tmpfiles rule
        provision = (CT / "provision").read_text()
        self.assertIn("printf 'D /tmp 1777 root root 7d\\n' > /etc/tmpfiles.d/tmp.conf", provision)

    def test_home_is_nosuid_on_the_host_and_idmapped_in_the_container(self):
        machine = (HOST / "nextjs-ct-machine").read_text()
        self.assertIn("Options=bind,nosuid,nodev", machine)
        self.assertIn("Bind=/home/$U:/home/$U:idmap", machine)
        self.assertIn("PrivateUsers=$SHIFT:65536", machine)
        files = machine.split("[Files]", 1)[1].split("[Network]", 1)[0]
        self.assertIn("PrivateUsersOwnership=chown", files)  # a [Files] key: in [Exec] systemd ignores it


class MacTests(unittest.TestCase):
    """Each account's container reaches its Macs the way every box does (owner, 2026-10-18: "cloudflare only"):
    `mac` for ejc3 alone, `colton-mac` for colton and ejc3 (2026-10-19), each with that account's own token."""

    def test_the_shared_cloudflare_client_runs_per_alias_with_the_accounts_own_token(self):
        body = TF.split("nextjs_ct_mac_ssh = <<-MACSSH\n", 1)[1].split("\nMACSSH\n", 1)[0]
        for line in ('MAC_USER="$1"', 'MAC_ALIAS="$2"', 'MAC_SECRET="$3"'):
            self.assertIn(line, body)
        self.assertLess(body.index("${local.mac_cloudflared_install}"), body.index("${local.mac_ssh_client_setup}"))
        clients = TF.split("nextjs_ct_mac_clients = {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('ejc3   = { mac = "macbook-ssh-access-nextjs-ejc3", colton-mac = "colton-mac-ssh-access-nextjs-ejc3" }', clients)
        self.assertIn('colton = { colton-mac = "colton-mac-ssh-access-nextjs-colton" }', clients)
        self.assertIn('colin  = { colin-pc = "colin-pc-ssh-access-nextjs-colin" }', clients)
        self.assertEqual(clients.count(" = {"), 3)  # nobody else
        self.assertIn("macs       = lookup(local.nextjs_ct_mac_clients, u, {})", TF)
        self.assertNotIn("macbook-ssh.sh", TF + (CT / "provision").read_text())
        snippet = TF.split("nextjs_ct_mac_ssh = <<-MACSSH", 1)[1].split("\nMACSSH", 1)[0]
        install = snippet.index("${local.mac_cloudflared_install}")
        check = snippet.index('/home/$MAC_USER/.local/bin/cloudflared" | sha256sum -c - >/dev/null 2>&1 ||')
        self.assertLess(install, check, "a cloudflared that did not install fails the snippet before ssh is configured")
        self.assertLess(check, snippet.index("${local.mac_ssh_client_setup}"))

    def test_provision_gives_windows_mcp_for_each_listed_pc_and_nothing_else(self):
        self.assertIn("windows_mcp = [for a in keys(lookup(local.nextjs_ct_mac_clients, u, {})) : a if contains(local.windows_mcp_pcs, a)]", TF)
        self.assertIn('"/var/lib/nextjs-ct/shared/${trimprefix(f, "scripts/")}"', TF)
        prov = (CT / "provision").read_text()
        loop = prov[prov.index("while read -r alias; do"):]
        loop = loop[:loop.index("\n", loop.index("done < <(cfg --arg u \"$U\" '.users[$u].windows_mcp")) + 1]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps({"users": {
                "colin": {"windows_mcp": ["colin-pc"]}, "colton": {"windows_mcp": []}, "connor": {}}}))
            (d / "windows").mkdir()
            (d / "windows" / "windows-mcp-client-setup.sh").write_text('echo "$(whoami-stub) $@" >> "$OUT"\n'.replace("$(whoami-stub)", "$U"))
            got = {}
            for u in ("colin", "colton", "connor"):
                out = d / f"{u}.out"
                script = ("set -euo pipefail\nSHARED=%s\nU=%s\nOUT=%s\nexport U OUT\nFAILED=0\nwarn() { FAILED=1; }\n"
                          "as_user() { \"$@\"; }\ncfg() { jq -r \"$@\" \"$SHARED/config.json\"; }\n%s") % (d, u, out, loop)
                r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                got[u] = out.read_text().split("\n")[:-1] if out.exists() else []
            self.assertEqual(got, {"colin": ["colin colin-pc"], "colton": [], "connor": []})

    def test_provision_configures_each_listed_mac_and_nothing_else(self):
        prov = (CT / "provision").read_text()
        loop = prov[prov.index("while read -r alias secret; do"):]
        loop = loop[:loop.index("\n", loop.index("done < <(cfg --arg u \"$U\" '.users[$u].macs")) + 1]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps({"users": {
                "ejc3": {"macs": {"mac": "macbook-ssh-access-nextjs-ejc3", "colton-mac": "colton-mac-ssh-access-nextjs-ejc3"}},
                "colton": {"macs": {"colton-mac": "colton-mac-ssh-access-nextjs-colton"}}, "connor": {}}}))
            (d / "snippets").mkdir()
            (d / "snippets" / "mac-ssh.sh").write_text('echo "$@" >> "$OUT"\n')
            got = {}
            for u in ("ejc3", "colton", "connor"):
                out = d / f"{u}.out"
                script = f'CONFIG={d}/config.json; SHARED={d}; U={u}; export OUT={out}\nwarn() {{ echo "$*" >&2; }}\n' \
                    + 'cfg() { jq -r "$@" "$CONFIG"; }\n' + loop
                subprocess.run(["bash", "-c", script], check=True)
                got[u] = sorted(out.read_text().splitlines()) if out.exists() else []
        self.assertEqual(got["ejc3"], ["ejc3 colton-mac colton-mac-ssh-access-nextjs-ejc3", "ejc3 mac macbook-ssh-access-nextjs-ejc3"])
        self.assertEqual(got["colton"], ["colton colton-mac colton-mac-ssh-access-nextjs-colton"])
        self.assertEqual(got["connor"], [])

    def test_a_failed_mac_setup_fails_provision(self):
        """warn's FAILED=1 must reach provision's own shell (no pipeline subshell), so the container is not marked ready."""
        prov = (CT / "provision").read_text()
        loop = prov[prov.index("while read -r alias secret; do"):]
        loop = loop[:loop.index("\n", loop.index("done < <(cfg")) + 1]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps({"users": {"ejc3": {"macs": {"mac": "s"}}}}))
            (d / "snippets").mkdir()
            (d / "snippets" / "mac-ssh.sh").write_text("exit 1\n")
            script = f'CONFIG={d}/config.json; SHARED={d}; U=ejc3; FAILED=""\nwarn() {{ FAILED=1; }}\n' \
                + 'cfg() { jq -r "$@" "$CONFIG"; }\n' + loop + 'echo "FAILED=$FAILED"\n'
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        self.assertIn("FAILED=1", r.stdout)

    def test_containers_get_the_jumpboxes_terraform(self):
        """The version main.tf requires (both jumpboxes, drift.yml); a full sha256 of the arm64 zip."""
        m = re.search(r'nextjs_ct_terraform = \{ version = "([0-9.]+)", sha256 = "([0-9a-f]{64})" \}', TF)
        self.assertIsNotNone(m)
        version = m.group(1)
        self.assertIn('required_version = "= %s"' % version, (ROOT / "main.tf").read_text())
        self.assertIn('TF_VERSION="%s"' % version, (ROOT / "jumpbox2-user-data.tf").read_text())
        self.assertIn("terraform_version: %s" % version, (ROOT / ".github/workflows/drift.yml").read_text())
        self.assertIn("    terraform           = local.nextjs_ct_terraform\n", TF)

    def _provision_terraform(self, d, version="1.10.3", reports="Terraform v1.10.3", sha=None, stamp=None, have=False):
        """Runs provision's Terraform block against a fake download; returns (installed?, curl calls, FAILED)."""
        import hashlib, zipfile
        prov = (CT / "provision").read_text()
        block = prov[prov.index("# Terraform, the version"):prov.index("# claw-master client")]
        block = block.replace("/usr/local/bin/", f"{d}/bin/").replace("/var/lib/nextjs-ct", f"{d}/state")
        (d / "bin").mkdir(exist_ok=True)
        (d / "fakes").mkdir(exist_ok=True)
        with zipfile.ZipFile(d / "tf.zip", "w") as z:
            info = zipfile.ZipInfo("terraform")
            info.external_attr = 0o755 << 16
            z.writestr(info, f"#!/bin/bash\necho '{reports}'\necho 'on linux_arm64'\n")
        real = hashlib.sha256((d / "tf.zip").read_bytes()).hexdigest()
        if have:
            fake(d / "bin", "terraform", "echo old\n")
        if stamp:
            (d / "state").mkdir(exist_ok=True)
            (d / "state" / "terraform.sha256").write_text((real if stamp == "real" else stamp) + "\n")
        (d / "config.json").write_text(json.dumps({"terraform": {"version": version, "sha256": sha or real}}))
        fake(d / "fakes", "uname", "echo aarch64\n")
        fake(d / "fakes", "curl", f'echo "$@" >> {d}/curl.log\nfor a; do [ "$prev" = -o ] && cp {d}/tf.zip "$a"; prev=$a; done\n')
        script = f'PATH={d}/fakes:$PATH; CONFIG={d}/config.json; FAILED=""\nwarn() {{ echo "$*" >&2; FAILED=1; }}\n' \
            + 'cfg() { jq -r "$@" "$CONFIG"; }\n' + block + 'echo "FAILED=$FAILED"\n'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        installed = (d / "bin" / "terraform").exists() and "Terraform v" in (d / "bin" / "terraform").read_text()
        calls = (d / "curl.log").read_text().splitlines() if (d / "curl.log").exists() else []
        return installed, calls, "FAILED=1" in r.stdout, real

    def test_provision_installs_the_pinned_terraform_only_when_it_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            installed, calls, failed, real = self._provision_terraform(d)
            self.assertEqual((installed, failed), (True, False))
            self.assertEqual(len(calls), 1)
            self.assertIn("https://releases.hashicorp.com/terraform/1.10.3/terraform_1.10.3_linux_arm64.zip", calls[0])
            self.assertEqual((d / "state" / "terraform.sha256").read_text().strip(), real)
        for why, kw in (("zip sha256 differs", {"sha": "0" * 64}),
                        ("binary is another version", {"reports": "Terraform v1.9.0"})):
            with self.subTest(why), tempfile.TemporaryDirectory() as d:
                d = Path(d)
                installed, calls, failed, _ = self._provision_terraform(d, **kw)
                self.assertEqual((installed, failed, len(calls)), (False, True, 1))
                self.assertFalse((d / "state" / "terraform.sha256").exists())

    def test_provision_leaves_a_verified_terraform_alone_and_replaces_a_stale_one(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            installed, calls, failed, _ = self._provision_terraform(d, have=True, stamp="real")
            self.assertEqual((installed, calls, failed), (False, [], False))  # the existing binary is kept, no download
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            installed, calls, failed, _ = self._provision_terraform(d, have=True, stamp="f" * 64)
            self.assertEqual((installed, len(calls), failed), (True, 1, False))

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



class ClawMasterAgentTests(unittest.TestCase):
    """Every Claude Code in a container reaches the pool, the desktop app's SSH sessions too (they start their own Claude Code
    with the person's own login, which spent that person's own subscription): claw-master-agent-setup runs the agent for an
    enrolled account and puts its three entries in a managed settings drop-in. Runs for real against a fake systemctl and a
    fake claw-master; `ss` is real, so "ready" is checked against an actual listening socket and its owner."""

    SETUP = CT / "claw-master-agent-setup"
    UNITS = CT / "units"
    TOKEN = "TESTTOKEN"
    USER = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    GROUP = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()

    def world(self, enrolled=True, listening=True, owner_is_unit=True, env_output=None):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(d)])
        home, bin_, state = d / "home", d / "bin", d / "state"
        for x in (bin_, state, home / ".config/claude-master"):
            x.mkdir(parents=True)
        if enrolled:
            (home / ".config/claude-master/client.pem").write_text("cert")
        fake(bin_, "systemctl", f"""echo "$*" >> "{d}/systemctl.log"
S="{state}"
case "$1" in
  is-active) [ -e "$S/active" ] ;;
  show) case "$3" in ActiveEnterTimestamp) cat "$S/started_at" 2>/dev/null || echo ;; MainPID) cat "$S/mainpid" 2>/dev/null || echo 0 ;; esac ;;
  start|restart) touch "$S/active"; date '+%a %Y-%m-%d %H:%M:%S %Z' > "$S/started_at" ;;
  disable) rm -f "$S/active" ;;
esac
""")
        out = env_output if env_output is not None else (
            'echo "HTTPS_PROXY=http://claw-master:%s@$4"\necho "NODE_EXTRA_CA_CERTS=$8/ca.pem"\n'
            'echo "NO_PROXY=localhost,127.0.0.1,::1"\n' % self.TOKEN)
        cm = fake(bin_, "claw-master", 'echo "$*" >> "%s"\n[ "$1 $2" = "agent env" ] || exit 9\n%s' % (d / "cm.log", out))
        (d / "config.json").write_text(json.dumps({"claw_master": {"server": "192.0.2.10:8443"}}))
        if listening:
            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen(16)
            self.addCleanup(srv.close)
            port = srv.getsockname()[1]
        else:
            with socket.socket() as sock:   # a port nothing listens on
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        # The unit's main process: this test process owns the listening socket, so its pid is the right answer.
        (state / "mainpid").write_text(str(os.getpid() if owner_is_unit else 1))
        env = {"PATH": "%s:/usr/bin:/bin:/usr/sbin:/sbin" % bin_, "CM_AGENT_HOME": str(home),
               "CM_AGENT_CONFIG": str(d / "config.json"), "CM_AGENT_BIN": str(cm),
               "CM_AGENT_ENVFILE": str(d / "etc/claw-master-agent.env"),
               "CM_AGENT_DROPIN": str(d / "etc/claude-code/managed-settings.d/50-claw-master-agent.json"),
               "CM_AGENT_LISTEN": "127.0.0.1:%d" % port, "CM_AGENT_TRIES": "4", "CM_AGENT_LOCK": str(d / "setup.lock")}
        return types.SimpleNamespace(d=d, home=home, env=env, port=port, state=state, pem=home / ".config/claude-master/client.pem",
                        dropin=Path(env["CM_AGENT_DROPIN"]), log=d / "systemctl.log")

    def run_setup(self, w, *extra):
        return subprocess.run(["bash", str(self.SETUP), self.USER, *extra], env=w.env, capture_output=True, text=True, timeout=60)

    def ours(self, w):
        return {"env": {"HTTPS_PROXY": "http://claw-master:%s@127.0.0.1:%d" % (self.TOKEN, w.port),
                        "NODE_EXTRA_CA_CERTS": "%s/.config/claude-master/ca.pem" % w.home,
                        "NO_PROXY": "localhost,127.0.0.1,::1"}}

    def calls(self, w, verb=None):
        lines = w.log.read_text().splitlines() if w.log.exists() else []
        return [l for l in lines if verb is None or l.split()[0] == verb]

    def test_an_enrolled_account_gets_the_agent_and_a_managed_drop_in(self):
        w = self.world()
        settings = w.home / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text('{"theme": "dark"}')
        r = self.run_setup(w)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(w.dropin.read_text()), self.ours(w))
        st = w.dropin.stat()
        self.assertEqual(oct(st.st_mode & 0o777), "0o640", "the proxy URL carries the agent's token")
        self.assertEqual(subprocess.run(["stat", "-c", "%G", str(w.dropin)], capture_output=True, text=True).stdout.strip(), self.GROUP)
        unit = "claw-master-agent@%s.service" % self.USER
        self.assertEqual(self.calls(w, "enable") + self.calls(w, "start"), ["enable " + unit, "start " + unit])
        self.assertEqual((w.d / "etc/claw-master-agent.env").read_text(),
                         "CLAW_MASTER_SERVER=192.0.2.10:8443\nCLAW_MASTER_AGENT_LISTEN=127.0.0.1:%d\n" % w.port)
        self.assertIn("agent env --listen 127.0.0.1:%d --server 192.0.2.10:8443 --dir %s/.config/claude-master" % (w.port, w.home),
                      (w.d / "cm.log").read_text())
        self.assertEqual(settings.read_text(), '{"theme": "dark"}', "the person's own settings are never touched")
        self.assertEqual(list(w.dropin.parent.glob(".*")), [], "no temporary file is left behind")

    def test_a_second_run_changes_nothing_and_restarts_nothing(self):
        w = self.world()
        os.utime(w.pem, (time.time() - 3600, time.time() - 3600))
        self.assertEqual(self.run_setup(w).returncode, 0)
        before = w.dropin.stat()
        r = self.run_setup(w)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = w.dropin.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns),
                         "Claude Code reloads a changed settings file: a run that changes nothing must not touch it")
        self.assertEqual(self.calls(w, "restart"), [])
        self.assertEqual(len(self.calls(w, "start")), 1)

    def test_a_certificate_newer_than_the_agent_restarts_it_and_so_does_a_new_build(self):
        w = self.world()
        os.utime(w.pem, (time.time() - 3600, time.time() - 3600))
        self.assertEqual(self.run_setup(w).returncode, 0)
        (w.state / "started_at").write_text(time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() - 600)) + "\n")
        os.utime(w.pem)                     # renewed after the agent started
        self.assertEqual(self.run_setup(w).returncode, 0)
        self.assertEqual(len(self.calls(w, "restart")), 1)
        self.assertEqual(self.run_setup(w).returncode, 0)
        self.assertEqual(len(self.calls(w, "restart")), 1, "once restarted, the same certificate restarts nothing")
        self.assertEqual(self.run_setup(w, "--restart").returncode, 0)
        self.assertEqual(len(self.calls(w, "restart")), 2)

    def test_runs_started_together_take_turns(self):
        """Provision, the path unit and the timer can start a run at the same moment (enabling the timer starts one)."""
        w = self.world()
        procs = [subprocess.Popen(["bash", str(self.SETUP), self.USER], env=w.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(4)]
        results = [(p.wait(timeout=60), p.stderr.read()) for p in procs]
        for p in procs:
            p.stdout.close(); p.stderr.close()
        self.assertEqual([rc for rc, _ in results], [0, 0, 0, 0], results)
        self.assertEqual(json.loads(w.dropin.read_text()), self.ours(w))

    def test_a_certificate_missing_for_a_moment_is_not_unenrollment(self):
        """A renewal swaps the directory with two renames; a run that lands between them must wait, not tear down."""
        w = self.world(enrolled=False)
        threading.Timer(0.6, lambda: w.pem.write_text("cert")).start()
        r = self.run_setup(w)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(w, "disable"), [])
        self.assertEqual(json.loads(w.dropin.read_text()), self.ours(w))

    def test_an_account_that_is_not_enrolled_has_no_agent_and_no_drop_in(self):
        w = self.world()
        self.assertEqual(self.run_setup(w).returncode, 0)
        w.pem.unlink()
        r = self.run_setup(w)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(w.dropin.exists())
        self.assertEqual(self.calls(w, "disable"), ["disable --now claw-master-agent@%s.service" % self.USER])

    def test_an_enrolled_account_with_a_fault_keeps_its_drop_in(self):
        """Every fault fails and leaves the drop-in in place: Claude then fails instead of using the person's own login."""
        w = self.world()
        self.assertEqual(self.run_setup(w).returncode, 0)
        want = w.dropin.read_text()
        cm = Path(w.env["CM_AGENT_BIN"])
        cm.rename(cm.with_name("gone"))
        r = self.run_setup(w)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is missing", r.stderr)
        self.assertEqual((w.dropin.read_text(), self.calls(w, "disable")), (want, []))
        cm.with_name("gone").rename(cm)
        cm.write_text('#!/bin/bash\necho "HTTPS_PROXY=http://elsewhere:1"\n')
        r = self.run_setup(w)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unexpected claw-master agent env output", r.stderr)
        self.assertEqual(w.dropin.read_text(), want)

    def test_ready_means_the_units_own_process_listens(self):
        for why, kw in (("nothing listens", {"listening": False}), ("another process listens", {"owner_is_unit": False})):
            w = self.world(**kw)
            r = self.run_setup(w)
            self.assertNotEqual(r.returncode, 0, why)
            self.assertIn("is not the listener", r.stderr, why)
            self.assertEqual(json.loads(w.dropin.read_text()), self.ours(w), why + ": the drop-in still keeps Claude off the person's login")

    def test_the_units_run_the_agent_and_set_it_up_again(self):
        agent = (self.UNITS / "claw-master-agent@.service").read_text()
        for line in ("User=%i", "EnvironmentFile=/etc/nextjs-ct/claw-master-agent.env", "Restart=always",
                     "ExecStart=/usr/local/bin/claw-master agent --listen ${CLAW_MASTER_AGENT_LISTEN} "
                     "--server ${CLAW_MASTER_SERVER} --dir /home/%i/.config/claude-master"):
            self.assertIn(line + "\n", agent)
        path = (self.UNITS / "claw-master-agent@.path").read_text()
        self.assertIn("PathChanged=/home/%i/.config/claude-master/client.pem\n", path)
        self.assertIn("Unit=claw-master-agent-setup@%i.service\n", path)
        timer = (self.UNITS / "claw-master-agent@.timer").read_text()
        self.assertIn("OnUnitActiveSec=1h\n", timer)
        self.assertIn("Unit=claw-master-agent-setup@%i.service\n", timer)
        oneshot = (self.UNITS / "claw-master-agent-setup@.service").read_text()
        self.assertIn("ExecStart=/usr/local/bin/claw-master-agent-setup %i\n", oneshot)
        setup = self.SETUP.read_text()
        for default in ("ENVFILE=${CM_AGENT_ENVFILE:-/etc/nextjs-ct/claw-master-agent.env}",
                        "DROPIN=${CM_AGENT_DROPIN:-/etc/claude-code/managed-settings.d/50-claw-master-agent.json}",
                        "LISTEN=${CM_AGENT_LISTEN:-127.0.0.1:8445}"):
            self.assertIn(default, setup)

    def test_provision_sets_up_the_agent_for_its_account_and_restarts_it_for_a_new_build(self):
        prov = (CT / "provision").read_text()
        self.assertIn('for t in "claw-master-agent@$U.path" "claw-master-agent@$U.timer"; do', prov)
        self.assertIn('/usr/local/bin/claw-master-agent-setup "$U" ${CM_UPDATED:+--restart} || warn', prov)
        self.assertLess(prov.index('CM_UPDATED=""'), prov.index("&& CM_UPDATED=1"))
        self.assertLess(prov.index("&& CM_UPDATED=1"), prov.index('claw-master-agent-setup "$U"'))
        self.assertLess(prov.index('U=$(cat "$ME/user")'), prov.index('claw-master-agent-setup "$U"'))

class NoContainerRestartTests(unittest.TestCase):
    """A re-run of setup (any change to the published script) must not restart the containers. Every
    systemd-nspawn@<user> unit Requires= the firewall and its home mount, and restarting a required unit restarts the
    containers that require it: on 2026-10-20 two routine applies ended every session (13:54, 14:04)."""

    MACHINE = (ROOT / "scripts/nextjs-ct/host/nextjs-ct-machine").read_text()

    def required_units(self):
        req = re.search(r"^Requires=(.*)$", self.MACHINE, re.M).group(1).split()
        self.assertIn("nextjs-ct-firewall.service", req)
        return req

    def test_setup_never_restarts_a_unit_the_containers_require(self):
        hosts = [TF] + [f.read_text() for f in (ROOT / "scripts/nextjs-ct/host").iterdir() if f.is_file()]
        for unit in self.required_units():
            name = unit.replace("$HOME_UNIT", "").strip()
            if not name:
                # the home mount unit: only ever enabled/started, never restarted
                for text in hosts:
                    self.assertNotRegex(text, r"systemctl (restart|try-restart|reload-or-restart)[^\n]*\$HOME_UNIT")
                continue
            for text in hosts:
                self.assertNotRegex(text, r"systemctl (restart|try-restart|reload-or-restart)[^\n]*" + re.escape(name))
                # stopping it happens only in the fail-closed branch of a rejected reload, in setup itself
                self.assertLessEqual(len(re.findall(r"systemctl stop[^\n]*" + re.escape(name), text)), 1 if text is TF else 0)

    def test_firewall_rules_are_reloaded_by_running_the_script(self):
        self.assertIn("if ! { systemctl start nextjs-ct-firewall.service && /usr/local/sbin/nextjs-ct-firewall; }; then", TF)

    def test_a_failed_reload_stops_the_firewall_so_the_containers_fail_closed(self):
        block = TF.split("if ! { systemctl start nextjs-ct-firewall.service && /usr/local/sbin/nextjs-ct-firewall; }; then", 1)[1].split("\nfi", 1)[0]
        self.assertIn("systemctl stop nextjs-ct-firewall.service", block)
        self.assertIn("exit 1", block)

class MemoryLimitRemovalTests(unittest.TestCase):
    """Run the real reset commands against systemctl so legacy live/persistent caps cannot survive."""

    def command(self):
        return SCRIPT.split("# ---- shared container memory\n", 1)[1].split("\n# ----", 1)[0]

    def run_command(self, failure=0, command=None, remove_failure=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            filesystem = root / "fs"
            command = self.command() if command is None else command
            unit = "systemd-nspawn@alice.service" if "systemd-nspawn@" in command else "machine.slice"
            controls = [filesystem / "run/systemd/system.control", filesystem / "etc/systemd/system.control"]
            properties = ("MemoryHigh", "MemoryMax", "MemorySwapMax")
            for control in controls:
                directory = control / (unit + ".d")
                directory.mkdir(parents=True)
                for name, value in zip(properties, ("80%", "75%" if unit.startswith("systemd-nspawn") else "85%", "50%")):
                    (directory / ("50-" + name + ".conf")).write_text(name + "=infinity\n")
                    (directory / ("50-" + name + "Scale.conf")).write_text(name + "=" + value + "\n")
                (directory / "40-CPUWeight.conf").write_text("CPUWeight=100\n")
                unrelated = control / "unrelated.service.d"
                unrelated.mkdir()
                (unrelated / "50-MemoryMaxScale.conf").write_text("MemoryMax=50%\n")
            helper = root / "fake-control.py"
            helper.write_text("""import json,os,sys
from pathlib import Path
root=Path(os.environ["MEMORY_FS"])
unit=os.environ["MEMORY_UNIT"]
operation=sys.argv[1]
with open(os.environ["MEMORY_CALLS"],"a") as f:f.write(" ".join(sys.argv[1:])+"\\n")
if operation=="rm":
    if int(os.environ["MEMORY_REMOVE_FAILURE"]):sys.exit(int(os.environ["MEMORY_REMOVE_FAILURE"]))
    for value in sys.argv[2:]:
        if value=="--":continue
        p=Path(value)
        assert p.parent.resolve().is_relative_to(root.resolve()), "refusing to touch real filesystem"
        p.unlink()
elif operation=="set-property":
    if int(os.environ["MEMORY_FAILURE"]):sys.exit(int(os.environ["MEMORY_FAILURE"]))
    assert sys.argv[2]==unit
    directory=root/"etc/systemd/system.control"/(unit+".d")
    for value in sys.argv[3:]:
        key=value.split("=",1)[0]
        (directory/("50-"+key+".conf")).write_text(value+"\\n")
elif operation=="daemon-reload":
    files={}
    for prefix in ("run","etc"):
        directory=root/prefix/"systemd/system.control"/(unit+".d")
        files.update({p.name:p for p in directory.glob("*.conf")})
    effective={}
    for name in sorted(files):
        for line in files[name].read_text().splitlines():
            if "=" in line:
                key,value=line.split("=",1)
                if key.startswith("Memory"):effective[key]=value
    with open(os.environ["MEMORY_RELOADS"],"a") as f:f.write(json.dumps(effective)+"\\n")
else:raise AssertionError(operation)
""")
            fake(root, "systemctl", 'exec /usr/bin/python3 "$MEMORY_FAKE" "$@"\n')
            fake(root, "rm", 'exec /usr/bin/python3 "$MEMORY_FAKE" rm "$@"\n')
            env = dict(os.environ, PATH=str(root) + ":" + os.environ["PATH"], U="alice",
                       MEMORY_FS=str(filesystem), MEMORY_FAKE=str(helper), MEMORY_UNIT=unit,
                       MEMORY_CALLS=str(root / "calls"), MEMORY_RELOADS=str(root / "reloads"),
                       MEMORY_FAILURE=str(failure), MEMORY_REMOVE_FAILURE=str(remove_failure))
            command = command.replace("/etc/systemd/system.control", str(controls[1])).replace(
                "/run/systemd/system.control", str(controls[0]))
            shell_options = re.search(r"^set -[^\n]+$", SCRIPT, re.M).group()
            result = subprocess.run(["bash"], input=shell_options + '\nsystemctl daemon-reload\n' + command
                                    + '\nsystemctl daemon-reload\nsystemctl daemon-reload\necho completed\n',
                                    text=True, capture_output=True, env=env)
            reloads = [json.loads(line) for line in (root / "reloads").read_text().splitlines()]
            self.assertNotEqual(reloads[0]["MemoryHigh"], "infinity", "fixture reproduces Scale precedence")
            if result.returncode == 0:
                for values in reloads[-2:]:
                    self.assertEqual(values, {name: "infinity" for name in properties})
                for control in controls:
                    self.assertFalse(list((control / (unit + ".d")).glob("*Scale.conf")))
                    self.assertTrue((control / (unit + ".d") / "40-CPUWeight.conf").exists())
                    self.assertTrue((control / "unrelated.service.d/50-MemoryMaxScale.conf").exists())
            calls = (root / "calls").read_text().splitlines()
            return result, [line for line in calls if line.startswith("set-property ")]

    def assert_unlimited_properties(self, call, unit):
        args = call.split()
        self.assertEqual(args[:2], ["set-property", unit])
        self.assertEqual(dict(arg.split("=", 1) for arg in args[2:]), {
            "MemoryHigh": "infinity", "MemoryMax": "infinity", "MemorySwapMax": "infinity",
        })

    def test_shared_parent_resets_live_and_persistent_limits(self):
        result, calls = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "completed")
        self.assertEqual(len(calls), 1)
        self.assert_unlimited_properties(calls[0], "machine.slice")
        self.assertLess(SCRIPT.index(self.command()), SCRIPT.index('/usr/local/sbin/nextjs-ct-machine ${u}'))

    def container_command(self):
        machine = (HOST / "nextjs-ct-machine").read_text()
        command = machine.split("# ---- clear legacy memory scales\n", 1)[1].split("\nnetworkctl reload", 1)[0]
        self.assertNotRegex(machine, r'systemctl (restart|try-restart)[^\n]*systemd-nspawn')
        dropin = machine.split('cat > "/etc/systemd/system/systemd-nspawn@$U.service.d/nextjs-ct.conf" <<EOF\n', 1)[1].split("\nEOF", 1)[0]
        for prop in ("MemoryHigh", "MemoryMax", "MemorySwapMax"):
            self.assertIn(prop + "=infinity", dropin)
        self.assertNotIn("75%", dropin)
        return command

    def test_each_container_clears_legacy_control_overrides_without_restart(self):
        result, calls = self.run_command(command=self.container_command())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "completed")
        self.assertEqual(len(calls), 1)
        self.assert_unlimited_properties(calls[0], "systemd-nspawn@alice.service")

    def test_failed_reset_stops_setup_instead_of_recording_success(self):
        for command in (self.command(), self.container_command()):
            with self.subTest(command=command.splitlines()[0]):
                result, calls = self.run_command(failure=17, command=command)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("completed", result.stdout)

    def test_failed_legacy_file_removal_stops_before_reset(self):
        for command in (self.command(), self.container_command()):
            with self.subTest(command=command.splitlines()[0]):
                result, calls = self.run_command(remove_failure=17, command=command)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])
                self.assertNotIn("completed", result.stdout)


if __name__ == "__main__":
    unittest.main(buffer=True)
