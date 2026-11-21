#!/usr/bin/env python3
"""Offline tests for fcvm-fleet.tf and modules/fcvm-fleet-region (ejc3/aws#606): the fleet's network admits TCP 443 only,
the GPU hosts and the coordinator stay off until their gates and inputs exist, and the coordinator's role reaches GPU hosts
and EC2 tiles from their templates only."""
import re
import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-fleet.tf").read_text()
STAGE_CERT = (ROOT / "fcvm-fleet-stage-cert.tf").read_text()
DNS = (ROOT / "fcvm-fleet-dns.tf").read_text()
ALARMS = (ROOT / "fcvm-fleet-alarms.tf").read_text() if (ROOT / "fcvm-fleet-alarms.tf").exists() else ""
BUDGET = (ROOT / "fcvm-fleet-budget.tf").read_text() if (ROOT / "fcvm-fleet-budget.tf").exists() else ""
MOD = (ROOT / "modules" / "fcvm-fleet-region" / "main.tf").read_text()


def block(header, text=TF):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


class NetworkTests(unittest.TestCase):
    def test_guarded_fleet_admits_only_https_and_slot_tls(self):
        sg = block('resource "aws_security_group" "fleet"', MOD)
        rules = re.findall(r"  ingress \{\n(.*?)\n  \}", sg, re.S)
        self.assertEqual(len(rules), 2)
        self.assertIn("from_port        = 443", sg)
        self.assertIn("to_port          = 443", sg)
        self.assertIn('protocol         = "tcp"', sg)
        self.assertIn('cidr_blocks      = ["0.0.0.0/0"]', rules[0])
        self.assertIn('ipv6_cidr_blocks = ["::/0"]', rules[0])
        self.assertIn("from_port   = var.slot_tls_ports.from", rules[1])
        self.assertIn("to_port     = var.slot_tls_ports.to", rules[1])
        self.assertIn('protocol    = "tcp"', rules[1])
        self.assertIn('cidr_blocks = ["0.0.0.0/0"]', rules[1])
        self.assertNotIn("ipv6_cidr_blocks", rules[1])
        self.assertNotIn("22", sg)
        # The coordinator admits its DNS port (DnsTests) and, with aggregated streaming on, 443 for the EC2 half's
        # aggregator (AggregatedTests); nothing else.
        coord = block('resource "aws_security_group" "fcvm_fleet_coordinator"')
        rules = re.findall(r"  ingress \{\n(.*?)\n  \}", coord, re.S)
        self.assertEqual(len(rules), 2, rules)
        for rule in rules:
            self.assertIn("from_port   = local.fcvm_fleet_dns_port", rule)
            self.assertIn("to_port     = local.fcvm_fleet_dns_port", rule)
        self.assertEqual(sorted(re.findall(r'protocol    = "(\w+)"', coord)), ["tcp", "udp"])
        self.assertEqual(coord.count('dynamic "ingress"'), 1, coord)
        self.assertIn("for_each = local.fcvm_fleet_aggregated ? [1] : []", coord)
        self.assertIn("from_port        = 443", coord)
        self.assertIn("to_port          = 443", coord)

    def test_public_java_join_is_virginia_host_only_and_ipv6_only(self):
        for region in ("east2", "west2"):
            self.assertNotIn("minecraft_java_access", block('module "fcvm_fleet_%s"' % region))
        east = block('module "fcvm_fleet_east1"')
        # Gen14 (reviewed) and the factory's generation fifteen (ejc3/fcvm c5e8fc89, which carries #1844's
        # host limits and Pumpkin 1aa35ecc's login timeout).
        self.assertIn('host_amis   = ["ami-0c3ea14848ddbeb93", "ami-089b83ada9f380d5e"]', east)
        self.assertIn('source_cidr = "::/0"', east)
        guard = block('resource "aws_vpc_security_group_ingress_rule" "minecraft_java"', MOD)
        self.assertRegex(guard, r"cidr_ipv6\s*=\s*var\.minecraft_java_access\.source_cidr")
        for field in ("from_port", "to_port"):
            self.assertRegex(guard, rf"{field}\s*=\s*25565")
        self.assertNotIn("cidr_ipv4", guard)
        host = block('resource "aws_launch_template" "host"', MOD)
        # Every Virginia host gets the group; the boot guard opens 25565 only on a listed image or
        # one that says it carries the join limits.
        self.assertIn('each.key == "x86_64" && var.minecraft_java_access != null', host)
        self.assertIn("aws_security_group.minecraft_java[*].id", host)
        for template in ("tile", "tile_on_demand", "tile_pool"):
            self.assertNotIn("minecraft_java", block('resource "aws_launch_template" "%s"' % template, MOD))
        self.assertNotIn("minecraft_java", block('resource "aws_security_group" "fleet"', MOD))
        self.assertEqual(TF.count("host_minecraft_java_security_group_arns"), 2)
        inline = block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"')
        self.assertNotIn("host_minecraft_java", inline, "Keep the new grant outside the nearly-full inline policy")
        dedicated = block('resource "aws_iam_policy" "fcvm_fleet_coordinator_java"')
        self.assertIn('Sid      = "LaunchVirginiaHostWithTemplateJavaGroup"', dedicated)
        self.assertIn('Action   = "ec2:RunInstances"', dedicated)
        self.assertIn('Resource = module.fcvm_fleet_east1.host_minecraft_java_security_group_arns[count.index]', dedicated)
        self.assertNotIn('"ec2:CreateFleet"', dedicated)
        attachment = block('resource "aws_iam_role_policy_attachment" "fcvm_fleet_coordinator_java"')
        self.assertIn('role       = aws_iam_role.fcvm_fleet_coordinator_task.name', attachment)
        self.assertIn('policy_arn = aws_iam_policy.fcvm_fleet_coordinator_java[count.index].arn', attachment)
        self.assertIn('ArnEquals = { "ec2:LaunchTemplate" = module.fcvm_fleet_east1.host_minecraft_java_template_arn }', dedicated)
        self.assertIn('Bool      = { "ec2:IsLaunchTemplateResource" = "true" }', dedicated)
        self.assertNotIn('IfExists', dedicated)
        source_template = block('output "host_minecraft_java_template_arn"', MOD)
        self.assertIn('aws_launch_template.host["x86_64"].arn', source_template)

    def test_ipv4_opening_requires_all_supplied_images_to_have_the_private_reviewed_guard(self):
        self.assertIn('data "aws_ami" "guarded"', MOD, "Public IPv4 requires AMI guard admission first")
        admitted = block('data "aws_ami" "guarded"', MOD)
        self.assertRegex(admitted, r"for_each\s*=\s*toset\(compact\(concat\(values\(var.gpu_amis\), values\(var.tile_amis\)\)\)\)")
        self.assertRegex(admitted, r'owners\s*=\s*\["self"\]')
        validated = re.search(r'  dynamic "filter" \{\n.*?\n  \}', admitted, re.S)
        self.assertIsNotNone(validated)
        self.assertRegex(validated.group(), r'for_each\s*=\s*var\.ami_release_guarded \? \[true\] : \[\]')
        self.assertEqual(dict(re.findall(r'name\s*=\s*"([^"]+)"\s+values\s*=\s*\[([^\]]+)\]', validated.group())),
                         {"tag:FleetImageValidated": '"true"'})
        filters = dict(re.findall(r'name\s*=\s*"([^"]+)"\s+values\s*=\s*\[([^\]]+)\]',
                                  admitted[:validated.start()] + admitted[validated.end():]))
        self.assertEqual(filters, {"image-id": "each.value", "is-public": '"false"',
                                  "state": '"available"', "tag:GuardHelperSHA256": "var.ip_options_guard_sha256"})
        sg = block('resource "aws_security_group" "fleet"', MOD)
        self.assertRegex(sg, r"depends_on\s*=\s*\[data.aws_ami.guarded\]")
        digest = block('variable "ip_options_guard_sha256"', MOD)
        self.assertNotIn("default", digest)
        for region in ("east2", "west2", "east1"):
            module = block('module "fcvm_fleet_%s"' % region)
            self.assertRegex(module, r'ip_options_guard_sha256\s*=\s*filesha256\("\$\{path.module\}/scripts/cve-2026-98432-mitigation.py"\)')

    def test_every_az_gets_a_public_ipv6_subnet_and_flow_logs(self):
        sub = block('resource "aws_subnet" "public"', MOD)
        self.assertIn("assign_ipv6_address_on_creation                = true", sub)
        self.assertIn("ipv6_cidr_block                                = cidrsubnet(aws_vpc.this.ipv6_cidr_block, 8, each.value)", sub)
        self.assertIn("assign_generated_ipv6_cidr_block = true", MOD)
        self.assertIn('traffic_type             = "ALL"', block('resource "aws_flow_log" "this"', MOD))

    def test_three_regions_three_existing_aliases_no_new_provider(self):
        self.assertNotIn('provider "aws"', TF)
        for name, alias in (("east2", "fcvm_fleet"), ("west2", "fcvm_fleet_west2"), ("east1", "fcvm_fleet_east1")):
            self.assertRegex(block('module "fcvm_fleet_%s"' % name), r"providers += \{ aws = aws\.%s \}" % alias)


class HostTests(unittest.TestCase):
    def test_java_guard_runs_before_agent_on_every_start_and_uses_the_booted_image(self):
        user_data = MOD.split("user_data = base64encode(<<-EOT", 1)[1].split("\n  EOT", 1)[0]
        before_env, after_env = user_data.split("printf 'FLEET_AGENT_ARGS", 1)
        self.assertIn("/etc/systemd/system/fcvm-fleet-agent.service.d/30-minecraft-java-guard.conf", before_env)
        # Closed before the agent starts; opened by its own unit after the agent has installed
        # the join limits and the guard has read them back.
        self.assertIn("ExecStartPre=/usr/local/libexec/fcvm-minecraft-java-guard close\n", before_env)
        self.assertIn("ExecStartPost=/bin/systemctl --no-block restart fcvm-minecraft-java-open.service\n", before_env)
        opener = before_env.split("cat > /etc/systemd/system/fcvm-minecraft-java-open.service <<'MINECRAFT_OPEN'\n", 1)[1].split("MINECRAFT_OPEN", 1)[0]
        for line in ("After=fcvm-fleet-agent.service", "PartOf=fcvm-fleet-agent.service", "Type=oneshot",
                     "EnvironmentFile=/etc/fcvm-fleet/agent.env", "EnvironmentFile=-/etc/fcvm-fleet/windows.env",
                     "ExecStart=/usr/local/libexec/fcvm-minecraft-java-guard open "):
            with self.subTest(line=line):
                self.assertIn(line, opener)
        self.assertIn("systemctl daemon-reload", before_env)
        self.assertIn("systemctl start --no-block fcvm-fleet-agent.service", after_env)
        guard = (ROOT / "modules/fcvm-fleet-region/minecraft-java-guard.sh").read_text()
        image = "ami-0c3ea14848ddbeb93"
        second = "ami-089b83ada9f380d5e"
        approved_list = image + "," + second
        with tempfile.TemporaryDirectory() as temp:
            curl = Path(temp) / "curl"
            curl.write_text(textwrap.dedent(r"""
                #!/bin/bash
                set -eu
                case "${!#}" in
                  */api/token) test "$BOOT_AMI" != no-token; printf fixture-token;;
                  */meta-data/ami-id|*/meta-data/mac|*/network/interfaces/macs/00:01:02:03:04:05/ipv6-prefix)
                    test "$(cat)" = 'X-aws-ec2-metadata-token: fixture-token'
                    case "${!#}" in
                      *ami-id) test "$BOOT_AMI" != no-image; printf "%s" "$BOOT_AMI";;
                      *ipv6-prefix) printf "%s" "$PREFIX";;
                      *mac) printf 00:01:02:03:04:05;;
                    esac;;
                  *) exit 90;;
                esac
            """).lstrip())
            curl.chmod(0o700)
            nft = Path(temp) / "nft"
            nft.write_text('#!/bin/bash\ncat\nexit "$NFT_STATUS"\n')
            nft.chmod(0o700)
            pci = Path(temp) / "pci"
            (pci / "drivers/vfio-pci").mkdir(parents=True)
            for i in range(8):
                name = f"0000:{i:02d}:00.0"
                dev = pci / "devices" / name
                dev.mkdir(parents=True)
                (dev / "vendor").write_text("0x10de\n")
                (dev / "class").write_text("0x030200\n")
                (pci / "drivers/vfio-pci" / name).symlink_to(dev)
            limits_env = Path(temp) / "minecraft.env"
            ip6tables = Path(temp) / "ip6tables"
            ip6tables.write_text(textwrap.dedent(r"""
                #!/bin/bash
                set -eu
                test "$1" = -S
                case "$2" in
                  FCVM-MC-JOIN) test -n "${IP6_JOIN:-}" || exit 1; printf "%s\n" "$IP6_JOIN";;
                  FORWARD) printf "%s\n" "$IP6_FORWARD";;
                  *) exit 2;;
                esac
            """).lstrip())
            ip6tables.chmod(0o700)
            script = (guard.replace("/usr/bin/curl", str(curl)).replace("/usr/sbin/nft", str(nft))
                      .replace("/sys/bus/pci", str(pci)).replace("/etc/fcvm-fleet/minecraft.env", str(limits_env))
                      .replace("/usr/sbin/ip6tables", str(ip6tables)))
            # ip6tables -S as the agent's chain prints on a host (ejc3/fcvm src/fleet/agent/joinguard.rs).
            syn = "-p tcp -m tcp --tcp-flags FIN,SYN,RST,ACK SYN"
            live_join = "\n".join([
                "-N FCVM-MC-JOIN",
                f"-A FCVM-MC-JOIN {syn} -m connlimit --connlimit-above 4 --connlimit-mask 64 --connlimit-saddr "
                "-j REJECT --reject-with tcp-reset",
                f"-A FCVM-MC-JOIN {syn} -m hashlimit --hashlimit-above 12/min --hashlimit-burst 6 --hashlimit-mode srcip "
                "--hashlimit-name fcvm-mc-join --hashlimit-srcmask 64 -j REJECT --reject-with tcp-reset"])
            live_forward = "\n".join(["-P FORWARD ACCEPT",
                                      "-A FORWARD -p tcp -m tcp --dport 25565 -j FCVM-MC-JOIN",
                                      '-A FORWARD -m comment --comment "netavark firewall plugin rules" -j NETAVARK_FORWARD',
                                      "-A FORWARD -i veth0 -j ACCEPT"])
            approved_prefix = "2001:db8:1:2:3::/80"
            agent_args = "--windows 4 --minecraft 4"
            cases = [(image, approved_prefix, agent_args, "8", "0", True),
                     (second, approved_prefix, agent_args, "8", "0", True),
                     ("ami-0bbbbbbbbbbbbbbbb", approved_prefix, agent_args, "8", "0", False),
                     ("no-token", approved_prefix, agent_args, "8", "0", False),
                     ("no-image", approved_prefix, agent_args, "8", "0", False),
                     (image, "2001:db8:1:2::/64", agent_args, "8", "0", False),
                     (image, approved_prefix + "\n" + approved_prefix, agent_args, "8", "0", False),
                     (image, approved_prefix, "--minecraft 4", "8", "0", False),
                     (image, approved_prefix, agent_args, "7", "0", False),
                     (image, approved_prefix, agent_args, "8", "1", True)]
            cases = [case + ("", live_join, live_forward) for case in cases]
            # An image off the list opens only when its build said its servers carry the join limits
            # (ejc3/fcvm#1915: MINECRAFT_JOIN_LIMITS=enforced in /etc/fcvm-fleet/minecraft.env), and
            # still only with the layout checks.
            unlisted = "ami-0ccccccccccccccc1"
            enforced = "MINECRAFT_JOIN_LIMITS=enforced\n"
            cases += [(unlisted, approved_prefix, agent_args, "8", "0", True, enforced, live_join, live_forward),
                      (unlisted, approved_prefix, agent_args, "8", "0", False, "MINECRAFT_JOIN_LIMITS=enforced # x\n",
                       live_join, live_forward),
                      (unlisted, approved_prefix, "--minecraft 4", "8", "0", False, enforced, live_join, live_forward),
                      (unlisted, "2001:db8:1:2::/64", agent_args, "8", "0", False, enforced, live_join, live_forward),
                      ("no-image", approved_prefix, agent_args, "8", "0", False, enforced, live_join, live_forward)]
            # The marker is a precondition, not the proof: the live chain decides, for a listed image
            # as for a marked one.
            unhooked = "\n".join(["-P FORWARD ACCEPT", "-A FORWARD -i veth0 -j ACCEPT"])
            accept_first = "\n".join(["-P FORWARD ACCEPT", "-A FORWARD -i veth0 -j ACCEPT",
                                       "-A FORWARD -p tcp -m tcp --dport 25565 -j FCVM-MC-JOIN"])
            for actual, marker in ((unlisted, enforced), (image, "")):
                cases += [(actual, approved_prefix, agent_args, "8", "0", False, marker, "", live_forward),
                          (actual, approved_prefix, agent_args, "8", "0", False, marker, live_join, unhooked),
                          (actual, approved_prefix, agent_args, "8", "0", False, marker, live_join, accept_first),
                          (actual, approved_prefix, agent_args, "8", "0", False, marker,
                           live_join.replace("--connlimit-above 4 ", "--connlimit-above 40 "), live_forward),
                          (actual, approved_prefix, agent_args, "8", "0", False, marker,
                           live_join.replace("--hashlimit-burst 6 ", "--hashlimit-burst 60 "), live_forward),
                          (actual, approved_prefix, agent_args, "8", "0", False, marker,
                           live_join.replace(" -j REJECT --reject-with tcp-reset", " -j ACCEPT"), live_forward),
                          (actual, approved_prefix, agent_args, "8", "0", False, marker,
                           "\n".join(live_join.splitlines()[:2]), live_forward)]
            # Rules that pass a substring check but bypass or narrow the limits, as Ubuntu noble's
            # ip6tables 1.8.10 (nf_tables) prints them. Each keeps Java closed.
            connlimit, hashlimit = live_join.splitlines()[1:]
            jump = "-A FORWARD -p tcp -m tcp --dport 25565 -j FCVM-MC-JOIN"
            bypasses = {
                "earlier RETURN": (live_join, "\n".join(["-P FORWARD ACCEPT",
                                                          "-A FORWARD -p tcp -m tcp --dport 25565 -j RETURN", jump])),
                "earlier jump elsewhere": (live_join, "\n".join(["-P FORWARD ACCEPT",
                                                                  "-A FORWARD -i br0 -j FCVM-OTHER", jump])),
                "connlimit on marked packets only": (live_join.replace(
                    connlimit, connlimit.replace(" -m connlimit ", " -m mark --mark 0x1 -m connlimit ")), live_forward),
                "connlimit keyed by destination": (live_join.replace(
                    connlimit, connlimit.replace("--connlimit-saddr", "--connlimit-daddr")), live_forward),
                "hashlimit on marked packets only": (live_join.replace(
                    hashlimit, hashlimit.replace(" -m hashlimit ", " -m mark --mark 0x1 -m hashlimit ")), live_forward),
                "hashlimit keyed by destination": (live_join.replace(hashlimit, hashlimit.replace(
                    "--hashlimit-mode srcip", "--hashlimit-mode dstip").replace(
                    "--hashlimit-srcmask 64", "--hashlimit-dstmask 64")), live_forward),
            }
            for label, (join, forward) in bypasses.items():
                for actual, marker in ((unlisted, enforced), (image, "")):
                    cases.append((actual, approved_prefix, agent_args, "8", "0", False, marker, join, forward))
            allow = ("forward ip6 daddr { 2001:db8:1:2:3::e, 2001:db8:1:2:3::f, "
                     "2001:db8:1:2:3::10, 2001:db8:1:2:3::11 } tcp dport 25565 return")
            labels = {rules: label for label, rules in bypasses.items()}
            for actual, prefix, args, gpu_count, status, forward_allowed, limits, join, forward in cases:
                with self.subTest(labels.get((join, forward), ""), actual=actual, prefix=prefix, args=args,
                                  gpu_count=gpu_count, status=status, limits=limits, join=join, forward=forward):
                    limits_env.write_text(limits)
                    result = subprocess.run(
                        ["/bin/bash", "-x", "-c", script, "--", "open", approved_list, gpu_count, "4", "4"],
                        env=dict(os.environ, BOOT_AMI=actual, PREFIX=prefix, NFT_STATUS=status, IP6_JOIN=join,
                                 IP6_FORWARD=forward, FCVM_JAVA_GUARD_WAIT="0",
                                 FLEET_AGENT_ARGS=args, WINDOWS_AGENT_ARGS="--windows-image /fixture/windows"),
                        capture_output=True,
                        text=True, check=False, timeout=5,
                    )
                    self.assertEqual(result.returncode, int(status))
                    self.assertIn("hook input priority -130", result.stdout)
                    self.assertIn("hook forward priority -130", result.stdout)
                    self.assertIn("input tcp dport 25565 drop", result.stdout)
                    self.assertEqual(allow in result.stdout, forward_allowed)
                    self.assertEqual("forward ip6 daddr" in result.stdout, forward_allowed)
                    self.assertIn("forward tcp dport 25565 drop", result.stdout)
                    if forward_allowed:
                        self.assertLess(result.stdout.index(allow), result.stdout.index("forward tcp dport 25565 drop"))
                    self.assertNotIn("fixture-token", result.stderr)
                    self.assertNotIn("flush ruleset", result.stdout)

    def test_java_guard_moves_the_java_jump_above_netavarks_while_java_is_closed(self):
        # On a staging host (2026-11-15) podman's netavark inserts its FORWARD jump at position 1
        # after the agent inserted the Java jump there, when the renderer containers start. The
        # guard then moves the agent's jump back to position 1, with Java closed throughout, and
        # opens only once it reads the jump first. An earlier rule of any other kind still keeps
        # Java closed and is never moved past.
        guard = (ROOT / "modules/fcvm-fleet-region/minecraft-java-guard.sh").read_text()
        image = "ami-0c3ea14848ddbeb93"
        syn = "-p tcp -m tcp --tcp-flags FIN,SYN,RST,ACK SYN"
        join = "\n".join([
            "-N FCVM-MC-JOIN",
            f"-A FCVM-MC-JOIN {syn} -m connlimit --connlimit-above 4 --connlimit-mask 64 --connlimit-saddr "
            "-j REJECT --reject-with tcp-reset",
            f"-A FCVM-MC-JOIN {syn} -m hashlimit --hashlimit-above 12/min --hashlimit-burst 6 --hashlimit-mode srcip "
            "--hashlimit-name fcvm-mc-join --hashlimit-srcmask 64 -j REJECT --reject-with tcp-reset"])
        jump = "-A FORWARD -p tcp -m tcp --dport 25565 -j FCVM-MC-JOIN"
        netavark = '-A FORWARD -m comment --comment "netavark firewall plugin rules" -j NETAVARK_FORWARD'
        cases = {
            # (FORWARD before, opens, FORWARD after)
            "netavark first": ([netavark, jump], True, [jump, netavark]),
            "jump already first": ([jump, netavark], True, [jump, netavark]),
            "Java first before netavark starts": ([jump], False, [jump]),
            "nft close fails": ([netavark, jump], False, [netavark, jump]),
            "netavark and a RETURN first": ([netavark, "-A FORWARD -p tcp -m tcp --dport 25565 -j RETURN", jump],
                                            False, None),
            "a RETURN first": (["-A FORWARD -p tcp -m tcp --dport 25565 -j RETURN", netavark, jump], False, None),
            "netavark with another comment": (
                ['-A FORWARD -m comment --comment "other" -j NETAVARK_FORWARD', jump], False, None),
            "netavark and no jump": ([netavark], False, [netavark]),
            "two jumps": ([jump, netavark, jump], False, None),
        }
        with tempfile.TemporaryDirectory() as temp:
            t = Path(temp)
            log = t / "events"
            (t / "curl").write_text(textwrap.dedent(r"""
                #!/bin/bash
                set -eu
                case "${!#}" in
                  */api/token) printf fixture-token;;
                  */meta-data/ami-id) cat >/dev/null; printf "%s" "$BOOT_AMI";;
                  */meta-data/mac) cat >/dev/null; printf 00:01:02:03:04:05;;
                  */ipv6-prefix) cat >/dev/null; printf 2001:db8:1:2:3::/80;;
                  *) exit 90;;
                esac
            """).lstrip())
            # nft logs whether it was handed an opened or a closed table.
            (t / "nft").write_text(textwrap.dedent(r"""
                #!/bin/bash
                if grep -q 'ip6 daddr'; then
                  echo "nft open" >> "$EVENTS"
                elif [[ "${FAIL_CLOSE:-}" == 1 ]]; then
                  echo "nft close failed" >> "$EVENTS"
                  exit 1
                else
                  echo "nft closed" >> "$EVENTS"
                fi
            """).lstrip())
            # ip6tables keeps FORWARD in a file and supports the two edits the guard may make.
            (t / "ip6tables").write_text(textwrap.dedent(r"""
                #!/usr/bin/env python3
                import os, sys
                args, state = sys.argv[1:], os.environ["FORWARD_FILE"]
                rules = [l for l in open(state).read().splitlines() if l]
                spec = ["-p", "tcp", "--dport", "25565", "-j", "FCVM-MC-JOIN"]
                jump = "-A FORWARD -p tcp -m tcp --dport 25565 -j FCVM-MC-JOIN"
                if args == ["-S", "FCVM-MC-JOIN"]:
                    print(os.environ["IP6_JOIN"]); sys.exit(0)
                if args == ["-S", "FORWARD"]:
                    print("-P FORWARD ACCEPT"); print("\n".join(rules)); sys.exit(0)
                open(os.environ["EVENTS"], "a").write("ip6tables " + " ".join(args) + "\n")
                if args == ["-D", "FORWARD"] + spec and jump in rules:
                    rules.remove(jump)
                elif args == ["-I", "FORWARD", "1"] + spec:
                    rules.insert(0, jump)
                else:
                    sys.exit(1)
                open(state, "w").write("\n".join(rules) + "\n")
            """).lstrip())
            for name in ("curl", "nft", "ip6tables"):
                (t / name).chmod(0o700)
            pci = t / "pci"
            (pci / "drivers/vfio-pci").mkdir(parents=True)
            for i in range(8):
                dev = pci / "devices" / f"0000:{i:02d}:00.0"
                dev.mkdir(parents=True)
                (dev / "vendor").write_text("0x10de\n")
                (dev / "class").write_text("0x030200\n")
                (pci / "drivers/vfio-pci" / dev.name).symlink_to(dev)
            script = (guard.replace("/usr/bin/curl", str(t / "curl")).replace("/usr/sbin/nft", str(t / "nft"))
                      .replace("/sys/bus/pci", str(pci)).replace("/etc/fcvm-fleet/minecraft.env", str(t / "mc.env"))
                      .replace("/usr/sbin/ip6tables", str(t / "ip6tables")))
            (t / "mc.env").write_text("")
            for label, (before, opens, after) in cases.items():
                with self.subTest(label):
                    (t / "forward").write_text("\n".join(before) + "\n")
                    log.write_text("")
                    result = subprocess.run(
                        ["/bin/bash", "-c", script, "--", "open", image, "8", "4", "4"],
                        env=dict(os.environ, BOOT_AMI=image, IP6_JOIN=join, FORWARD_FILE=str(t / "forward"),
                                 EVENTS=str(log), FCVM_JAVA_GUARD_WAIT="0", FLEET_AGENT_ARGS="--windows 4 --minecraft 4",
                                 FAIL_CLOSE="1" if label == "nft close fails" else "0",
                                 WINDOWS_AGENT_ARGS="--windows-image /fixture/windows"),
                        capture_output=True, text=True, check=False, timeout=10)
                    events = log.read_text().splitlines()
                    if label == "nft close fails":
                        self.assertNotEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(events, ["nft close failed"])
                        self.assertEqual((t / "forward").read_text().splitlines(), before)
                        continue
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(events[-1], "nft open" if opens else "nft closed", events)
                    moved = [e for e in events if e.startswith("ip6tables ")]
                    if moved:
                        # The jump moves only while Java is closed: a closed table goes in first and no
                        # opened one before the move is done.
                        first = events.index(moved[0])
                        self.assertIn("nft closed", events[:first], events)
                        self.assertNotIn("nft open", events[:events.index(moved[-1])], events)
                    if after is not None:
                        self.assertEqual((t / "forward").read_text().split("\n")[:-1], after)
                    else:
                        self.assertEqual(moved, [], "an earlier rule other than netavark's is never moved past")

    def test_java_guard_closes_before_the_agent_starts_whatever_the_host_holds(self):
        guard = (ROOT / "modules/fcvm-fleet-region/minecraft-java-guard.sh").read_text()
        with tempfile.TemporaryDirectory() as temp:
            nft = Path(temp) / "nft"
            nft.write_text('#!/bin/bash\ncat\n')
            nft.chmod(0o700)
            result = subprocess.run(["/bin/bash", "-c", guard.replace("/usr/sbin/nft", str(nft)), "--", "close"],
                                    capture_output=True, text=True, check=True, timeout=5)
        self.assertIn("flush table ip6 fcvm_minecraft_java", result.stdout)
        self.assertIn("input tcp dport 25565 drop", result.stdout)
        self.assertIn("forward tcp dport 25565 drop", result.stdout)
        self.assertNotIn("return", result.stdout)

    def test_potato_argument_tracks_the_actual_boot_image_not_the_template_history(self):
        # The publisher reuses user data when it switches image IDs. Exercise
        # the same bootstrap with the candidate, current host, every rollback and failed IMDS.
        oldest = "ami-0620224a0a4dd168e"
        older = "ami-04c3294f21cb88942"
        rollback = "ami-09ac99f69700ec306"
        previous = "ami-05923927f810a2ce9"
        candidate = "ami-0ccccccccccccccc0"   # stands for the publisher's us-east-1 copy of a build-region host
        east = block('module "fcvm_fleet_east1"')
        configured = re.search(r'potato_host_amis\s*=\s*concat\(\[(.*?)\n  \], (.*?)\)\n', east, re.S)
        self.assertIsNotNone(configured)
        self.assertEqual(re.findall(r'local\.fcvm_ami_bootstrap\["host/x86_64/([0-9a-f]+)"\]\.image',
                                    configured[1]), ["95217caa", "a4a0bd3e", "92a26793", "343b2287"])
        # and every copy the publisher has made of a reviewed build-region host
        self.assertEqual(configured[2], "flatten([for copies in data.aws_ami_ids.fcvm_ami_build_region_copies : copies.ids])")
        windows = "ami-0dddddddddddddddd"   # cad0ddad: the first host with the /still route removed
        legacy_windows = ("ami-0bbbbbbbbbbbbbbb", "ami-0eeeeeeeeeeeeeee")  # 853eb034 and 0e910245
        self.assertEqual(re.search(r'windows_host_amis\s*=\s*(.+)', east)[1],
                         'concat(data.aws_ami_ids.fcvm_ami_build_region_copies["host/x86_64/cad0ddad"].ids, '
                         'data.aws_ami_ids.fcvm_ami_build_region_copies["host/x86_64/c12ad070"].ids, '
                         '["ami-089b83ada9f380d5e"])')
        # Legacy copies still carry Minecraft and potato, but the widened public ports
        # must never expose their unauthenticated Windows /still endpoint on rollback.
        self.assertNotIn('"--minecraft', TF)
        self.assertRegex(east, r'minecraft_host_amis\s*=\s*concat\(data\.aws_ami_ids\.fcvm_ami_build_region_copies'
                               r'\["host/x86_64/853eb034"\]\.ids, data\.aws_ami_ids\.fcvm_ami_build_region_copies'
                               r'\["host/x86_64/0e910245"\]\.ids, data\.aws_ami_ids\.fcvm_ami_build_region_copies'
                               r'\["host/x86_64/cad0ddad"\]\.ids\)\n')
        guard = MOD.split('%{if length(var.potato_host_amis) > 0~}\n', 1)[1].split("    unset token boot_ami\n", 1)[0]
        self.assertEqual(guard.count("--minecraft ${var.minecraft_count}"), 1)
        # the nested Windows case, rendered as Terraform does when the list is not empty
        windows_if = "      %{if length(var.windows_host_amis) > 0~}\n"
        minecraft_if = "      %{if length(var.minecraft_host_amis) > 0~}\n"
        self.assertEqual(guard.count(windows_if), 1)
        self.assertEqual(guard.count(minecraft_if), 1)
        guard = guard.replace(windows_if, "").replace(minecraft_if, "").replace("      %{endif~}\n", "")
        self.assertNotIn("%{", guard)
        with tempfile.TemporaryDirectory() as temp:
            curl = Path(temp) / "curl"
            curl.write_text(
                "#!/bin/bash\n"
                "set -eu\n"
                'case "${!#}" in\n'
                "  */api/token) test \"$BOOT_AMI\" != no-token; printf fixture-token;;\n"
                "  */meta-data/ami-id)\n"
                "    test \"$(cat)\" = 'X-aws-ec2-metadata-token: fixture-token'\n"
                '    test "$BOOT_AMI" != no-ami; printf "%s" "$BOOT_AMI";;\n'
                "  *) exit 90;;\n"
                "esac\n"
            )
            curl.chmod(0o700)
            boot = textwrap.dedent(guard).replace('${join("|", sort(tolist(var.potato_host_amis)))}',
                                                   "|".join(sorted([oldest, older, rollback, previous, candidate, windows, *legacy_windows])))
            boot = boot.replace('${join("|", sort(tolist(var.windows_host_amis)))}', windows)
            boot = boot.replace('${join("|", sort(tolist(var.minecraft_host_amis)))}',
                                "|".join(sorted([windows, *legacy_windows])))
            boot = boot.replace("${var.windows_count}", "4").replace("${var.minecraft_count}", "4")
            boot = boot.replace("/usr/bin/curl", str(curl))
            for actual, expected in (
                (oldest, "--phones 2 --potato-players 96"),
                (older, "--phones 2 --potato-players 96"),
                (rollback, "--phones 2 --potato-players 96"),
                (previous, "--phones 2 --potato-players 96"),
                (candidate, "--phones 2 --potato-players 96"),
                (windows, "--phones 2 --potato-players 96 --windows 4 --minecraft 4"),
                *((actual, "--phones 2 --potato-players 96 --minecraft 4") for actual in legacy_windows),
                ("ami-051db4be56ae7f6b0", "--phones 2"),
                ("no-token", "--phones 2"),
                ("no-ami", "--phones 2"),
            ):
                with self.subTest(actual=actual):
                    result = subprocess.run(
                        ["/bin/bash", "-eux", "-c", "agent_args='--phones 2'\n" + boot + '\nprintf "%s" "$agent_args"'],
                        env=dict(os.environ, BOOT_AMI=actual), capture_output=True, text=True, check=True, timeout=5,
                    )
                    self.assertEqual(result.stdout, expected)
                    self.assertNotIn("fixture-token", result.stderr)

    def test_factory_host_needs_both_local_image_and_agent_support_for_each_extra_mode(self):
        # Feed the actual fallback shell fake read-only probes, using a non-default
        # Minecraft count to verify it still comes from the port allocation's input.
        fallback = MOD.split('    unset token boot_ami\n    %{endif~}\n', 1)[1].split("    printf 'FLEET_AGENT_ARGS", 1)[0]
        self.assertIn('    %{~if var.ami_release_guarded~}\n', fallback)
        fallback = fallback.replace('    %{~if var.ami_release_guarded~}\n', '').replace('    %{endif~}\n', '')
        with tempfile.TemporaryDirectory() as temp:
            fixtures = Path(temp)
            windows_env = fixtures / 'windows.env'
            windows_env.write_text('WINDOWS_AGENT_ARGS=--windows-image /legacy-image\n')
            fcvm = fixtures / 'fcvm'
            fcvm.write_text('''#!/bin/bash
set -eu
test "$*" = 'fleet agent --help'
test "$HELP" != fails || exit 1
for option in $HELP; do printf '      %s <VALUE>  description\\n' "$option"; done
''')
            podman = fixtures / 'podman'
            podman.write_text('''#!/bin/bash
set -eu
test "$1 $2" = 'image exists'
case " $IMAGES " in *" $3 "*) exit 0;; esac
exit 1
''')
            for probe in (fcvm, podman):
                probe.chmod(0o700)
            shell = (textwrap.dedent(fallback).replace('/usr/local/bin/fcvm', str(fcvm))
                     .replace('/usr/bin/podman', str(podman))
                     .replace('/etc/fcvm-fleet/windows.env', str(windows_env))
                     .replace('${var.minecraft_count}', '3')
                     .replace('${var.windows_count}', '2'))
            potato_image = 'localhost/fcvm-potato:fleet'
            minecraft_image = 'localhost/fcvm-minecraft:fleet'
            both_images = f'{potato_image} {minecraft_image}'
            both_flags = '--potato-players --minecraft --windows'
            legacy = 'WINDOWS_AGENT_ARGS=--windows-image /legacy-image\n'
            # The image's build writes this line only when its bridge refuses /still on the public
            # ports (ejc3/fcvm#1839, fleet/ami/host/build.sh).
            refusing = legacy + 'WINDOWS_BRIDGE_STILL=refused\n'
            for name, before, images, help_text, suffix, windows in (
                ('factory host whose bridge refuses /still', '', both_images, both_flags,
                 ' --potato-players 96 --minecraft 3 --windows 2', refusing),
                ('refusing bridge, old agent without --windows', '', both_images, '--potato-players --minecraft',
                 ' --potato-players 96 --minecraft 3', refusing),
                ('refusing bridge and no mode images', '', '', both_flags, ' --windows 2', refusing),
                ('a marker with anything else on its line is not the marker', '', '', both_flags, '',
                 legacy + 'WINDOWS_BRIDGE_STILL=refused # comment\n'),
                ('reviewed Windows image is not given --windows twice', ' --windows 4', '', both_flags, '', refusing),
                ('factory host with both', '', both_images, both_flags, ' --potato-players 96 --minecraft 3', legacy),
                ('only Minecraft image', '', minecraft_image, both_flags, ' --minecraft 3', legacy),
                ('only potato flag', '', both_images, '--potato-players --minecraft-image --windows', ' --potato-players 96', legacy),
                ('neither image; legacy Windows config present', '', '', both_flags, '', legacy),
                ('old agent with both images', '', both_images, '--potato-image --minecraft-image --windows', '', legacy),
                ('help probe failure', '', both_images, 'fails', '', legacy),
                ('reviewed image keeps existing arguments once', ' --windows 4 --potato-players 96 --minecraft=3', both_images, both_flags, '', legacy),
            ):
                with self.subTest(name=name):
                    windows_env.write_text(windows)
                    result = subprocess.run(
                        ['/bin/bash', '-eu', '-c', f"agent_args='--phones 2{before}'\n" + shell + '\nprintf "%s" "$agent_args"'],
                        env=dict(os.environ, IMAGES=images, HELP=help_text), capture_output=True, text=True, check=True, timeout=5,
                    )
                    self.assertEqual(result.stdout, f'--phones 2{before}{suffix}')

    def test_hosts_ask_for_a_prefix_only_and_imdsv2_hop_1(self):
        lt = block('resource "aws_launch_template" "host"', MOD)
        self.assertIn("ipv6_prefix_count           = 1", lt)
        self.assertNotIn("ipv6_address_count", lt)  # EC2 refuses a prefix count and an address count together
        self.assertIn('http_tokens                 = "required"', lt)
        self.assertIn("http_put_response_hop_limit = 1", lt)

    def test_the_host_root_volume_is_filled_at_the_highest_initialization_rate(self):
        # A lazily loaded root volume put 32.3 s between kernel start and the store unit on a host's first boot, against
        # 11.1 s on a volume already read (ejc3/fcvm#1257); EBS takes 100 to 300 MiB/s.
        lt = block('resource "aws_launch_template" "host"', MOD)
        bdm = re.search(r"block_device_mappings \{\n(.*?)\n  \}\n", lt, re.S)
        self.assertIsNotNone(bdm, lt)
        self.assertIn('device_name = "/dev/sda1"', bdm.group(1))
        self.assertIn("volume_initialization_rate = 300", bdm.group(1))

    def test_the_coordinator_launches_the_hosts_and_no_group_does(self):
        # Auto Scaling took 6 to 14 s to start a launch after a raise (ejc3/fcvm#1257); the coordinator launches the hosts
        # itself from the templates, which it gets only while the gate is on.
        self.assertNotIn("aws_autoscaling_group", MOD)
        self.assertNotIn("aws_autoscaling_group", TF)
        self.assertIn("hosts_on = var.hosts_enabled && length(local.host_archs) > 0", MOD)
        names = block('output "host_template_names"', MOD)
        self.assertIn("value = local.hosts_on ? [for a in local.host_archs : aws_launch_template.host[a].name] : []", names)
        self.assertIn("fcvm_fleet_hosts_enabled       = true", TF)

    def test_hosts_and_tiles_have_amis_only_where_there_is_g_spot_quota(self):
        # One g4dn.metal is the main cost and us-east-1 holds the account's only G spot quota: an AMI elsewhere would start a
        # second always-on host (or a group that can never launch).
        for var in ("fcvm_fleet_host_amis", "fcvm_fleet_tile_amis"):
            default = re.search(r"default = (\{.*\})\n", block('variable "%s"' % var)).group(1)
            self.assertRegex(default, r'^\{ "us-east-1" = \{ x86_64 = "ami-[0-9a-f]{17}" \} \}$')

    def test_only_architectures_with_an_ami_get_a_template(self):
        self.assertIn('host_archs = [for a in sort(keys(local.host_types)) : a if try(var.gpu_amis[a], "") != ""]', MOD)
        self.assertIn('host_types = { x86_64 = "g4dn.metal", arm64 = "g5g.metal" }', MOD)


class OnDemandFallbackTests(unittest.TestCase):
    """Spot first, on demand in the same pass when spot is refused, at most 2 on-demand hosts across a coordinator’s regions (owner,
    2026-11-18: two awake GPU hosts for spot cover): the coordinator's cap, handed to it in its environment."""

    def test_each_coordinator_caps_its_on_demand_hosts_at_two(self):
        self.assertRegex(TF, r"(?m)^  fcvm_fleet_on_demand_max\s+= 2$")
        header = TF.split("variable ", 1)[0]
        self.assertIn("so 2 is $15.65 an hour", header)
        # The quota is stated by reference to the one place that sets it, not as a second number that can drift.
        self.assertIn("local.fcvm_fleet_on_demand_g_vcpus (fcvm-fleet-account.tf)", header)
        self.assertNotRegex(header, r"\b(384|768) vCPU")
        stage = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        prod_config = (ROOT / "fcvm-fleet-prod.tf").read_text()
        prod = block('resource "aws_ecs_task_definition" "fcvm_fleet_prod_coordinator"', prod_config)
        cap = '{ name = "FLEET_HOST_ON_DEMAND_MAX", value = tostring(local.fcvm_fleet_on_demand_max) }'
        for coordinator in (stage, prod):
            self.assertIn(cap, coordinator)
        self.assertNotIn("host_on_demand_max", MOD)

    def test_hosts_launch_only_where_a_host_type_is_offered(self):
        self.assertIn("value = local.host_subnets", block('output "host_subnet_ids"', MOD))
        offerings = block('data "aws_ec2_instance_type_offerings" "host"', MOD)
        self.assertIn('location_type = "availability-zone"', offerings)
        self.assertIn("values = [local.host_types[each.key]]", offerings)
        subnets = TF.split("fcvm_fleet_host_subnets = flatten(", 1)[1].split("\n\n", 1)[0]
        self.assertIn('[for s in m.host_subnet_ids : "${r}:${s}"]', subnets)
        self.assertIn("if length(m.host_template_names) > 0", subnets)

    def test_the_host_templates_reach_the_coordinator_in_its_environment(self):
        # An image from before ejc3/fcvm#1257 refuses an unknown argument and does not start, and the image after it has no
        # --scale-group: the templates go in the environment, which the older image ignores, so either image starts.
        td = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertIn('{ name = "FLEET_HOST_TEMPLATES", value = join(",", local.fcvm_fleet_host_templates) }', td)
        self.assertIn('{ name = "FLEET_HOST_SUBNETS", value = join(",", local.fcvm_fleet_host_subnets) }', td)
        self.assertIn('[for t in m.host_template_names : "${r}:${t}"]', TF)
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('["--sleep-when-idle"]', cmd)
        for gone in ("--scale-group", "--on-demand-group", "--spot-wait", "--host-template"):
            self.assertNotIn(gone, cmd)

    def test_accepted_on_demand_launches_keep_the_existing_notification_destination(self):
        # Preserve SNS addresses so the confirmed subscriber is not deleted/recreated with the Auto Scaling groups.
        topic = block('resource "aws_sns_topic" "alerts"', MOD)
        subscriber = block('resource "aws_sns_topic_subscription" "alerts_email"', MOD)
        self.assertIn('name  = "fcvm-fleet-alerts"', topic)
        self.assertIn('topic_arn = aws_sns_topic.alerts[0].arn', subscriber)
        self.assertIn('endpoint  = var.alert_email', subscriber)
        metric = block('resource "aws_cloudwatch_log_metric_filter" "fcvm_fleet_on_demand_launches"')
        self.assertIn('pattern        = "\\\"wake: host launch accepted\\\" \\\"on-demand\\\""', metric)
        self.assertNotIn('spot launch refused', metric)
        alarm = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_on_demand_launches"')
        self.assertIn('statistic           = "Sum"', alarm)
        self.assertIn('threshold           = 1', alarm)
        self.assertIn('alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]', alarm)
        self.assertIn('ALARM_ARNS = jsonencode(concat([local.fcvm_fleet_on_demand_alarm_arn], '
                      'values(local.fcvm_fleet_health_alarm_arns), values(local.fcvm_fleet_prod_alarm_arns)))', TF)
        for region in ('east2', 'west2', 'east1'):
            self.assertIn('module.fcvm_fleet_%s.alert_topic_arns' % region, TF)
        # A launch-event counter returning to zero says nothing about whether the host is still charging.
        self.assertNotIn('ok_actions', alarm)
        permission = block('resource "aws_lambda_permission" "fcvm_fleet_on_demand_notify"')
        self.assertIn('principal      = "lambda.alarms.cloudwatch.amazonaws.com"', permission)
        self.assertIn('source_arn     = local.fcvm_fleet_on_demand_alarm_arn', permission)
        self.assertIn('source_account = local.fcvm_fleet_account_id', permission)
        policy = block('resource "aws_iam_role_policy" "fcvm_fleet_on_demand_notify"')
        self.assertIn('Action   = "sns:Publish"', policy)
        self.assertIn('Resource = local.fcvm_fleet_alert_topic_arns', policy)
        self.assertNotIn('Resource = "*"', policy)


class OnDemandNotificationTests(unittest.TestCase):
    ALARM = 'arn:aws:cloudwatch:us-east-2:123456789012:alarm:fcvm-fleet-on-demand-host-launches'
    HEALTH = 'arn:aws:cloudwatch:us-east-2:123456789012:alarm:fcvm-fleet-coordinator-not-running'
    TOPICS = ['arn:aws:sns:us-east-1:123456789012:fcvm-fleet-alerts',
              'arn:aws:sns:us-west-2:123456789012:fcvm-fleet-alerts']

    def setUp(self):
        self.sdk = mock.Mock()
        spec = importlib.util.spec_from_file_location('fleet_notify', ROOT / 'scripts/fcvm-fleet-on-demand-notify.py')
        self.notify = importlib.util.module_from_spec(spec)
        with mock.patch.dict('sys.modules', {'boto3': self.sdk}):
            spec.loader.exec_module(self.notify)
        env = mock.patch.dict('os.environ', {'ALARM_ARNS': json.dumps([self.ALARM, self.HEALTH]),
                                             'TOPIC_ARNS': json.dumps(self.TOPICS)})
        env.start()
        self.addCleanup(env.stop)

    def event(self, state='ALARM', alarm=None, source='aws.cloudwatch', description='This counts launch events, not running hosts.'):
        arn = alarm or self.ALARM
        return {'source': source, 'alarmArn': arn,
                'alarmData': {'alarmName': arn.rsplit(':', 1)[-1],
                              'state': {'value': state, 'reason': 'threshold crossed'},
                              'configuration': {'description': description}}}

    def test_accepted_launch_alarm_uses_each_existing_topics_regional_endpoint(self):
        self.assertEqual(self.notify.handler(self.event(), None), {'notified': 2})
        self.assertEqual(self.sdk.client.call_args_list,
                         [mock.call('sns', region_name='us-east-1'), mock.call('sns', region_name='us-west-2')])
        calls = self.sdk.client.return_value.publish.call_args_list
        self.assertEqual([c.kwargs['TopicArn'] for c in calls], self.TOPICS)
        self.assertTrue(all('not running hosts' in c.kwargs['Message'] for c in calls))
        self.assertEqual(calls[0].kwargs['Subject'], 'fcvm fleet: fcvm-fleet-on-demand-host-launches is ALARM')

    def test_a_health_alarm_and_its_recovery_are_forwarded_with_their_description(self):
        for state in ('ALARM', 'OK'):
            self.sdk.reset_mock()
            self.assertEqual(self.notify.handler(self.event(state=state, alarm=self.HEALTH, description='no task'), None),
                             {'notified': 2})
            call = self.sdk.client.return_value.publish.call_args_list[0]
            self.assertEqual(call.kwargs['Subject'], f'fcvm fleet: fcvm-fleet-coordinator-not-running is {state}')
            self.assertTrue(call.kwargs['Message'].startswith('no task\n\nState: ' + state))

    def test_subjects_stay_within_sns_limits(self):
        long = 'arn:aws:cloudwatch:us-east-2:123456789012:alarm:' + 'x' * 200
        with mock.patch.dict('os.environ', {'ALARM_ARNS': json.dumps([long])}):
            self.notify.handler(self.event(alarm=long), None)
        subject = self.sdk.client.return_value.publish.call_args_list[0].kwargs['Subject']
        self.assertLess(len(subject), 100)
        self.assertNotIn('\n', subject)

    def test_insufficient_data_other_alarms_and_other_event_sources_do_not_send(self):
        for event in (self.event(state='INSUFFICIENT_DATA'), self.event(alarm='unrelated'),
                      self.event(source='unrelated'), {}):
            self.assertEqual(self.notify.handler(event, None), {'notified': 0})
        self.sdk.client.assert_not_called()

    def test_publish_failures_raise_for_lambda_asynchronous_retry(self):
        self.sdk.client.return_value.publish.side_effect = RuntimeError('publish unavailable')
        with self.assertRaisesRegex(RuntimeError, 'publish unavailable'):
            self.notify.handler(self.event(), None)


class HealthAlarmTests(unittest.TestCase):
    """fcvm-fleet-alarms.tf: the coordinator's outages page through the fleet's existing alert path, and the log patterns
    are the strings ejc3/fcvm and the pinned cloudflared actually write."""

    def test_every_health_alarm_exists_only_while_the_coordinator_runs_and_reaches_the_forwarder(self):
        self.assertIn('fcvm_fleet_health_alarm_names = local.fcvm_fleet_coordinator_running ? concat(', ALARMS)
        for name in ("fcvm-fleet-coordinator-not-running", "fcvm-fleet-dns-no-healthy-target", "fcvm-fleet-state-not-written",
                     "fcvm-fleet-coordinator-degraded", "fcvm-fleet-certificate-renewal-failed", "fcvm-fleet-tunnel-down"):
            self.assertIn(f'"{name}"', ALARMS)
        alarms = re.findall(r'resource "aws_cloudwatch_metric_alarm" "[^"]+" \{.*?\n\}\n', ALARMS, re.S)
        self.assertEqual(len(alarms), 3)
        for alarm in alarms:
            self.assertRegex(alarm, r"(count|for_each)\s+= local\.fcvm_fleet_coordinator_running \?")
            self.assertIn("alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]", alarm)
            self.assertIn("depends_on          = [aws_lambda_permission.fcvm_fleet_health_alarms]", alarm)
        permission = block('resource "aws_lambda_permission" "fcvm_fleet_health_alarms"', ALARMS)
        self.assertIn("for_each       = local.fcvm_fleet_health_alarm_arns", permission)
        self.assertIn('principal      = "lambda.alarms.cloudwatch.amazonaws.com"', permission)
        self.assertIn("source_arn     = each.value", permission)

    def test_no_running_task_and_no_healthy_dns_target_breach_on_missing_data(self):
        ecs = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_coordinator_not_running"', ALARMS)
        self.assertIn('namespace           = "AWS/ECS"', ecs)
        self.assertIn('ServiceName = "fcvm-fleet-coordinator"', ecs)
        service = block('resource "aws_ecs_service" "fcvm_fleet_coordinator"')
        self.assertRegex(service, r'(?m)^  name\s+= "fcvm-fleet-coordinator"$')
        self.assertIn('statistic           = "SampleCount"', ecs)
        dns = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_dns_no_healthy_target"', ALARMS)
        self.assertIn('metric_name         = "HealthyHostCount"', dns)
        self.assertIn("TargetGroup  = aws_lb_target_group.fcvm_fleet_dns.arn_suffix", dns)
        for alarm in (ecs, dns):
            self.assertIn('comparison_operator = "LessThanThreshold"', alarm)
            self.assertIn("threshold           = 1", alarm)
            self.assertIn('treat_missing_data  = "breaching"', alarm)

    def test_log_patterns_are_lines_the_coordinator_writes(self):
        """The coordinator filters match log calls from the staging image. CI verifies the excerpts at their source."""
        phrases = {
            "fcvm-fleet-state-not-written": ["state not written", "the last state was not stored"],
            "fcvm-fleet-coordinator-degraded": ["listing the GPU hosts failed", "terminating GPU hosts failed",
                                                "launching GPU hosts failed", "EC2 refused a GPU host launch",
                                                "asking a lost GPU host launch again failed", "text check failed"],
            "fcvm-fleet-certificate-renewal-failed": ["certificate renewal failed", "the renewed certificate was not stored",
                                                      "the certificate secret could not be read"],
            "fcvm-fleet-tunnel-down": ["Serve tunnel error", "Unable to establish connection", "Retrying connection in up to"],
        }
        for name, want in phrases.items():
            entry = ALARMS.split(f'"{name}" = {{', 1)[1].split("\n    }", 1)[0]
            pattern = re.search(r'pattern\s+= "(.*)"\n', entry)[1]
            self.assertEqual(re.findall(r'\?\\"([^"\\]+)\\"', pattern), want, name)
        # A spot capacity refusal is routine and is not a degraded state.
        self.assertFalse([l for l in ALARMS.splitlines() if "pattern" in l and "spot launch refused" in l])
        revision, source = (ROOT / "scripts/fixtures/fcvm-coordinator-log-lines.txt").read_text().split("\n", 1)
        self.assertRegex(revision, r"^[0-9a-f]{40}$")
        for name, want in phrases.items():
            if name == "fcvm-fleet-tunnel-down":
                continue
            for phrase in want:
                self.assertIn('"' + phrase, source, f"{name}: {phrase!r} is missing from ejc3/fcvm at {revision}")


class SpendBackstopTests(unittest.TestCase):
    """fcvm-fleet-budget.tf: $750 a day of each stack's host and tile spend (owner, 2026-11-18) stops that stack's new
    launches."""

    def test_the_daily_budget_is_750_on_each_stacks_tagged_instances_only(self):
        self.assertIn("fcvm_fleet_daily_budget_usd = 750", BUDGET)
        self.assertIn('fcvm_fleet_budget_purposes  = ["fcvm-fleet-host", "fcvm-fleet-tile"]', BUDGET)
        legacy = block('resource "aws_budgets_budget" "fcvm_fleet_daily"', BUDGET)
        stage = block('resource "aws_budgets_budget" "fcvm_fleet_stage_daily"', BUDGET)
        prod = block('resource "aws_budgets_budget" "fcvm_fleet_prod_daily"', BUDGET)
        self.assertIn('name         = "fcvm-fleet-daily"', legacy)
        self.assertIn('name   = "TagKeyValue"', legacy)
        self.assertNotRegex(legacy, r"(?m)^  (?:filter_expression|metrics)\b")
        self.assertIn('name         = "fcvm-fleet-stage-daily"', stage)
        self.assertIn("var.fcvm_fleet_prod_enabled ? [] : [80, 100]", legacy)
        self.assertIn("var.fcvm_fleet_prod_enabled ? [80, 100] : []", stage)
        self.assertIn("fcvm_fleet_stage_budget = var.fcvm_fleet_prod_enabled ? aws_budgets_budget.fcvm_fleet_stage_daily : aws_budgets_budget.fcvm_fleet_daily", BUDGET)
        self.assertEqual(re.findall(r"threshold\s+= (\d+)\n", prod), ["80", "100"])
        self.assertEqual(prod.count("subscriber_sns_topic_arns = local.fcvm_fleet_alert_topic_arns"), 2)
        for budget in (legacy, stage):
            self.assertIn("threshold                 = notification.value", budget)
        self.assertIn('name         = "${local.fcvm_fleet_prod_stack.prefix}-daily"', prod)
        self.assertIn("count        = local.fcvm_fleet_prod\n", prod)
        for budget in (stage, prod):
            self.assertIn("provider     = aws.fcvm_fleet", budget)
            self.assertIn('time_unit    = "DAILY"', budget)
            self.assertIn("limit_amount = tostring(local.fcvm_fleet_daily_budget_usd)", budget)
            self.assertNotIn("cost_filter", budget)
            self.assertRegex(budget, r'\n  metrics\s+= \["UnblendedCost"\]\n')
            purpose, fleet = re.findall(r"tags \{\n\s+key\s+= \"(\w+)\"\n\s+values\s+= (\S+)\n\s+match_options = \[\"EQUALS\"\]", budget)
            self.assertEqual(purpose, ("Purpose", "local.fcvm_fleet_budget_purposes"))
            self.assertEqual(fleet[0], "Fleet")
            self.assertEqual(budget.count("    and {\n"), 2)
            self.assertIn("subscriber_sns_topic_arns = local.fcvm_fleet_alert_topic_arns", budget)
            self.assertIn("depends_on = [aws_ce_cost_allocation_tag.purpose, aws_ce_cost_allocation_tag.fleet]", budget)
        # Prod counts Fleet=prod; staging counts what carries no other stack's Fleet tag, untagged instances included.
        fleet_values = lambda b: re.findall(r'key\s+= "Fleet"\n\s+values\s+= (\S+)\n', b)
        self.assertEqual(fleet_values(prod), ["[local.fcvm_fleet_prod_stack.fleet]"])
        self.assertEqual(fleet_values(stage), ["local.fcvm_fleet_budget_other_fleets"])
        self.assertIn("      not {\n        tags {\n          key           = \"Fleet\"\n          values        = local.fcvm_fleet_budget_other_fleets\n", stage)
        self.assertNotIn("not {", prod)
        self.assertIn('fcvm_fleet_budget_other_fleets = [for k, s in local.fcvm_fleet_stacks : s.fleet if k != "stage"]', BUDGET)
        # The gpubox test hosts and AMI builders share the account and must not stop the wall.
        self.assertNotIn("fcvm-fleet-gpubox\"]", BUDGET)
        for key in ("purpose", "fleet"):
            tag = block('resource "aws_ce_cost_allocation_tag" "%s"' % key, BUDGET)
            self.assertNotRegex(tag, r"provider\s+=")  # only the management account activates cost allocation tags
            self.assertIn("prevent_destroy = true", tag)
        # Each host and tile launch template tags its instance with the Purpose the budget filters on.
        self.assertEqual(len(re.findall(r'Purpose = "fcvm-fleet-host"', MOD)), 1)
        self.assertGreaterEqual(len(re.findall(r'Purpose = "fcvm-fleet-tile"', MOD)), 3)

    def test_the_backstop_denies_only_new_launches_to_the_coordinator(self):
        cap = block('resource "aws_iam_policy" "fcvm_fleet_spend_cap"', BUDGET)
        self.assertIn('Effect   = "Deny"', cap)
        self.assertIn('Action   = ["ec2:RunInstances", "ec2:CreateFleet", "ec2:StartInstances"]', cap)
        for kept in ("TerminateInstances", "StopInstances", "Describe"):
            self.assertNotIn(kept, cap)
        self.assertNotIn('resource "aws_budgets_budget_action"', BUDGET)  # unsupported for DAILY
        grant = block('resource "aws_iam_role_policy" "fcvm_fleet_budget_action"', BUDGET)
        self.assertIn('Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]', grant)
        self.assertIn("Resource  = [for s in local.fcvm_fleet_spend_cap_stacks : s.role_arn]", grant)
        self.assertIn('ArnEquals = { "iam:PolicyARN" = aws_iam_policy.fcvm_fleet_spend_cap.arn }', grant)
        self.assertIn('Action   = "budgets:ViewBudget"', grant)
        self.assertIn("Resource = [for s in local.fcvm_fleet_spend_cap_stacks : s.budget_id]", grant)
        self.assertNotIn('Resource = "*"', grant)
        # Each stack: its own budget, decision record and coordinator role; prod's only while its switch is on.
        stacks = BUDGET.split("fcvm_fleet_spend_cap_stacks = concat(", 1)[1].split("\n  )\n", 1)[0]
        for line in ("budget    = local.fcvm_fleet_stage_budget.name",
                     "state_key = aws_s3_object.fcvm_fleet_spend_cap_state.key",
                     "role_arn  = aws_iam_role.fcvm_fleet_coordinator_task.arn",
                     "[for i, b in aws_budgets_budget.fcvm_fleet_prod_daily : {",
                     "state_key = aws_s3_object.fcvm_fleet_prod_spend_cap_state[i].key",
                     "role_arn  = aws_iam_role.fcvm_fleet_prod_coordinator_task[i].arn"):
            self.assertIn(line, stacks)
        self.assertIn('key          = "spend-cap/${local.fcvm_fleet_prod_stack.fleet}-daily.json"',
                      block('resource "aws_s3_object" "fcvm_fleet_prod_spend_cap_state"', BUDGET))
        self.assertIn('key          = "spend-cap/daily.json"', block('resource "aws_s3_object" "fcvm_fleet_spend_cap_state"', BUDGET))
        self.assertIn("STACKS = jsonencode([for s in local.fcvm_fleet_spend_cap_stacks : { budget = s.budget, state_key = s.state_key, role = s.role }])",
                      block('resource "aws_lambda_function" "fcvm_fleet_budget_action"', BUDGET))
        # The deny policy keeps its name and description (a new description would replace it).
        self.assertIn('description = "Attached to fcvm-fleet-coordinator by the daily billing reconciler: no new GPU hosts or EC2 tiles"', cap)
        trust = block('resource "aws_iam_role" "fcvm_fleet_budget_action"', BUDGET)
        self.assertIn('Principal = { Service = "lambda.amazonaws.com" }', trust)
        worker = block('resource "aws_lambda_function" "fcvm_fleet_budget_action"', BUDGET)
        # The previous Lambda revision may still run after AWS updates the environment.
        for key, resource in {
            "BUDGET_NAME": "aws_budgets_budget.fcvm_fleet_daily.name",
            "STATE_KEY": "aws_s3_object.fcvm_fleet_spend_cap_state.key",
            "ROLE_NAME": "aws_iam_role.fcvm_fleet_coordinator_task.name",
        }.items():
            self.assertRegex(worker, rf"(?m)^      {key} += {re.escape(resource)}$")
        self.assertIn("reserved_concurrent_executions = 1", worker)
        self.assertIn('expire    = "cron(0 0 * * ? *)"', BUDGET)
        self.assertIn('reconcile = "cron(2/5 * * * ? *)"', BUDGET)
        self.assertIn('"fcvm-fleet-spend-cap-unhealthy"', ALARMS)
        self.assertIn('"fcvm-fleet-spend-cap-unhealthy"', BUDGET)

    def test_budgets_may_publish_to_the_fleet_alert_topic(self):
        policy = block('resource "aws_sns_topic_policy" "alerts"', MOD)
        self.assertIn('Principal = { Service = "budgets.amazonaws.com" }', policy)
        self.assertIn('"aws:SourceAccount" = data.aws_caller_identity.current.account_id', policy)
        # Replacing the default policy must keep the account's own grant (the forwarder Lambda publishes as the account).
        self.assertIn('Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }', policy)
        self.assertIn("depends_on = [aws_sns_topic_policy.alerts]", block('output "alert_topic_arns"', MOD))


class CoordinatorTests(unittest.TestCase):
    def test_the_service_runs_the_tagged_image_from_the_fleet_repository(self):
        self.assertIn("fcvm_fleet_coordinator_enabled = true", TF)
        self.assertRegex(TF, r'fcvm_fleet_coordinator_tag = "[0-9a-f]{8,40}"')
        self.assertIn('track_latest = true', TF)
        self.assertNotIn('ignore_changes', block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"'))
        self.assertIn('fcvm_fleet_coordinator_running = local.fcvm_fleet_coordinator_enabled && local.fcvm_fleet_coordinator_tag != ""', TF)
        self.assertIn("image     = local.fcvm_fleet_coordinator_running ? nonsensitive(data.aws_ssm_parameter.fcvm_coordinator_release.value)", TF)
        self.assertIn("desired_count                      = local.fcvm_fleet_coordinator_running ? 1 : 0", block('resource "aws_ecs_service" "fcvm_fleet_coordinator"'))

    def test_the_task_role_reaches_hosts_and_tiles_only(self):
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"')
        self.assertIn('"ec2:LaunchTemplate"', pol)
        self.assertIn('Condition = { StringNotEquals = { "aws:RequestTag/Purpose" = ["fcvm-fleet-tile", "fcvm-fleet-host"] } }', pol)
        self.assertIn('Condition = { StringEquals = { "ec2:CreateAction" = ["RunInstances", "CreateFleet"] } }', pol)
        self.assertIn('"ec2:ResourceTag/Purpose" = ["fcvm-fleet-tile", "fcvm-fleet-host"]', pol)
        self.assertIn("Resource  = [aws_iam_role.fcvm_fleet_host.arn, aws_iam_role.fcvm_fleet_tile.arn]", pol)
        self.assertIn('"iam:PassedToService" = "ec2.amazonaws.com"', pol)
        self.assertNotRegex(pol, r'Action\s*=\s*"(ec2|iam|s3):\*"')

    def test_cloudflared_is_pinned_and_takes_its_token_from_secrets_manager(self):
        td = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertRegex(td, r'image\s+= "cloudflare/cloudflared:\$\{local\.cloudflared_version\}"')
        self.assertRegex(td, r'secrets\s+= \[\{ name = "TUNNEL_TOKEN", valueFrom = local\.fcvm_fleet_tunnel_token_ref \}\]')
        self.assertEqual(len(re.findall(r"readonlyRootFilesystem\s*=\s*true", td)), 2)
        self.assertNotIn(":latest", td)


class StateBucketLifecycleTests(unittest.TestCase):
    def test_noncurrent_state_versions_expire_after_a_week_keeping_the_newest_hundred(self):
        life = block('resource "aws_s3_bucket_lifecycle_configuration" "fcvm_fleet_coordinator_state"')
        self.assertIn("bucket   = aws_s3_bucket.fcvm_fleet_coordinator_state.id", life)
        rules = re.findall(r"^  rule \{\n(.*?)^  \}$", life, re.M | re.S)
        self.assertEqual(len(rules), 2)
        versions, uploads = rules
        self.assertIn('id     = "expire-old-state-versions"', versions)
        self.assertRegex(versions, r'filter \{\s*prefix = "state\.json"\s*\}')
        self.assertRegex(versions, r"noncurrent_days\s+= 7\n")
        self.assertRegex(versions, r"newer_noncurrent_versions = 100\n")
        self.assertIn("filter {}", uploads)
        self.assertRegex(uploads, r"abort_incomplete_multipart_upload \{\s*days_after_initiation = 1\s*\}")
        self.assertNotIn("noncurrent_version_expiration", uploads)
        self.assertNotRegex(life, r"(?m)^\s*expiration \{", "the current state object must never expire")


class SecretTests(unittest.TestCase):
    def test_stage_tls_bootstrap_allows_only_stage_roles_and_terraform(self):
        staging = STAGE_CERT
        self.assertIn('fcvm_fleet_stage_tls_name = "fcvm-fleet/stage/tls"', staging)
        self.assertNotIn('resource "aws_secretsmanager_secret_version"', staging)
        policy = block('resource "aws_secretsmanager_secret_policy" "fcvm_fleet_stage_tls"', staging)
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Principal = "*"', policy)
        self.assertIn('Resource  = aws_secretsmanager_secret.fcvm_fleet_stage_tls.arn', policy)
        self.assertIn('Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]', policy)
        roles = re.search(r'ArnNotEquals = \{ "aws:PrincipalArn" = \[(.*?)\]', policy, re.S)
        self.assertIsNotNone(roles)
        self.assertEqual(set(re.findall(r"[\w.]+", roles[1])), {
            "aws_iam_role.fcvm_fleet_coordinator_task.arn", "aws_iam_role.fcvm_fleet_host.arn",
            "aws_iam_role.fcvm_fleet_tile.arn", "local.fcvm_fleet_access_role_arn",
        })

    def test_only_the_certificate_container_is_created_here(self):
        names = re.findall(r'resource "aws_secretsmanager_secret" "[^"]+" \{[^}]*?\bname\s+= ([^\n]+)', TF, re.S)
        self.assertEqual(names, ['"fcvm-fleet/tls-wildcard"'])
        self.assertRegex(TF, r'\btls_secret\s+= local\.fcvm_fleet_stage_tls_name')
        self.assertNotIn('resource "aws_secretsmanager_secret_version"', TF)

    def test_hosts_and_tiles_read_only_what_they_need(self):
        host = block('resource "aws_iam_role_policy" "fcvm_fleet_host"')
        self.assertIn('Resource = [local.fcvm_fleet_secret_arn["tls"], local.fcvm_fleet_secret_arn["agent-access-token"]]', host)
        self.assertNotIn("ec2:", host)
        tile = block('resource "aws_iam_role_policy" "fcvm_fleet_tile"')
        self.assertIn('Resource = local.fcvm_fleet_secret_arn["tls"]', tile)





class CoordinatorLaunchSplit(unittest.TestCase):
    TF = (ROOT / "fcvm-fleet.tf").read_text()

    def stmt(self, sid):
        return re.split(r'Sid +="%s"' % sid, self.TF.replace('= "', '="'), maxsplit=1)[1].split("\n      },", 1)[0].replace('="', '= "')

    def test_created_and_referenced_resources_are_split_and_pinned(self):
        created = self.stmt("LaunchTilesFromTheirTemplates")
        self.assertIn("Resource  = local.fcvm_fleet_tile_created_arns", created)
        referenced = self.stmt("LaunchTilesWithTheirOwnResources")
        self.assertIn("Resource  = local.fcvm_fleet_tile_referenced_arns", referenced)
        for s in (created, referenced):
            self.assertIn('"ec2:LaunchTemplate" = local.fcvm_fleet_tile_template_condition', s)
        self.assertNotIn('Action    = "ec2:RunInstances"\n        Resource  = "*"', self.TF)
        refs = self.TF.split("fcvm_fleet_tile_referenced_arns = concat(", 1)[1].split("\n  )\n", 1)[0]
        for part in (":image/${ami}", ":subnet/${s}", ":security-group/${m.security_group_id}", "local.fcvm_fleet_tile_template_arns"):
            self.assertIn(part, refs, part)
        self.assertIn('"instance", "volume", "network-interface"', self.TF)
        spot = self.stmt("LaunchTaggedTileSpotRequestsOnly")
        self.assertIn('Resource = local.fcvm_fleet_tile_spot_request_arns', spot)
        self.assertIn('"aws:RequestTag/Purpose" = "fcvm-fleet-tile"', spot)
        self.assertIn('"aws:RequestedRegion"', spot)
        self.assertNotIn('"ec2:LaunchTemplate"', spot)

    def test_state_object_secrets_and_spot_role(self):
        self.assertIn('Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${local.fcvm_fleet_stage.state_key}"', self.TF)
        secrets = self.stmt("FleetSecrets")
        self.assertNotIn('cloudflare-dns-token', secrets)
        self.assertIn('Resource = [local.fcvm_fleet_secret_arn["tls"]]', secrets)
        self.assertNotIn('coordinator-tunnel-token', secrets)
        self.assertNotIn('agent-access-token', secrets)
        self.assertIn('aws_service_name = "spot.amazonaws.com"', self.TF)

class CoordinatorScaleStateAndFallback(unittest.TestCase):
    TF = CoordinatorLaunchSplit.TF
    stmt = CoordinatorLaunchSplit.stmt

    def test_a_missing_state_object_reads_as_404(self):
        s = self.stmt("StateObjectMissingReadsAs404")
        self.assertIn('Action   = "s3:ListBucket"', s)
        self.assertIn("Resource = aws_s3_bucket.fcvm_fleet_coordinator_state.arn\n", s + "\n")

    def test_the_coordinator_launches_hosts_only_from_their_templates(self):
        fleet = self.stmt("LaunchHostsAsInstantFleets")
        self.assertRegex(fleet, r'Action\s*=\s*"ec2:CreateFleet"')
        self.assertRegex(fleet, r'Resource\s*=\s*concat\(local\.fcvm_fleet_host_fleet_arns, local\.fcvm_fleet_host_volume_arns\)')
        self.assertNotIn('Resource  = "*"', fleet)
        self.assertNotIn('Condition', fleet)
        instances = self.stmt("LaunchFleetInstancesInFleetRegionsOnly")
        self.assertIn('Resource = local.fcvm_fleet_host_instance_arns', instances)
        self.assertNotIn('Condition', instances)
        fleet_refs = self.stmt("LaunchHostsWithTheirOwnResources")
        self.assertRegex(fleet_refs, r'Resource\s*=\s*local\.fcvm_fleet_host_referenced_arns')
        self.assertRegex(fleet_refs, r'Action\s*=\s*\["ec2:RunInstances", "ec2:CreateFleet"\]')
        # Instance context is absent on fleet, volume, AMI, template and subnet authorization checks.
        for s in (fleet, fleet_refs):
            self.assertNotIn('"ec2:InstanceType"', s)
            self.assertNotIn('"aws:RequestTag/Purpose"', s)
        created = self.stmt("LaunchHostsFromTheirTemplates")
        referenced = self.stmt("LaunchHostsWithTheirOwnResources")
        self.assertRegex(referenced, r'Resource\s*=\s*local\.fcvm_fleet_host_referenced_arns')
        self.assertIn('"ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition', created)
        self.assertIn('ArnLikeIfExists = { "ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition }', referenced)
        self.assertNotIn('"aws:RequestedRegion"', referenced)
        refs = self.TF.split("fcvm_fleet_host_referenced_arns = concat(", 1)[1].split("\n  )\n", 1)[0]
        for part in ("var.fcvm_fleet_host_amis", ":subnet/${s}", ":security-group/${m.security_group_id}", "local.fcvm_fleet_host_template_arns"):
            self.assertIn(part, refs, part)
        self.assertIn('m.host_subnet_ids', refs)
        self.assertIn('if length(m.host_template_names) > 0', refs)
        self.assertIn('value = local.hosts_on ? [for t in aws_launch_template.host : t.arn] : []',
                      block('output "host_template_arns"', MOD))
        # A host template launches host types only. Instant fleets need no EC2 Fleet service-linked role.
        deny = self.stmt("HostTemplatesLaunchHostTypesOnly")
        self.assertIn('Effect   = "Deny"', deny)
        self.assertIn('ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition }', deny)
        self.assertIn('StringNotEquals = { "ec2:InstanceType" = local.fcvm_fleet_host_types }', deny)
        self.assertIn('fcvm_fleet_host_types              = ["g4dn.metal", "g5g.metal"]', self.TF)
        self.assertNotIn('aws_service_name = "ec2fleet.amazonaws.com"', self.TF)
        self.assertNotIn("autoscaling:", self.TF)

    def test_create_fleet_parent_does_not_require_keys_absent_from_the_real_denial(self):
        # Recorded by STS DecodeAuthorizationMessage, not a synthetic context supplied to the policy simulator.
        fixture = json.loads((ROOT / 'scripts/fixtures/fcvm-fleet-create-fleet-context.json').read_text())
        self.assertEqual(set(fixture['actual_context_key_names']), {
            'aws:Region', 'aws:Service', 'ec2:InstanceID', 'aws:Resource', 'aws:Type', 'aws:Account', 'aws:ARN'})
        self.assertEqual(fixture['matched_statement_ids'], ['FleetTypesOnly'])
        for sid in ('LaunchHostsAsInstantFleets', 'LaunchFleetInstancesInFleetRegionsOnly'):
            parent = self.stmt(sid)
            self.assertNotIn('Condition', parent, sid)
            self.assertNotRegex(parent, r'Resource\s*=\s*"\*"', sid)
        for sid in ('FleetTypesOnly', 'FleetInstancesAreTagged'):
            deny = self.stmt(sid)
            self.assertIn('"ec2:RunInstances"', deny)
            self.assertNotIn('"ec2:CreateFleet"', deny)

    def test_each_templates_purpose_is_enforced_on_run_instances_instances_only(self):
        for sid, kind in (('HostTemplatesAreTaggedHostsOnly', 'host'), ('TileTemplatesAreTaggedTilesOnly', 'tile')):
            deny = self.stmt(sid)
            self.assertIn('Action   = "ec2:RunInstances"', deny)
            self.assertIn('Resource = "arn:aws:ec2:*:*:instance/*"', deny)
            self.assertIn('"ec2:LaunchTemplate" = local.fcvm_fleet_%s_template_condition' % kind, deny)
            self.assertIn('"aws:RequestTag/Purpose" = "fcvm-fleet-%s"' % kind, deny)

    def test_an_on_demand_tile_template_is_a_launchable_template(self):
        od = block('resource "aws_launch_template" "tile_on_demand"', MOD)
        spot = block('resource "aws_launch_template" "tile"', MOD)
        self.assertNotIn("instance_market_options", od)
        self.assertIn("instance_market_options", spot)
        self.assertRegex(od, r'name\s*=\s*"\$\{var\.name_prefix\}-tile-on-demand-\$\{each\.key\}"')
        self.assertIn('tags          = { Name = "fcvm-fleet-tile", Purpose = "fcvm-fleet-tile", Fleet = var.fleet }', od)
        for part in ("iam_instance_profile", "http_tokens                 = \"required\"", "ipv6_address_count          = 1"):
            self.assertIn(part, od, part)
        self.assertIn("[for t in aws_launch_template.tile_on_demand : t.arn]", block('output "tile_template_arns"', MOD))


class TileRootVolume(unittest.TestCase):
    def test_every_tile_template_sets_the_root_volume_to_gp3_1000_mibps_4000_iops(self):
        # A fresh tile reached its first frame in 29 to 40 s on the AMI's default gp3 (125 MiB/s, 3000 IOPS); see the pull
        # request that set this for the measurements.
        self.assertIn('tile_root_volume = { type = "gp3", throughput = 1000, iops = 4000 }', MOD)
        for name in ("tile", "tile_on_demand", "tile_pool"):
            lt = block('resource "aws_launch_template" "%s"' % name, MOD)
            bdm = re.search(r"block_device_mappings \{\n(.*?)\n  \}\n", lt, re.S)
            self.assertIsNotNone(bdm, name)
            self.assertIn('device_name = "/dev/sda1"', bdm.group(1), name)
            for part in ("volume_type = local.tile_root_volume.type", "throughput  = local.tile_root_volume.throughput",
                         "iops        = local.tile_root_volume.iops"):
                self.assertIn(part, bdm.group(1), name)


class TilePool(unittest.TestCase):
    TF = CoordinatorLaunchSplit.TF
    stmt = CoordinatorLaunchSplit.stmt

    def test_the_pool_template_is_persistent_spot_that_stops_and_tags_its_request(self):
        pool = block('resource "aws_launch_template" "tile_pool"', MOD)
        self.assertIn('spot_instance_type             = "persistent"', pool)
        self.assertIn('instance_interruption_behavior = "stop"', pool)
        self.assertIn('resource_type = "spot-instances-request"', pool)
        self.assertEqual(pool.count('Purpose = "fcvm-fleet-tile"'), 2)
        self.assertIn("[for t in aws_launch_template.tile_pool : t.arn]", block('output "tile_template_arns"', MOD))

    def test_start_stop_retag_and_cancel_are_limited_to_tiles(self):
        ss = self.stmt("StartAndStopTilesOnly")
        self.assertIn('["ec2:StartInstances", "ec2:StopInstances"]', ss)
        self.assertIn('"ec2:ResourceTag/Purpose" = "fcvm-fleet-tile"', ss)
        rt = self.stmt("RetagTilesForThePool")
        self.assertIn('"ec2:ResourceTag/Purpose" = "fcvm-fleet-tile"', rt)
        self.assertIn('"aws:TagKeys" = ["FleetPool", "FleetTile"]', rt)
        cs = self.stmt("CancelTileSpotRequestsOnly")
        self.assertIn('Resource  = "arn:aws:ec2:*:*:spot-instances-request/*"', cs)
        self.assertIn('"ec2:ResourceTag/Purpose" = "fcvm-fleet-tile"', cs)
        for s in (ss, rt, cs):
            self.assertNotIn('Resource = "*"', s)

    def test_the_coordinator_gets_the_pool_flags_only_behind_the_switch(self):
        self.assertIn('local.fcvm_fleet_tile_pool ? ["--ec2-pool", "8", "--ec2-pool-template", "${local.fcvm_fleet_stage.prefix}-tile-pool-x86_64"] : []', self.TF)


class ModuleDependencies(unittest.TestCase):
    def test_no_module_level_depends_on(self):
        # A module-level depends_on defers the module's data sources (the AZ list) to apply time, and the subnets'
        # for_each over them then fails to plan; the flow log waits for the bucket policy through an input instead.
        tf = (ROOT / "fcvm-fleet.tf").read_text()
        for name in ("east2", "west2", "east1"):
            start = tf.index('module "fcvm_fleet_%s"' % name)
            body = tf[start:tf.index("\n}\n", start)]
            self.assertNotIn("depends_on", body, name)
            self.assertRegex(body, r"flow_log_bucket_policy_id\s*=\s*aws_s3_bucket_policy\.fcvm_fleet_flow_logs\.id", name)

class CoordinatorTileTypes(unittest.TestCase):
    def test_the_coordinator_launches_only_the_fleet_types_and_tile_templates_only_tile_types(self):
        stmt = CoordinatorLaunchSplit.stmt
        s = stmt(CoordinatorLaunchSplit, "FleetTypesOnly")
        self.assertIn('Effect    = "Deny"', s)
        self.assertIn('Action    = "ec2:RunInstances"', s)
        self.assertIn('"ec2:InstanceType" = concat(local.fcvm_fleet_tile_types, local.fcvm_fleet_host_types)', s)
        tagged = stmt(CoordinatorLaunchSplit, "FleetInstancesAreTagged")
        self.assertIn('Action    = "ec2:RunInstances"', tagged)
        t = stmt(CoordinatorLaunchSplit, "TileTemplatesLaunchTileTypesOnly")
        self.assertIn('ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_tile_template_condition }', t)
        self.assertIn('StringNotEquals = { "ec2:InstanceType" = local.fcvm_fleet_tile_types }', t)
        self.assertIn('fcvm_fleet_tile_types              = ["g4dn.xlarge", "g5g.xlarge"]', CoordinatorLaunchSplit.TF)

class RegistryTests(unittest.TestCase):
    def test_the_bootstrap_image_is_protected_from_the_rolling_retention_rule(self):
        policy = block('resource "aws_ecr_lifecycle_policy" "fcvm_fleet_coordinator"')
        self.assertIn('tagPatternList = [local.fcvm_fleet_coordinator_tag]', policy)
        protected, rolling = policy.split('description  = "Keep the 20 newest images"', 1)
        bootstrap, stage_only = protected.split('var.fcvm_fleet_prod_enabled ? [] : [', 1)
        self.assertIn('tagPatternList = [local.fcvm_fleet_coordinator_tag]', bootstrap)
        self.assertIn("rulePriority = 2", stage_only)
        self.assertIn('rulePriority = 1', protected)
        self.assertRegex(protected, r'\bcountNumber\s*=\s*1\b')
        self.assertIn('rulePriority = 2', protected)
        self.assertIn('countNumber = 20', rolling)

    def test_the_coordinator_image_has_an_immutable_scanned_repository_in_the_fleet_account(self):
        repo = block('resource "aws_ecr_repository" "fcvm_fleet_coordinator"')
        self.assertIn("provider             = aws.fcvm_fleet", repo)
        self.assertIn('name                 = "fcvm-fleet/coordinator"', repo)
        self.assertIn('image_tag_mutability = "IMMUTABLE"', repo)
        self.assertIn("scan_on_push = true", repo)
        life = block('resource "aws_ecr_lifecycle_policy" "fcvm_fleet_coordinator"')
        self.assertIn("countNumber = 20", life)
        self.assertIn("coordinator_repo   = aws_ecr_repository.fcvm_fleet_coordinator.repository_url", TF)


class StageWallTests(unittest.TestCase):
    """The staging wall runs end to end: the host agent dials the coordinator, and the coordinator owns the host group's
    capacity (scale to zero with no viewer) and the EC2 side."""

    def test_the_host_agent_dials_the_coordinator_with_its_secrets_in_the_home_region(self):
        args = TF.split("fcvm_fleet_host_agent_args = join(", 1)[1].split("\n  ])\n", 1)[0]
        for a in ['"--dial wss://${local.fcvm_dev_agent_path}"',
                  '"--access-secret ${aws_secretsmanager_secret.fcvm_fleet_agent_access.name}"',
                  '"--tls-secret ${data.aws_secretsmanager_secret_versions.fcvm_fleet_stage_tls.secret_name}"',
                  '"--secret-region ${local.fcvm_fleet_home_region}"',
                  '"--host-from-imds"', '"--ipv6-prefix-from-imds"', '"--frame-zone ${local.fcvm_fleet_names_zone}"', '"--phones 2"']:
            self.assertIn(a, args)
        self.assertEqual(len(re.findall(r"host_agent_args\s*=\s*local\.fcvm_fleet_host_agent_args\b", TF)), 3)

    def test_boot_writes_the_agent_env_and_starts_the_unit_only_with_arguments(self):
        self.assertIn('%{if var.host_agent_args != ""~}', MOD)
        self.assertIn("> /etc/fcvm-fleet/agent.env", MOD)
        self.assertIn("systemctl enable fcvm-fleet-agent.service", MOD)
        # Not a blocking start from cloud-init: the unit waits for the GPUs and the store.
        self.assertIn("systemctl start --no-block fcvm-fleet-agent.service", MOD)
        self.assertNotIn("systemctl enable --now fcvm-fleet-agent", MOD)

    def test_the_coordinator_sleeps_and_runs_the_ec2_side(self):
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('["--sleep-when-idle"]', cmd)
        self.assertIn('"--allowed-origin", "https://${local.fcvm_dev_site_stage_host}"', cmd)
        self.assertIn('"--ec2-regions", "us-east-1"', cmd)
        self.assertNotIn("--dns-token-secret", tf_code(cmd))
        task = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertIn("command   = local.fcvm_fleet_coordinator_running ? local.fcvm_fleet_coordinator_command : null", task)

    def test_the_coordinator_takes_forwarded_viewer_addresses_only_behind_required_access(self):
        # The coordinator honours x-fleet-viewer-ip only for a service token whose JWT it verifies itself
        # (test_the_coordinator_verifies_the_access_tokens_the_tunnel_admits); the tunnel connector's access.required
        # stays in front of it.
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('"--access-viewer-address"', cmd)
        dev = (ROOT / "fcvm-dev.tf").read_text()
        tunnel = dev[dev.index('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fcvm_fleet_coordinator"'):]
        tunnel = tunnel[:tunnel.index("\n}\n")]
        self.assertEqual(tunnel.count("hostname ="), 2)
        api, direct = tunnel.split("hostname = local.fcvm_dev_direct_stage_host", 1)
        self.assertIn("required  = true", api)
        self.assertIn("required  = true", direct)

    def test_the_coordinator_verifies_the_access_tokens_the_tunnel_admits(self):
        # ejc3/fcvm c74ee76 (fleet/PROTOCOL.md, --access-team-domain): the coordinator checks each Access JWT's
        # signature, issuer, audience and expiry itself. /live takes the viewer applications' audiences, /agent only
        # the agents' application, so a token for one is never accepted on the other.
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        app = "cloudflare_zero_trust_access_application"
        self.assertIn('"--access-team-domain", trimprefix(local.browser_manager_issuer, "https://")', cmd)
        live = re.findall(r'"--access-aud", (\S+?\.aud)\b', cmd)
        agent = re.findall(r'"--agent-access-aud", (\S+?\.aud)\b', cmd)
        self.assertEqual(sorted(live), [f"{app}.fcvm_dev_api_stage.aud", f"{app}.fcvm_dev_site_stage.aud"], cmd)
        self.assertEqual(agent, [f"{app}.fcvm_dev_agent.aud"], cmd)
        # Every audience the tunnel connector admits to the coordinator is one the coordinator verifies, on the side
        # its route serves: a new Access application in front of the coordinator needs its flag here too.
        dev = (ROOT / "fcvm-dev.tf").read_text()
        tunnel = dev[dev.index('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fcvm_fleet_coordinator"'):]
        tunnel = tunnel[:tunnel.index("\n}\n")]
        admitted = [a.strip() for tags in re.findall(r"aud_tag\s*=\s*\[([^\]]*)\]", tunnel) for a in tags.split(",")]
        self.assertEqual(sorted(admitted), sorted(live + agent))
        # The team the tokens are checked against is the team the connector names.
        teams = set(re.findall(r'team_name\s*=\s*"([^"]+)"', tunnel))
        issuer = re.search(r'browser_manager_issuer\s*=\s*"([^"]+)"', (ROOT / "browser-manager.tf").read_text()).group(1)
        self.assertEqual({f"https://{t}.cloudflareaccess.com" for t in teams}, {issuer})
        # Staging viewers all pass Access, so they count as verified; Turnstile is the production stack's.
        self.assertNotIn("--turnstile", cmd)

    def test_the_coordinator_forces_sleep_at_a_750_dollar_daily_spend_ceiling(self):
        # Owner, 2026-11-18: $750 a day at the Spot price, forced sleep. The meter reads DescribeInstances and
        # DescribeSpotPriceHistory, which the task role already has under ec2:Describe*.
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('["--daily-spend-usd", tostring(local.fcvm_fleet_daily_spend_ceiling_usd)]', cmd)
        self.assertRegex(TF, r"fcvm_fleet_daily_spend_ceiling_usd\s*=\s*750\n")
        self.assertIn('"--sleep-when-idle"', cmd)
        self.assertIn('Action   = ["ec2:Describe*"]', TF)

    def test_the_coordinator_checks_viewer_words_with_haiku_it_may_invoke(self):
        # The default, --text-check none, refuses every viewer word. The model the command names is the one the
        # task role may invoke, through the profile in the fleet account and the foundation model it routes to.
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('["--text-check", "bedrock", "--text-check-model", local.bedrock_haiku_profile, '
                      '"--text-check-region", local.bedrock_haiku_region]', cmd)
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"')
        grant = pol.split('Sid    = "CheckViewerWordsWithClaudeHaiku55"', 1)[1].split("\n      },", 1)[0]
        self.assertIn('Action = "bedrock:InvokeModel"', grant)
        self.assertIn('"arn:aws:bedrock:*:${local.fcvm_fleet_account_id}:inference-profile/${local.bedrock_haiku_profile}"', grant)
        self.assertIn('"arn:aws:bedrock:*::foundation-model/${local.bedrock_haiku_model}"', grant)
        self.assertIn('"aws-marketplace:ProductId" = ["prod-6cyn7tgqazjhu"]', pol)


class TileSubnetTests(unittest.TestCase):
    def test_both_tile_templates_name_a_fleet_subnet(self):
        # Without one, a launch from the template lands in the default VPC's subnet, which the coordinator may not use.
        for name in ('resource "aws_launch_template" "tile"', 'resource "aws_launch_template" "tile_on_demand"'):
            self.assertIn("subnet_id                   = aws_subnet.public[local.tile_az].id", block(name, MOD))
        self.assertIn("tile_az = local.azs[0]", MOD)

    def test_the_coordinator_gets_every_fleet_subnet_and_group_for_tile_launches(self):
        # A tile launch tries each AZ's subnet of a region, in order, before the next region (ejc3/fcvm#1284). The
        # launch names the subnet and the group, which the role may reference by ARN.
        task = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertIn('{ name = "FLEET_EC2_SUBNETS", value = join(" ", [for r, m in local.fcvm_fleet_regional : '
                      '"${r}:${join(",", m.subnet_ids)}"]) }', task)
        self.assertIn('{ name = "FLEET_EC2_SECURITY_GROUPS", value = join(" ", [for r, m in local.fcvm_fleet_regional : '
                      '"${r}:${m.security_group_id}"]) }', task)
        refs = TF.split("fcvm_fleet_tile_referenced_arns = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('[for s in m.subnet_ids : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:subnet/${s}"]', refs)
        self.assertIn('"arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:security-group/${m.security_group_id}"', refs)


class PublishedPortsTests(unittest.TestCase):
    """The copy of ejc3/fcvm's port declaration is the one on its main: a mode or a count fcvm changes there reaches the
    security group only through this file, so a stale copy fails here rather than dropping viewers."""

    def test_the_copy_is_fcvm_mains(self):
        import urllib.request
        url = "https://raw.githubusercontent.com/ejc3/fcvm/main/fleet/published-ports.json"
        with urllib.request.urlopen(url, timeout=30) as response:
            upstream = response.read()
        self.assertEqual((ROOT / "fcvm-fleet-published-ports.json").read_bytes(), upstream,
                         "fcvm-fleet-published-ports.json differs from ejc3/fcvm main's fleet/published-ports.json: copy it")


class SlotV4Tests(unittest.TestCase):
    """IPv4 viewers reach a GPU host's slots on its one public address, a port per slot."""

    def test_slot_publish_contract_and_ipv4_ingress_agree(self):
        # Every number comes from ejc3/fcvm's declaration (fcvm-fleet-published-ports.json, a copy of fleet/published-ports.json).
        decl = json.loads((ROOT / "fcvm-fleet-published-ports.json").read_text())
        ranges = {r["mode"]: r for r in decl["gpu_host_ipv4"]["ranges"]}
        base = decl["gpu_host_ipv4"]["base"]
        self.assertIn('fcvm_fleet_published_ports    = jsondecode(file("${path.module}/fcvm-fleet-published-ports.json"))', TF)
        self.assertIn("fcvm_fleet_slot_tls_port_base = local.fcvm_fleet_published_ports.gpu_host_ipv4.base", TF)
        for local, mode in (("fcvm_fleet_host_gpu_slots", "slot"), ("fcvm_fleet_host_windows", "windows"),
                            ("fcvm_fleet_host_minecraft", "minecraft")):
            self.assertRegex(TF, rf"{local}\s+= local\.fcvm_fleet_tls_port_ranges\.{mode}\.count\b")
        self.assertNotRegex(TF, r"fcvm_fleet_slot_tls_port_base\s+=\s+\d", "the base is a literal again")
        # Every region's range is one expression over the declaration; regions differ only in which modes they admit.
        ports = re.search(r"  fcvm_fleet_ipv4_viewer_ports = \{(.*?)\n  \} \}", TF, re.S)[1]
        self.assertIn("from = local.fcvm_fleet_slot_tls_port_base + min([for m in modes : "
                      "local.fcvm_fleet_tls_port_ranges[m].offset]...)", ports)
        self.assertIn("to   = local.fcvm_fleet_slot_tls_port_base + max([for m in modes : "
                      "local.fcvm_fleet_tls_port_ranges[m].offset + local.fcvm_fleet_tls_port_ranges[m].count]...) - 1", ports)
        self.assertNotRegex(ports, r"\b20\d\d\d\b", "a port number is written into the expression")
        # Only us-east-1 may add the extra ports; closing its gate reuses the ordinary GPU slot range.
        gate = "fcvm_fleet_extra_ipv4_viewer_ports_enabled"
        self.assertRegex(block(f'variable "{gate}"'), r"(?m)^  default\s*= true$")
        modes = re.search(r"  fcvm_fleet_ipv4_viewer_modes = \{(.*?)\n  \}", TF, re.S)[1]
        self.assertIn(f'"us-east-1" = var.{gate} ? [', modes)
        self.assertIn('for m in keys(local.fcvm_fleet_tls_port_ranges) : m if m != "aggregate" || local.fcvm_fleet_aggregated', modes)
        self.assertIn('] : ["slot"]', modes)
        self.assertRegex(modes, r'(?m)^    "us-east-2" = \["slot"\]$')
        self.assertRegex(modes, r'(?m)^    "us-west-2" = \["slot"\]$')
        self.assertIn("fcvm_fleet_aggregate_tls_port = local.fcvm_fleet_slot_tls_port_base + "
                      "local.fcvm_fleet_tls_port_ranges.aggregate.offset", TF)
        # What that evaluates to today: the range admits every place, and the aggregator's port is the one after.
        end = max(r["offset"] + r["count"] for m, r in ranges.items() if m != "aggregate")
        self.assertEqual((base, base + end - 1, base + ranges["aggregate"]["offset"]), (20200, 20215, 20216))
        self.assertEqual({m: (base + r["offset"], r["count"]) for m, r in ranges.items()},
                         {"slot": (20200, 8), "windows": (20208, 4), "minecraft": (20212, 4), "aggregate": (20216, 1)})
        # The counts the agent is started with are the ones the range was sized for, in every region.
        self.assertIn('agent_args="$agent_args --windows ${var.windows_count}"', MOD)
        self.assertIn('agent_args="$agent_args --minecraft ${var.minecraft_count}"', MOD)
        self.assertEqual(len(re.findall(r"windows_count\s*=\s*local\.fcvm_fleet_host_windows\n", TF)), 3)
        self.assertEqual(len(re.findall(r"minecraft_count\s*=\s*local\.fcvm_fleet_host_minecraft\n", TF)), 3)
        # The agent forwards from the same base.
        self.assertIn('"--tls-publish-base ${local.fcvm_fleet_slot_tls_port_base}"', TF)
        self.assertIn('"--public-ipv4-from-imds"', TF)
        sg = block('resource "aws_security_group" "fleet"', MOD)
        rules = re.findall(r"  ingress \{\n(.*?)\n  \}", sg, re.S)
        self.assertEqual(len(rules), 2, rules)
        slots = [rule for rule in rules if "var.slot_tls_ports" in rule]
        self.assertEqual(len(slots), 1)
        self.assertIn('description = "GPU slot renderer TLS over IPv4"', slots[0])
        self.assertIn("from_port   = var.slot_tls_ports.from", slots[0])
        self.assertIn("to_port     = var.slot_tls_ports.to", slots[0])
        self.assertIn('cidr_blocks = ["0.0.0.0/0"]', slots[0])
        # Every region module takes its range from the one map, under its own region's key.
        for module, region in (("east1", "us-east-1"), ("east2", "us-east-2"), ("west2", "us-west-2")):
            self.assertRegex(block(f'module "fcvm_fleet_{module}"'),
                             rf'(?m)^  slot_tls_ports\s*= local\.fcvm_fleet_ipv4_viewer_ports\["{region}"\]$')

    def test_the_coordinator_serves_the_launch_names_subzone(self):
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertNotIn("--dns-nameservers", cmd)
        self.assertIn('["--dns-subzone", local.fcvm_fleet_names_zone, "--dns-subzone-ns", join(",", local.fcvm_fleet_names_ns), '
                      '"--dns-listen", "0.0.0.0:${local.fcvm_fleet_dns_port}"]', cmd)
        # The certificate is the subzone's, its DNS-01 record answered by this server (StageSubzoneCertificateTests).
        self.assertIn('"--tls-secret", data.aws_secretsmanager_secret_versions.fcvm_fleet_stage_tls.secret_name, "--tls-scope", "subzone"', cmd)


class DnsTests(unittest.TestCase):
    """fcvm-fleet-dns.tf: the subzone is delegated to two static addresses that reach the coordinator's DNS port."""

    def test_the_subzone_is_below_the_zone_and_its_nameservers_are_outside_it(self):
        zone = re.search(r'fcvm_fleet_names_zone = "([^"]+)"', DNS)[1]
        ns = re.findall(r'"(ns\d-[^"]+)"', DNS.split("fcvm_fleet_names_ns", 1)[1].split("\n", 1)[0])
        self.assertTrue(zone.endswith(".fcvm.dev"), zone)
        self.assertEqual(len(ns), 2)
        for n in ns:
            self.assertTrue(n.endswith(".fcvm.dev") and not n.endswith("." + zone), n)

    def test_two_static_addresses_in_two_azs_reach_the_one_task_across_zones(self):
        self.assertIn("count    = 2", block('resource "aws_eip" "fcvm_fleet_dns"', DNS))
        lb = block('resource "aws_lb" "fcvm_fleet_dns"', DNS)
        self.assertIn('load_balancer_type               = "network"', lb)
        self.assertIn("enable_cross_zone_load_balancing = true", lb)
        self.assertIn("allocation_id = aws_eip.fcvm_fleet_dns[subnet_mapping.key].id", lb)
        self.assertIn("fcvm_fleet_dns_subnets = slice(module.fcvm_fleet_east2.subnet_ids, 0, 2)", DNS)

    def test_udp_and_tcp_53_go_to_the_tasks_high_port_with_the_client_address(self):
        listener = block('resource "aws_lb_listener" "fcvm_fleet_dns"', DNS)
        self.assertIn("port              = 53", listener)
        self.assertIn('protocol          = "TCP_UDP"', listener)
        tg = block('resource "aws_lb_target_group" "fcvm_fleet_dns"', DNS)
        self.assertIn('protocol             = "TCP_UDP"', tg)
        self.assertIn("port                 = local.fcvm_fleet_dns_port", tg)
        self.assertIn('target_type          = "ip"', tg)
        self.assertIn('preserve_client_ip   = "true"', tg)
        self.assertIn('protocol            = "TCP"', tg)
        self.assertIn("fcvm_fleet_dns_port = 5353", DNS)
        svc = block('resource "aws_ecs_service" "fcvm_fleet_coordinator"')
        self.assertIn("target_group_arn = aws_lb_target_group.fcvm_fleet_dns.arn", svc)
        self.assertIn("container_port   = local.fcvm_fleet_dns_port", svc)
        self.assertIn('portMappings = [{ containerPort = local.fcvm_fleet_dns_port, protocol = "udp" }]', TF)

    def test_the_task_runs_only_where_the_load_balancer_listens_and_after_its_listener(self):
        # A target in an AZ the load balancer does not enable is unused even across zones, and ECS refuses a target
        # group no listener attaches yet.
        svc = block('resource "aws_ecs_service" "fcvm_fleet_coordinator"')
        self.assertIn("subnets          = local.fcvm_fleet_dns_subnets", svc)
        self.assertRegex(svc, r"depends_on\s*=\s*\[[^\]]*aws_lb_listener\.fcvm_fleet_dns")

    def test_the_coordinator_group_keeps_its_original_description(self):
        # The description is ForceNew, and the running task's interface keeps the group from being deleted.
        coord = block('resource "aws_security_group" "fcvm_fleet_coordinator"')
        self.assertIn('description = "fcvm fleet coordinator task: nothing in (browsers and agents arrive through its '
                      'Cloudflare tunnel), all out"', coord)

    def test_the_delegation_is_ns_records_with_unproxied_addresses_and_no_ds(self):
        addr = block('resource "cloudflare_dns_record" "fcvm_fleet_names_ns_address"', DNS)
        self.assertIn('type    = "A"', addr)
        self.assertIn("content = aws_eip.fcvm_fleet_dns[count.index].public_ip", addr)
        self.assertIn("proxied = false", addr)
        ns = block('resource "cloudflare_dns_record" "fcvm_fleet_names_delegation"', DNS)
        self.assertIn('type       = "NS"', ns)
        self.assertIn("name       = local.fcvm_fleet_names_zone", ns)
        self.assertNotRegex(DNS, r'type\s*=\s*"(DS|AAAA)"')



class AggregatedTests(unittest.TestCase):
    """Aggregated streaming (ejc3/fcvm#1232) is one switch, off, and off changes nothing staging runs."""

    def test_the_switch_is_off(self):
        self.assertRegex(TF, r"\n  fcvm_fleet_aggregated = false\n")

    def test_everything_it_adds_is_gated_by_it(self):
        agent = TF.split("fcvm_fleet_host_agent_args = join(", 1)[1].split("\n\n", 1)[0]
        self.assertIn('], local.fcvm_fleet_aggregated ? [', agent)
        self.assertIn('"--aggregate-tls-port ${local.fcvm_fleet_aggregate_tls_port}"', agent)
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('local.fcvm_fleet_aggregated ? ["--ec2-aggregate-listen", "127.0.0.1:', cmd)
        self.assertIn('"--ec2-aggregate-tls"', cmd)
        task = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertIn('for_each = local.fcvm_fleet_aggregated ? ["fleet-run"] : []', task)
        self.assertIn("[for _ in(local.fcvm_fleet_aggregated ? [1] : []) : {", task)
        self.assertIn('namespace = "net.ipv4.ip_unprivileged_port_start", value = "443"', task)
        self.assertIn('containerPath = "/run/fcvm-fleet"', task)
        self.assertIn('"/run/fcvm-fleet/tls"', cmd)
        dual = block('resource "aws_ecs_account_setting_default" "fcvm_fleet_dual_stack"')
        # The broadcast reads every tile at its IPv6 name too (fcvm-fleet-broadcast.tf).
        self.assertIn("count    = local.fcvm_fleet_aggregated || local.fcvm_fleet_broadcast ? 1 : 0", dual)

    def test_the_host_aggregator_port_follows_the_slots(self):
        # After every place's port: the GPU slots', the Windows sandboxes' and the Minecraft servers'.
        self.assertIn("fcvm_fleet_aggregate_tls_port = local.fcvm_fleet_slot_tls_port_base + "
                      "local.fcvm_fleet_tls_port_ranges.aggregate.offset", TF)
        priv = int(re.search(r"fcvm_fleet_aggregate_port\s+= (\d+)", TF).group(1))
        # The private listener is outside the admitted range.
        self.assertFalse(20200 <= priv <= 20216, priv)

class BroadcastTests(unittest.TestCase):
    """The broadcast (ejc3/fcvm "Broadcast mode") is one switch: the coordinator's flags, its task size, its
    secret and the private IVS channel all follow it."""

    BC = (ROOT / "fcvm-fleet-broadcast.tf").read_text()

    def test_the_coordinator_gets_its_flags_and_size_from_the_switch(self):
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('local.fcvm_fleet_broadcast ? ["--broadcast-listen", "127.0.0.1:', cmd)
        self.assertIn('"--broadcast-secret", aws_secretsmanager_secret.fcvm_fleet_broadcast[0].name', cmd)
        self.assertIn('"--direct-capacity", tostring(local.fcvm_fleet_direct_capacity)', cmd)
        task = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertIn('cpu                = local.fcvm_fleet_broadcast ? "4096" : "512"', task)  # 1080 lines need 4 CPUs (ejc3/fcvm#1498)
        self.assertIn('memory             = local.fcvm_fleet_broadcast ? "8192" : "1024"', task)

    def test_the_channel_is_private_and_only_the_coordinator_reads_its_secret(self):
        ch = block('resource "aws_ivs_channel" "fcvm_fleet_broadcast"', self.BC)
        self.assertIn("authorized   = true", ch)
        self.assertIn('latency_mode = "LOW"', ch)
        pol = block('resource "aws_iam_policy" "fcvm_fleet_coordinator_broadcast"', self.BC)
        self.assertIn('Action   = "secretsmanager:GetSecretValue"', pol)
        self.assertIn("Resource = aws_secretsmanager_secret.fcvm_fleet_broadcast[0].arn", pol)
        self.assertEqual(pol.count("Effect"), 1)
        self.assertNotIn('resource "aws_iam_role_policy"', self.BC)
        attachment = block('resource "aws_iam_role_policy_attachment" "fcvm_fleet_coordinator_broadcast"', self.BC)
        self.assertIn("role       = aws_iam_role.fcvm_fleet_coordinator_task.name", attachment)
        self.assertIn("policy_arn = aws_iam_policy.fcvm_fleet_coordinator_broadcast[0].arn", attachment)
        for r in re.findall(r'resource "([a-z_]+)" "fcvm_fleet_broadcast[a-z_]*"', self.BC):
            self.assertIn(r, {"aws_ivs_channel", "tls_private_key", "aws_ivs_playback_key_pair", "aws_secretsmanager_secret",
                              "aws_secretsmanager_secret_version", "aws_iam_policy", "aws_iam_role_policy_attachment"}, r)
        # Every one of its seven resources and its one data source is counted by the switch.
        self.assertEqual(len(re.findall(r"count\s+= local\.fcvm_fleet_broadcast \? 1 : 0", self.BC)), 8)

    def test_first_rollout_waits_for_credentials_and_the_read_grant(self):
        service = block('resource "aws_ecs_service" "fcvm_fleet_coordinator"')
        dependencies = re.search(r"depends_on\s*=\s*\[(.*?)\]", service, re.S).group(1)
        self.assertEqual(set(re.findall(r"\b(?:ephemeral\.)?aws_[\w.]+", dependencies)), {
            "ephemeral.aws_ssm_parameter.fcvm_coordinator_release_guard",
            "aws_lb_listener.fcvm_fleet_dns",
            "aws_secretsmanager_secret_version.fcvm_fleet_broadcast",
            "aws_iam_role_policy_attachment.fcvm_fleet_coordinator_broadcast",
        })


class StackScopeTests(unittest.TestCase):
    """Staging is one stack of the fleet, scoped by its Fleet tag, so a second stack (production, fcvm.dev) in the same
    account can be added beside it: no staging credential may launch, stop, start, retag, terminate or cancel what
    another stack's coordinator launched, and the reverse (ejc3/fcvm fleet production plan, invariant I2)."""

    def stacks(self):
        return TF.split("fcvm_fleet_stacks = {", 1)[1].split("\n  }\n", 1)[0]

    def scope(self):
        return block('resource "aws_iam_policy" "fcvm_fleet_stack_scope"')

    def deny(self, sid):
        pol = self.scope()
        parts = re.split(r'Sid +="%s"' % sid, pol.replace('= "', '="'), maxsplit=1)
        self.assertEqual(len(parts), 2, sid)
        # Without terraform fmt's alignment, so an assertion reads "Key = value".
        return re.sub(r"(\w) +=", r"\1 =", parts[1].split("\n      },", 1)[0].replace('="', '= "'))

    def test_the_stage_stack_keeps_the_names_staging_already_has(self):
        stage = self.stacks().split("stage = {", 1)[1].split("\n    }", 1)[0]
        self.assertRegex(stage, r'\bfleet\s+= "stage"')
        self.assertRegex(stage, r'\bprefix\s+= "fcvm-fleet"')
        self.assertRegex(stage, r'\btls_secret\s+= local\.fcvm_fleet_stage_tls_name')
        self.assertRegex(stage, r'\bstate_key\s+= "state.json"')
        self.assertRegex(stage, r"\buntagged_spot_requests\s+= true")
        self.assertIn("fcvm_fleet_stage = local.fcvm_fleet_stacks.stage", TF)
        # The zone certificate's secret keeps its name; staging's subzone certificate is the stack's.
        self.assertRegex(block('resource "aws_secretsmanager_secret" "fcvm_fleet_tls"'), r'\bname\s+= "fcvm-fleet/tls-wildcard"\n')
        self.assertRegex(block('resource "aws_secretsmanager_secret" "fcvm_fleet_stage_tls"', STAGE_CERT), r"\bname\s+= local\.fcvm_fleet_stage_tls_name\n")
        self.assertIn('Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${local.fcvm_fleet_stage.state_key}"',
                      block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"'))
        self.assertIn('{ name = "FLEET_STATE_KEY", value = local.fcvm_fleet_stage.state_key }',
                      block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"'))

    def test_the_coordinator_is_given_its_stack_as_its_fleet_name(self):
        # ejc3/fcvm src/cli/args.rs: --fleet-name (FLEET_NAME, default "stage") is the Fleet tag the coordinator sets on
        # every tile (RunInstances) and host (CreateFleet) it launches and filters DescribeInstances by: the sweep, the
        # agent identity check and the spend meter's listing and Fleet metric dimension.
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('["--fleet-name", local.fcvm_fleet_stage.fleet]', cmd)
        for template in ("tile", "tile-on-demand", "tile-pool"):
            self.assertIn('"${local.fcvm_fleet_stage.prefix}-%s-x86_64"' % template, cmd)
        self.assertNotIn('"fcvm-fleet-tile', cmd)

    def test_every_launch_template_tags_what_it_launches_with_the_stack(self):
        for name in ("east2", "west2", "east1"):
            mod = block('module "fcvm_fleet_%s"' % name)
            self.assertRegex(mod, r"\n  fleet\s+= local\.fcvm_fleet_stage\.fleet\n", name)
            self.assertRegex(mod, r"\n  name_prefix\s+= local\.fcvm_fleet_stage\.prefix\n", name)
        for kind, purpose in (("host", "host"), ("tile", "tile"), ("tile_on_demand", "tile"), ("tile_pool", "tile")):
            lt = block('resource "aws_launch_template" "%s"' % kind, MOD)
            specs = re.findall(r"  tag_specifications \{\n(.*?)\n  \}", lt, re.S)
            self.assertEqual(len(specs), 2 if kind == "tile_pool" else 1, kind)
            for spec in specs:
                self.assertIn('Purpose = "fcvm-fleet-%s", Fleet = var.fleet }' % purpose, spec)
            self.assertRegex(lt, r'\n  name\s+= "\$\{var\.name_prefix\}-%s-\$\{each\.key\}"' % kind.replace("_", "-"))
            self.assertIn('tags = { Name = "${var.name_prefix}-%s-${each.key}", Fleet = var.fleet }' % kind.replace("_", "-"), lt)

    def test_the_coordinator_touches_only_its_own_stacks_instances(self):
        pol = self.scope()
        # Every stack that exists: production's only while its switch is on (ProdStackTests).
        self.assertRegex(pol, r"\n  for_each\s+= local\.fcvm_fleet_stacks_enabled\n")
        self.assertIn('fcvm_fleet_stacks_enabled = { for k, s in local.fcvm_fleet_stacks : k => s if k != "prod" || var.fcvm_fleet_prod_enabled }', TF)
        self.assertEqual(len(re.findall(r'Effect\s+= "Deny"', pol)), 4, pol)
        self.assertNotRegex(pol, r'Effect\s+= "Allow"')
        touch = self.deny("OnlyThisStacksInstances")
        self.assertIn('Action = ["ec2:TerminateInstances", "ec2:StopInstances", "ec2:StartInstances"]', touch)
        self.assertIn('Resource = "arn:aws:ec2:*:*:instance/*"', touch)
        # StringNotEquals is true for an instance with no Fleet tag: a staging credential cannot act on it either.
        self.assertIn('Condition = { StringNotEquals = { "ec2:ResourceTag/Fleet" = each.value.fleet } }', touch)
        retag = self.deny("RetagOnlyThisStacksInstances")
        self.assertIn('Action = ["ec2:CreateTags", "ec2:DeleteTags"]', retag)
        self.assertIn('StringNotEquals = { "ec2:ResourceTag/Fleet" = each.value.fleet }', retag)
        # Tags written by a launch are the launch's (LaunchOnlyThisStacksInstances), not a retag.
        self.assertIn('Null = { "ec2:CreateAction" = "true" }', retag)
        launch = self.deny("LaunchOnlyThisStacksInstances")
        self.assertIn('Action = "ec2:RunInstances"', launch)
        self.assertIn('Resource = "arn:aws:ec2:*:*:instance/*"', launch)
        self.assertIn('Condition = { StringNotEquals = { "aws:RequestTag/Fleet" = each.value.fleet } }', launch)
        cancel = self.deny("CancelOnlyThisStacksSpotRequests")
        self.assertIn('Action = "ec2:CancelSpotInstanceRequests"', cancel)
        self.assertIn('Resource = "arn:aws:ec2:*:*:spot-instances-request/*"', cancel)
        self.assertIn('{ StringNotEquals = { "ec2:ResourceTag/Fleet" = each.value.fleet } }', cancel)
        # The pool's persistent spot requests from before the pool template tagged them Fleet: only a stack that has
        # them (untagged_spot_requests) may cancel an untagged one; a request tagged for another stack is always denied.
        self.assertIn('[for _ in(each.value.untagged_spot_requests ? [1] : []) : { Null = { "ec2:ResourceTag/Fleet" = "false" } }]...',
                      cancel)

    def test_the_scope_policy_is_attached_to_the_stacks_coordinator_after_its_templates_tag_the_stack(self):
        att = block('resource "aws_iam_role_policy_attachment" "fcvm_fleet_stack_scope"')
        self.assertRegex(att, r"\n  for_each\s+= local\.fcvm_fleet_stack_coordinator_roles\n")
        self.assertIn("role       = each.value", att)
        self.assertIn("policy_arn = aws_iam_policy.fcvm_fleet_stack_scope[each.key].arn", att)
        roles = TF.split("fcvm_fleet_stack_coordinator_roles = merge(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn("{ stage = aws_iam_role.fcvm_fleet_coordinator_task.name },", roles)
        self.assertIn('{ for r in aws_iam_role.fcvm_fleet_prod_coordinator_task : "prod" => r.name },', roles)
        # The deny on launches must not take effect before the templates carry the Fleet tag.
        self.assertRegex(att, r"depends_on = \[module\.fcvm_fleet_east2, module\.fcvm_fleet_west2, module\.fcvm_fleet_east1, "
                              r"module\.fcvm_fleet_prod_east1\]")
        # The coordinator's inline policy is at its size limit: the scope adds nothing to it.
        self.assertNotIn('/Fleet"', block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"'))

    def test_what_the_stack_owns_carries_its_fleet_tag(self):
        for header in ('resource "aws_iam_role" "fcvm_fleet_host"', 'resource "aws_iam_role" "fcvm_fleet_tile"',
                       'resource "aws_iam_role" "fcvm_fleet_coordinator_execution"',
                       'resource "aws_iam_role" "fcvm_fleet_coordinator_task"',
                       'resource "aws_secretsmanager_secret" "fcvm_fleet_tls"',
                       'resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"',
                       'resource "aws_ecs_service" "fcvm_fleet_coordinator"'):
            self.assertRegex(block(header), r"\n  tags\s+= \{[^\n]*Fleet = local\.fcvm_fleet_stage\.fleet", header)
        dev = (ROOT / "fcvm-dev.tf").read_text()
        for name in ("fcvm_fleet_coordinator_tunnel", "fcvm_fleet_agent_access"):
            self.assertRegex(block('resource "aws_secretsmanager_secret" "%s"' % name, dev),
                             r"\n  tags\s+= \{[^\n]*Fleet = local\.fcvm_fleet_stage\.fleet", name)
        bc = (ROOT / "fcvm-fleet-broadcast.tf").read_text()
        self.assertRegex(block('resource "aws_secretsmanager_secret" "fcvm_fleet_broadcast"', bc),
                         r"\n  tags\s+= \{[^\n]*Fleet = local\.fcvm_fleet_stage\.fleet")


class StateVersionRecoveryTests(unittest.TestCase):
    """ejc3/fcvm#1951: the coordinator reads an earlier version of state.json when the current one does not parse, and
    logs two markers that page through the fleet's alert path."""
    def test_the_role_reads_versions_of_the_state_object_and_lists_versions_of_its_bucket_only(self):
        recovery = block('resource "aws_iam_policy" "fcvm_fleet_state_recovery"')
        self.assertEqual(re.findall(r'Action\s+= "([^"]+)"', recovery), ["s3:GetObjectVersion", "s3:ListBucketVersions"])
        get, listing = recovery.split('Sid       = "StateObjectVersionList"')
        self.assertIn('Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/state.json"', get)
        self.assertIn("Resource  = aws_s3_bucket.fcvm_fleet_coordinator_state.arn\n", listing)
        self.assertIn('"s3:prefix" = "state.json"', listing)
        self.assertNotIn("*", recovery)

    def test_state_recovery_is_attached_without_using_the_roles_inline_policy_quota(self):
        inline = block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"')
        for action in ('"s3:GetObjectVersion"', '"s3:ListBucketVersions"'):
            self.assertNotIn(action, inline)
            self.assertEqual(TF.count(action), 1)
        attachment = block('resource "aws_iam_role_policy_attachment" "fcvm_fleet_state_recovery"')
        self.assertIn("role       = aws_iam_role.fcvm_fleet_coordinator_task.name", attachment)
        self.assertIn("policy_arn = aws_iam_policy.fcvm_fleet_state_recovery.arn", attachment)

    def test_both_state_markers_alarm_into_the_on_demand_alarms_forwarder(self):
        for name, metric, marker in (("fcvm-fleet-state-write-lost", "StateWriteLost", "FLEET_STATE_WRITE_LOST"),
                                     ("fcvm-fleet-state-unreadable", "StateUnreadable", "FLEET_STATE_UNREADABLE")):
            entry = ALARMS.split(f'"{name}" = {{', 1)[1].split("\n    }", 1)[0]
            self.assertIn(f'metric = "{metric}"', entry)
            self.assertIn('pattern     = "\\"%s\\""' % marker, entry)
            self.assertIn("periods     = 1", entry)
            revision, source = (ROOT / "scripts/fixtures/fcvm-coordinator-log-lines.txt").read_text().split("\n", 1)
            self.assertIn('"%s"' % marker, source, f"{marker} is missing from ejc3/fcvm at {revision}")
            self.assertIn(f'"{name}"', ALARMS.split("fcvm_fleet_health_alarm_names", 1)[0])
        # The shared resources give each entry the on-demand alarm's namespace, forwarder, 5-minute Sum >= 1 and
        # not-breaching treatment of missing data.
        alarm = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_health_logs"', ALARMS)
        for line in ('comparison_operator = "GreaterThanOrEqualToThreshold"', 'statistic           = "Sum"',
                     "period              = 300", "threshold           = 1", 'treat_missing_data  = "notBreaching"',
                     "alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]"):
            self.assertIn(line, alarm)
        metric_filter = block('resource "aws_cloudwatch_log_metric_filter" "fcvm_fleet_health"', ALARMS)
        self.assertIn('namespace     = "FCVM/Fleet"', metric_filter)
        self.assertIn("log_group_name = aws_cloudwatch_log_group.fcvm_fleet_coordinator.name", metric_filter)
        on_demand = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_on_demand_launches"')
        self.assertIn("alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]", on_demand)
        # The forwarder is allowed to be invoked by, and publishes for, every alarm of the map.
        self.assertIn("values(local.fcvm_fleet_health_alarm_arns)", TF)
        self.assertIn("for_each       = local.fcvm_fleet_health_alarm_arns",
                      block('resource "aws_lambda_permission" "fcvm_fleet_health_alarms"', ALARMS))


PROD = (ROOT / "fcvm-fleet-prod.tf").read_text() if (ROOT / "fcvm-fleet-prod.tf").exists() else ""
PROD_DNS = (ROOT / "fcvm-fleet-prod-dns.tf").read_text() if (ROOT / "fcvm-fleet-prod-dns.tf").exists() else ""
PROD_BC = (ROOT / "fcvm-fleet-prod-broadcast.tf").read_text() if (ROOT / "fcvm-fleet-prod-broadcast.tf").exists() else ""
DEV_PROD = (ROOT / "fcvm-dev-prod.tf").read_text()



class StageSubzoneCertificateTests(unittest.TestCase):
    """Staging's coordinator takes the subzone-only certificate (ejc3/fcvm#1973): *.tiles-stage.fcvm.dev in a secret of its
    own, its DNS-01 record answered by its own DNS server, no Cloudflare token; and BroadcastLive's IVS read (#1974)."""

    def cmd(self):
        return TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]

    def test_the_coordinator_orders_the_subzone_certificate_with_no_cloudflare_token(self):
        cmd = tf_code(self.cmd())
        self.assertIn('["--dns-zone", "fcvm.dev", "--tls-secret", data.aws_secretsmanager_secret_versions.fcvm_fleet_stage_tls.secret_name, '
                      '"--tls-scope", "subzone", "--secrets-region", local.fcvm_fleet_home_region]', cmd)
        for gone in ("--dns-token-secret", "--dns-zone-id", "fcvm_fleet_dns_token", "fcvm_fleet_tls.name"):
            self.assertNotIn(gone, cmd)
        self.assertEqual(re.search(r'fcvm_fleet_names_zone = "([^"]+)"', DNS)[1], "tiles-stage.fcvm.dev")
        # --tls-scope subzone needs --dns-subzone (ejc3/fcvm cert.rs, tls_plan).
        self.assertIn('"--dns-subzone", local.fcvm_fleet_names_zone', cmd)

    def test_no_staging_role_reads_the_dns_token_or_the_zone_certificate(self):
        self.assertNotIn("fcvm_fleet_dns_token", tf_code(TF))
        self.assertNotIn('"cloudflare-dns-token"', TF)
        self.assertNotIn('"tls-wildcard"', TF)
        self.assertIn('"tls"                      = data.aws_secretsmanager_secret_versions.fcvm_fleet_stage_tls.secret_arn', TF)
        for role in ("fcvm_fleet_host", "fcvm_fleet_tile", "fcvm_fleet_coordinator_task"):
            policy = tf_code(block(f'resource "aws_iam_role_policy" "{role}"'))
            self.assertNotIn("fcvm_fleet_tls.", policy, role)
            self.assertNotIn("cloudflare", policy.lower().replace("cloudflared", ""), role)

    def test_hosts_and_tiles_are_pointed_at_the_subzone_certificate(self):
        args = TF.split("fcvm_fleet_host_agent_args = join(", 1)[1].split("\n  ])\n", 1)[0]
        self.assertIn('"--tls-secret ${data.aws_secretsmanager_secret_versions.fcvm_fleet_stage_tls.secret_name}"', args)
        self.assertIn('"--frame-zone ${local.fcvm_fleet_names_zone}"', args)
        self.assertNotIn("--frame-zone fcvm.dev", args)
        for name in ("east2", "west2", "east1"):
            self.assertRegex(block('module "fcvm_fleet_%s"' % name),
                             r"\n  tile_tls_secret\s+= data\.aws_secretsmanager_secret_versions\.fcvm_fleet_stage_tls\.secret_name\n", name)

    def test_the_subzone_secret_is_staging_only(self):
        self.assertRegex(block('resource "aws_secretsmanager_secret" "fcvm_fleet_stage_tls"', STAGE_CERT),
                         r'\bname\s+= local\.fcvm_fleet_stage_tls_name\n')
        policy = block('resource "aws_secretsmanager_secret_policy" "fcvm_fleet_stage_tls"', STAGE_CERT)
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]', policy)
        principals = policy.split('"aws:PrincipalArn" = [', 1)[1].split("]", 1)[0]
        self.assertEqual(re.findall(r"[\w.\[\]]+(?=,)", principals),
                         ["aws_iam_role.fcvm_fleet_coordinator_task.arn", "aws_iam_role.fcvm_fleet_host.arn",
                          "aws_iam_role.fcvm_fleet_tile.arn", "local.fcvm_fleet_access_role_arn"])
        self.assertNotIn("prod", policy)
        # Empty or unverified secrets must block every consumer, including targeted template or role plans.
        guard = block('data "aws_secretsmanager_secret_versions" "fcvm_fleet_stage_tls"', STAGE_CERT)
        self.assertIn('for label in ["AWSCURRENT", "FLEET_STAGE_TLS_BOOTSTRAPPED"]', guard)
        self.assertIn('contains(version.version_stages, label)', guard)
        self.assertNotIn('aws_secretsmanager_secret.fcvm_fleet_stage_tls.', TF)

    def test_the_coordinator_may_ask_ivs_about_the_staging_channel_only(self):
        policy = block('resource "aws_iam_policy" "fcvm_fleet_coordinator_broadcast_live"')
        self.assertIn("count       = length(aws_ivs_channel.fcvm_fleet_broadcast)", policy)
        self.assertIn('Action   = "ivs:GetStream"', policy)
        self.assertIn("Resource = aws_ivs_channel.fcvm_fleet_broadcast[0].arn", policy)
        attach = block('resource "aws_iam_role_policy_attachment" "fcvm_fleet_coordinator_broadcast_live"')
        self.assertIn("role       = aws_iam_role.fcvm_fleet_coordinator_task.name", attach)
        self.assertNotIn("ivs:", block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"'))
        # The broadcast secret names the channel whose stream the metric asks about.
        self.assertIn("channel_arn      = aws_ivs_channel.fcvm_fleet_broadcast[0].arn", (ROOT / "fcvm-fleet-broadcast.tf").read_text())


def tf_code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")) + "\n"


def blocks(text):
    """Every top-level resource, data, ephemeral and module block: (kind, type, name, body)."""
    code = tf_code(text)
    found = []
    for m in re.finditer(r'^(resource|data|ephemeral) "(\w+)" "(\w+)" \{|^(module) "(\w+)" \{', code, re.M):
        start = m.start()
        body = code[start:code.index("\n}\n", start) + 2]
        if m.group(1):
            found.append((m.group(1), m.group(2), m.group(3), body))
        else:
            found.append(("module", "", m.group(5), body))
    return found


def command(text, name):
    return text.split(f"{name} = ", 1)[1].split("\n  ) : []\n", 1)[0] if name == "fcvm_fleet_prod_coordinator_command" \
        else text.split(f"{name} = concat(", 1)[1].split("\n  )\n", 1)[0]


class ProdStackTests(unittest.TestCase):
    """fcvm-fleet-prod*.tf: the production fleet (fcvm.dev) beside staging in the same account, behind
    var.fcvm_fleet_prod_enabled (ejc3/fcvm fleet production plan, A3). With the switch off nothing is created; with it on,
    every prod launch, instance and spot request is Fleet=prod and the prod coordinator acts on nothing else; and
    staging's resources render exactly as before."""

    PROD_FILES = {"fcvm-fleet-prod.tf": PROD, "fcvm-fleet-prod-dns.tf": PROD_DNS, "fcvm-fleet-prod-broadcast.tf": PROD_BC,
                  "fcvm-fleet-prod-drills.tf": (ROOT / "fcvm-fleet-prod-drills.tf").read_text()}
    GATES = (r"local\.fcvm_fleet_prod", r"2 \* local\.fcvm_fleet_prod", r"local\.fcvm_fleet_prod_broadcast",
             r"var\.fcvm_fleet_prod_enabled \? .+ : 0", r"length\(aws_iam_policy\.fcvm_fleet_prod_coordinator_java\)")

    def stack(self, name):
        return TF.split("fcvm_fleet_stacks = {", 1)[1].split("\n  }\n", 1)[0].split(f"{name} = {{", 1)[1].split("\n    }", 1)[0]

    # ------------------------------------------------------------------------------------------- nothing while off
    def test_every_prod_block_exists_only_while_the_switch_is_on(self):
        self.assertIn("default     = true", block('variable "fcvm_fleet_prod_enabled"', DEV_PROD))
        self.assertIn("fcvm_fleet_prod             = var.fcvm_fleet_prod_enabled ? 1 : 0", DEV_PROD)
        self.assertIn("fcvm_fleet_prod_broadcast = var.fcvm_fleet_prod_enabled && local.fcvm_fleet_broadcast ? 1 : 0", PROD_BC)
        seen = []
        for path, text in self.PROD_FILES.items():
            self.assertTrue(text, path)
            for kind, rtype, name, body in blocks(text):
                seen.append(rtype or kind)
                gate = re.search(r"\n  (count|for_each)\s+= (.+)\n", body)
                self.assertIsNotNone(gate, f"{path}: {kind} {rtype} {name} has no count or for_each")
                if gate.group(1) == "count":
                    self.assertTrue(any(re.fullmatch(g, gate.group(2)) for g in self.GATES), f"{name}: count = {gate.group(2)}")
                else:
                    self.assertTrue(gate.group(2).startswith("var.fcvm_fleet_prod_enabled ? "), f"{name}: {gate.group(2)}")
                    self.assertTrue(gate.group(2).endswith(" : {}"), name)
        # The things that cost money while they exist are among them.
        for billable in ("aws_lb", "aws_eip", "aws_ivs_channel", "aws_ecs_service", "aws_ecs_task_definition", "module"):
            self.assertIn(billable, seen)
        for output, text in (("fcvm_fleet_prod", PROD),):
            self.assertIn("value = var.fcvm_fleet_prod_enabled ? {", block(f'output "{output}"', text))

    def test_nothing_prod_is_created_outside_the_prod_files_while_off(self):
        # The one prod entry outside them: the stack map, whose scope policy and attachment skip prod while off.
        self.assertIn('fcvm_fleet_stacks_enabled = { for k, s in local.fcvm_fleet_stacks : k => s if k != "prod" || var.fcvm_fleet_prod_enabled }', TF)
        self.assertIn("for_each    = local.fcvm_fleet_stacks_enabled", block('resource "aws_iam_policy" "fcvm_fleet_stack_scope"'))
        self.assertIn('{ for r in aws_iam_role.fcvm_fleet_prod_coordinator_task : "prod" => r.name },', TF)
        # The prod values reach staging's resources nowhere else.
        for header in ('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"', 'resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"',
                       'resource "aws_ecs_service" "fcvm_fleet_coordinator"', 'module "fcvm_fleet_east1"'):
            self.assertNotRegex(block(header), r'fcvm_fleet_prod|stacks\.prod|"prod"|/prod/', header)
        self.assertNotRegex(command(TF, "fcvm_fleet_coordinator_command"), r'fcvm_fleet_prod|stacks\.prod|"prod"|/prod/')

    # ------------------------------------------------------------------------------------------- Fleet=prod
    def test_the_prod_stack_has_its_own_tag_names_secret_and_state_key(self):
        prod = self.stack("prod")
        self.assertRegex(prod, r'\bfleet\s+= "prod"')
        self.assertRegex(prod, r'\bprefix\s+= "fcvm-fleet-prod"')
        self.assertRegex(prod, r'\btls_secret\s+= "fcvm-fleet/prod/tls"')
        self.assertRegex(prod, r'\bstate_key\s+= "prod/state.json"')
        # A new stack has no untagged spot requests: it may cancel only requests tagged Fleet=prod.
        self.assertRegex(prod, r"\buntagged_spot_requests\s+= false")
        self.assertIn("fcvm_fleet_prod_stack  = local.fcvm_fleet_stacks.prod", PROD)
        # The coordinator role names fcvm-dev-prod.tf's Turnstile secret policy admits.
        self.assertIn('name     = "${local.fcvm_fleet_prod_coordinator}-execution"', block('resource "aws_iam_role" "fcvm_fleet_prod_coordinator_execution"', PROD))
        self.assertIn("name     = local.fcvm_fleet_prod_coordinator\n", block('resource "aws_iam_role" "fcvm_fleet_prod_coordinator_task"', PROD))

    def test_prod_templates_tag_what_they_launch_fleet_prod_in_their_own_vpc(self):
        mod = block('module "fcvm_fleet_prod_east1"', PROD)
        self.assertIn("fleet                   = local.fcvm_fleet_prod_stack.fleet", mod)
        self.assertIn("name_prefix             = local.fcvm_fleet_prod_stack.prefix", mod)
        self.assertIn('source                  = "./modules/fcvm-fleet-region"', mod)
        self.assertIn("providers               = { aws = aws.fcvm_fleet_east1 }", mod)
        self.assertIn("cidr_block              = local.fcvm_fleet_prod_cidr", mod)
        self.assertIn('fcvm_fleet_prod_cidr   = "10.23.0.0/16"', PROD)
        self.assertNotIn("10.23.", TF.split("fcvm_fleet_cidrs", 1)[1].split("\n", 1)[0])
        self.assertIn("host_profile_name         = aws_iam_instance_profile.fcvm_fleet_prod_host[0].name", mod)
        self.assertIn("tile_profile_name         = aws_iam_instance_profile.fcvm_fleet_prod_tile[0].name", mod)
        self.assertIn("tile_tls_secret = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name", mod)
        self.assertIn("alert_topic     = false", mod)
        # Its images come from prod's own pointer, which the staging publisher cannot write.
        self.assertIn("gpu_amis                = local.fcvm_fleet_prod_host_amis", mod)
        self.assertIn("tile_amis               = local.fcvm_fleet_prod_tile_amis", mod)
        self.assertIn("ami_release_guarded           = true", mod)
        self.assertIn("ami_release_parameter_version = one(ephemeral.aws_ssm_parameter.fcvm_fleet_prod_ami_release_guard[*].version)", mod)
        self.assertIn('name        = "/fcvm-fleet/prod/ami-release"', block('resource "aws_ssm_parameter" "fcvm_fleet_prod_ami_release"', PROD))
        release = (ROOT / "fcvm-fleet-ami-release.tf").read_text()
        self.assertIn("fcvm_ami_templates    = module.fcvm_fleet_east1.image_templates", release)
        for name in ("fcvm-fleet-ami-pipeline.tf", "fcvm-fleet-ami-release.tf"):
            self.assertNotIn("fcvm_fleet_prod", (ROOT / name).read_text(), name)

    def test_the_prod_coordinator_launches_only_through_prod_templates_and_passes_only_prod_roles(self):
        launch = block('resource "aws_iam_policy" "fcvm_fleet_prod_coordinator_launch"', PROD)
        images = block('resource "aws_iam_policy" "fcvm_fleet_prod_validated_images"', PROD)
        task = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_coordinator_task"', PROD)
        for pol in (launch, images, task):
            # Never staging's templates, roles or launch resources.
            self.assertNotRegex(pol, r"local\.fcvm_fleet_(tile|host)_(template_condition|referenced_arns|created_arns)")
            self.assertNotIn("aws_iam_role.fcvm_fleet_host", pol)
            self.assertNotIn("aws_iam_role.fcvm_fleet_tile", pol)
            self.assertNotIn("local.fcvm_fleet_regions", pol)
        for cond in re.findall(r'"ec2:LaunchTemplate" = (\S+) \}', launch + images):
            self.assertIn(cond, ("local.fcvm_fleet_prod_tile_template_condition", "local.fcvm_fleet_prod_host_template_condition"))
        self.assertIn("Resource  = [aws_iam_role.fcvm_fleet_prod_host[0].arn, aws_iam_role.fcvm_fleet_prod_tile[0].arn]", task)
        # The same statements as staging's launch policy: a staging change to one shows up here.
        staging = block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"')
        sids = lambda text: set(re.findall(r'Sid\s+= "(\w+)"', text))
        prod_only = {"StateObjectEarlierVersions", "StateObjectVersionList", "PassTheProdHostAndTileRolesOnly",
                     "AskWhetherTheProdBroadcastIsLive"}
        self.assertEqual(sids(launch) | sids(task) - prod_only,
                         sids(staging) - {"PassTheHostAndTileRolesOnly"})
        # The prod scope policy (Fleet=prod) is attached to the prod task role.
        self.assertIn("aws_iam_role_policy_attachment.fcvm_fleet_stack_scope", block('resource "aws_ecs_service" "fcvm_fleet_prod_coordinator"', PROD))

    def test_prod_instance_roles_read_only_prod_secrets(self):
        host = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_host"', PROD)
        self.assertIn("Resource = [aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn, aws_secretsmanager_secret.fcvm_fleet_prod_agent_access[0].arn]", host)
        tile = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_tile"', PROD)
        self.assertIn("Resource = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn", tile)
        for pol in (host, tile):
            self.assertNotIn("local.fcvm_fleet_secret_arn", pol)
        # And no other identity reads or writes the prod certificate, whatever its own policy says.
        sp = block('resource "aws_secretsmanager_secret_policy" "fcvm_fleet_prod_tls"', PROD)
        self.assertIn('Effect    = "Deny"', sp)
        self.assertIn('Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]', sp)
        self.assertEqual(re.findall(r"^\s+(aws_iam_role\.\w+\[0\]\.arn|local\.\w+),$", sp, re.M),
                         ["aws_iam_role.fcvm_fleet_prod_coordinator_task[0].arn", "aws_iam_role.fcvm_fleet_prod_host[0].arn",
                          "aws_iam_role.fcvm_fleet_prod_tile[0].arn", "local.fcvm_fleet_access_role_arn"])
        self.assertNotIn('"Allow"', sp)

    def test_the_prod_state_object_is_its_own_key_with_its_versions(self):
        task = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_coordinator_task"', PROD)
        key = '"${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${local.fcvm_fleet_prod_stack.state_key}"'
        self.assertEqual(task.count(key), 2)
        self.assertIn('Action   = ["s3:GetObject", "s3:PutObject"]', task)
        self.assertIn('Action   = "s3:GetObjectVersion"', task)
        self.assertIn('Condition = { StringEquals = { "s3:prefix" = local.fcvm_fleet_prod_stack.state_key } }', task)
        self.assertNotIn("state.json", task)
        self.assertIn('{ name = "FLEET_STATE_KEY", value = local.fcvm_fleet_prod_stack.state_key }',
                      block('resource "aws_ecs_task_definition" "fcvm_fleet_prod_coordinator"', PROD))

    # ------------------------------------------------------------------------------------------- the coordinator
    def test_the_prod_coordinator_arguments(self):
        cmd = command(PROD, "fcvm_fleet_prod_coordinator_command")
        self.assertIn('["--fleet-name", local.fcvm_fleet_prod_stack.fleet]', cmd)
        self.assertIn('"--allowed-origin", local.fcvm_fleet_prod_site_origin', cmd)
        self.assertIn('fcvm_fleet_prod_site_origin = "https://fcvm.dev"', PROD)
        # The page connects straight to the coordinator: CF-Connecting-IP is the viewer.
        self.assertIn('"--trust-cf-connecting-ip"', cmd)
        self.assertNotIn("--access-viewer-address", cmd)
        # No Cloudflare token for fcvm.dev anywhere in the prod task: the certificate is the subzone's only.
        self.assertNotIn("--dns-token-secret", cmd)
        self.assertNotIn("--dns-zone-id", cmd)
        self.assertNotIn("fcvm_fleet_dns_token", PROD)
        self.assertIn('"--tls-secret", aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name', cmd)
        self.assertIn('["--dns-subzone", local.fcvm_fleet_prod_names_zone, "--dns-subzone-ns", join(",", local.fcvm_fleet_prod_names_ns)', cmd)
        self.assertIn('["--daily-spend-usd", tostring(local.fcvm_fleet_prod_daily_spend_usd)]', cmd)
        self.assertIn("fcvm_fleet_prod_daily_spend_usd = 1", PROD)  # budget-trip drill (#942); restored to 750 next
        # ejc3/fcvm#1973 (src/cli/args.rs, src/fleet/coord/cert.rs): two awake hosts, one per zone, and the subzone
        # certificate, which refuses --dns-token-secret. --awake-capacity is gone from fcvm, so no file may pass it.
        self.assertIn('["--awake-hosts", tostring(local.fcvm_fleet_prod_awake_hosts), "--host-spread", "az"]', cmd)
        self.assertRegex(PROD, r"\n  fcvm_fleet_prod_awake_hosts\s+= 2\n")
        self.assertIn('"--tls-secret", aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name, "--tls-scope", "subzone"', cmd)
        for path in ROOT.glob("*.tf"):
            self.assertNotIn("awake-capacity", path.read_text(), path.name)
            self.assertNotIn("FLEET_AWAKE_HOSTS", path.read_text(), path.name)
        # No _acme-challenge record anywhere for prod: its challenge is answered by its own subzone server.
        for text in (PROD, PROD_DNS, DEV_PROD):
            self.assertNotRegex(tf_code(text), r"_acme-challenge")
        self.assertNotIn("--warm-hours", cmd)
        self.assertIn('"--broadcast-secret", aws_secretsmanager_secret.fcvm_fleet_prod_broadcast[0].name', cmd)
        for template in ("tile", "tile-on-demand", "tile-pool"):
            self.assertIn('"${local.fcvm_fleet_prod_stack.prefix}-%s-x86_64"' % template, cmd)

    def test_the_prod_coordinator_verifies_the_access_tokens_prods_tunnel_admits(self):
        cmd = command(PROD, "fcvm_fleet_prod_coordinator_command")
        app = "cloudflare_zero_trust_access_application"
        self.assertIn('"--access-team-domain", trimprefix(local.browser_manager_issuer, "https://")', cmd)
        live = re.findall(r'"--access-aud", (\S+?\.aud)\b', cmd)
        agent = re.findall(r'"--agent-access-aud", (\S+?\.aud)\b', cmd)
        self.assertEqual(live, [f"{app}.fcvm_dev_ops_prod[0].aud"])
        self.assertEqual(agent, [f"{app}.fcvm_dev_agent_prod[0].aud"])
        tunnel = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fcvm_fleet_prod_coordinator"', DEV_PROD)
        admitted = re.findall(r"aud_tag\s*=\s*\[(\S+\.aud)\]\n", tunnel)
        self.assertEqual(len(admitted), tunnel.count("aud_tag"))
        self.assertEqual(sorted(admitted), sorted(live + agent))

    def test_turnstile_reaches_the_coordinator_through_the_execution_role(self):
        td = block('resource "aws_ecs_task_definition" "fcvm_fleet_prod_coordinator"', PROD)
        for name in ("FLEET_TURNSTILE_SECRET", "FLEET_TURNSTILE_SITE_KEY"):
            self.assertIn('{ name = "%s", valueFrom = "${aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].arn}:%s::" }' % (name, name), td)
        ex = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_coordinator_execution_secret"', PROD)
        self.assertIn("aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].arn", ex)
        self.assertIn("aws_secretsmanager_secret.fcvm_fleet_prod_coordinator_tunnel[0].arn", ex)
        self.assertIn('"${aws_secretsmanager_secret.fcvm_fleet_prod_coordinator_tunnel[0].arn}:TUNNEL_TOKEN::"', td)
        self.assertIn("execution_role_arn       = aws_iam_role.fcvm_fleet_prod_coordinator_execution[0].arn", td)

    def test_the_prod_hosts_have_three_zones_to_spread_over(self):
        td = block('resource "aws_ecs_task_definition" "fcvm_fleet_prod_coordinator"', PROD)
        self.assertIn("condition     = length(local.fcvm_fleet_prod_host_subnets) >= local.fcvm_fleet_prod_min_host_zones", td)
        self.assertRegex(PROD, r"\n  fcvm_fleet_prod_min_host_zones\s+= 3\n")
        self.assertIn('{ name = "FLEET_HOST_SUBNETS", value = join(",", local.fcvm_fleet_prod_host_subnets) }', td)
        # One host subnet per zone that offers a host type, from a subnet in every zone of the prod VPC.
        self.assertIn("host_subnets = [for az in local.host_azs : aws_subnet.public[az].id if contains(local.azs, az)]", MOD)
        self.assertIn("for_each                                       = { for i, az in local.azs : az => i }",
                      block('resource "aws_subnet" "public"', MOD))

    def test_the_prod_service_is_one_task_replaced_in_place_from_its_own_pointer(self):
        svc = block('resource "aws_ecs_service" "fcvm_fleet_prod_coordinator"', PROD)
        self.assertIn("desired_count                      = 1", svc)
        self.assertIn("deployment_minimum_healthy_percent = 0", svc)
        self.assertIn("deployment_maximum_percent         = 100", svc)
        self.assertIn("cluster         = aws_ecs_cluster.fcvm_fleet_prod[0].id", svc)
        self.assertIn("target_group_arn = aws_lb_target_group.fcvm_fleet_prod_dns[0].arn", svc)
        self.assertIn("subnets          = local.fcvm_fleet_prod_dns_subnets", svc)
        self.assertRegex(svc, r"depends_on = \[[^\]]*aws_lb_listener\.fcvm_fleet_prod_dns")
        self.assertRegex(svc, r"depends_on = \[[^\]]*aws_iam_role_policy_attachment\.fcvm_fleet_prod_coordinator_execution,")
        self.assertRegex(svc, r"lifecycle\s*\{\s*prevent_destroy\s*=\s*true\s*\}")
        ptr = block('resource "aws_ssm_parameter" "fcvm_fleet_prod_coordinator_release"', PROD)
        self.assertIn('name     = "/fcvm-fleet/prod/coordinator-image"', ptr)
        self.assertIn("ignore_changes = [value]", ptr)
        td = block('resource "aws_ecs_task_definition" "fcvm_fleet_prod_coordinator"', PROD)
        self.assertIn("image                  = nonsensitive(aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].value)", td)
        self.assertIn("@sha256:[0-9a-f]{64}$", td)
        self.assertIn("depends_on               = [ephemeral.aws_ssm_parameter.fcvm_fleet_prod_coordinator_release_guard]", td)

    def test_the_prod_agents_differ_from_stagings_only_in_their_stack(self):
        stage = re.findall(r'^    "(--[^"]+)",$', TF.split("fcvm_fleet_host_agent_args = join(", 1)[1].split("\n  ] : []))", 1)[0], re.M)
        prod = re.findall(r'^    "(--[^"]+)",$', PROD.split("fcvm_fleet_prod_host_agent_args = ", 1)[1].split("\n  ] : [])) : \"\"", 1)[0], re.M)
        flag = lambda a: a.split(" ", 1)[0]
        self.assertEqual([flag(a) for a in stage], [flag(a) for a in prod])
        differ = {flag(s): p for s, p in zip(stage, prod) if s != p}
        self.assertEqual(differ, {
            "--dial": "--dial wss://${local.fcvm_dev_agent_prod_path}",
            "--access-secret": "--access-secret ${aws_secretsmanager_secret.fcvm_fleet_prod_agent_access[0].name}",
            "--tls-secret": "--tls-secret ${aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name}",
            "--frame-zone": "--frame-zone ${local.fcvm_fleet_prod_names_zone}",
        })
        self.assertIn('fcvm_dev_agent_prod_path    = "${local.fcvm_dev_api_prod_hostname}/agent"', DEV_PROD)

    def test_all_modes_are_the_same_as_stagings(self):
        stage, prod = tf_code(block('module "fcvm_fleet_east1"')), tf_code(block('module "fcvm_fleet_prod_east1"', PROD))
        for arg in ("potato_host_amis", "windows_host_amis", "minecraft_host_amis", "minecraft_java_access",
                    "slot_tls_ports", "windows_count", "minecraft_count", "hosts_enabled"):
            pick = lambda text: re.sub(r"\s+", " ", re.search(r"\n  %s\s+= (.*?)\n  [a-z_]+\s+=" % arg, text, re.S).group(1))
            self.assertEqual(pick(stage), pick(prod), arg)

    # ------------------------------------------------------------------------------------------- DNS and broadcast
    def test_the_prod_subzone_is_tiles_fcvm_dev_with_its_own_addresses(self):
        self.assertIn('fcvm_fleet_prod_names_zone = "tiles.fcvm.dev"', PROD_DNS)
        self.assertIn('fcvm_fleet_prod_names_ns   = ["ns1-tiles.fcvm.dev", "ns2-tiles.fcvm.dev"]', PROD_DNS)
        lb = block('resource "aws_lb" "fcvm_fleet_prod_dns"', PROD_DNS)
        self.assertIn("allocation_id = aws_eip.fcvm_fleet_prod_dns[subnet_mapping.key].id", lb)
        self.assertIn("enable_cross_zone_load_balancing = true", lb)
        self.assertIn('protocol          = "TCP_UDP"', block('resource "aws_lb_listener" "fcvm_fleet_prod_dns"', PROD_DNS))
        self.assertIn('preserve_client_ip   = "true"', block('resource "aws_lb_target_group" "fcvm_fleet_prod_dns"', PROD_DNS))
        ns = block('resource "cloudflare_dns_record" "fcvm_fleet_prod_names_delegation"', PROD_DNS)
        self.assertIn('type       = "NS"', ns)
        # Cloudflare rejects DNS record comments longer than 100 characters on this zone.
        comment = re.search(r'^  comment\s*= "([^"\n]*)"$', ns, re.M).group(1)
        self.assertLessEqual(len(comment), 100)
        self.assertIn("name       = local.fcvm_fleet_prod_names_zone", ns)
        addr = block('resource "cloudflare_dns_record" "fcvm_fleet_prod_names_ns_address"', PROD_DNS)
        self.assertIn("content = aws_eip.fcvm_fleet_prod_dns[count.index].public_ip", addr)
        self.assertIn("proxied = false", addr)
        self.assertNotRegex(PROD_DNS, r'type\s*=\s*"(DS|AAAA)"')
        # The page (ejc3/fcvm fleet/web, env.production) expects this subzone and fleet-api.fcvm.dev, which
        # fcvm-dev-prod.tf routes through prod's tunnel.
        self.assertIn('fcvm_dev_api_prod_hostname  = "fleet-api.fcvm.dev"', DEV_PROD)
        # Staging's subzone and addresses are unchanged.
        self.assertIn('fcvm_fleet_names_zone = "tiles-stage.fcvm.dev"', DNS)
        self.assertIn("count    = 2\n", block('resource "aws_eip" "fcvm_fleet_dns"', DNS))

    def test_the_prod_broadcast_is_its_own_channel_key_pair_and_secret(self):
        self.assertIn('name         = "${local.fcvm_fleet_prod_stack.prefix}-broadcast"', block('resource "aws_ivs_channel" "fcvm_fleet_prod_broadcast"', PROD_BC))
        self.assertIn("public_key = tls_private_key.fcvm_fleet_prod_broadcast_playback[0].public_key_pem",
                      block('resource "aws_ivs_playback_key_pair" "fcvm_fleet_prod_broadcast"', PROD_BC))
        self.assertIn('name                    = "fcvm-fleet/prod/broadcast"', block('resource "aws_secretsmanager_secret" "fcvm_fleet_prod_broadcast"', PROD_BC))
        self.assertNotIn("aws_ivs_channel.fcvm_fleet_broadcast", PROD_BC)
        task = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_coordinator_task"', PROD)
        self.assertIn("aws_secretsmanager_secret.fcvm_fleet_prod_broadcast[*].arn", task)
        # BroadcastLive (ejc3/fcvm#1974): IVS GetStream on the prod channel, and nothing else of IVS.
        live = task.split('[for channel in aws_ivs_channel.fcvm_fleet_prod_broadcast : {', 1)[1].split("}])", 1)[0]
        self.assertRegex(live, r'Action\s+= "ivs:GetStream"\n')
        self.assertRegex(live, r"Resource\s+= channel\.arn\n")
        self.assertEqual(re.findall(r'"ivs:[^"]*"', tf_code(PROD)), ['"ivs:GetStream"'])
        self.assertNotIn("ivs:", tf_code(PROD_BC))
        # Staging's GetStream is its own policy, on its own channel (StageSubzoneCertificateTests).
        self.assertEqual(re.findall(r'"ivs:[^"]*"', tf_code(TF)), ['"ivs:GetStream"'])

    # ------------------------------------------------------------------------------------------- staging unchanged
    def test_the_region_module_renders_stagings_names_as_before(self):
        # Staging's three regions pass the prefix fcvm-fleet; with it, every network name is the literal it was.
        staged = MOD.replace("${var.name_prefix}", "fcvm-fleet").replace("= var.name_prefix\n", '= "fcvm-fleet"\n')
        for line in ('  tags                             = { Name = "fcvm-fleet-${local.region}" }',
                     '  tags                                           = { Name = "fcvm-fleet-${each.key}" }',
                     '  name        = "fcvm-fleet"', '  name        = "fcvm-fleet-minecraft-java"',
                     '  tags                     = { Name = "fcvm-fleet-${local.region}-flow-log", BucketPolicy = var.flow_log_bucket_policy_id }',
                     '  name  = "fcvm-fleet-alerts"'):
            self.assertIn(line + "\n", staged)
        self.assertNotIn('"fcvm-fleet-${local.region}"', MOD)
        # Staging keeps its default alert topic. All regions use the staging TLS secret; only east1 guards AMI releases.
        self.assertRegex(block('variable "alert_topic"', MOD), r"default\s+= true")
        self.assertRegex(block('variable "tile_tls_secret"', MOD), r'default\s+= ""')
        self.assertRegex(block('variable "ami_release_guarded"', MOD), r"default\s+= false")
        for name in ("east2", "west2", "east1"):
            mod = block('module "fcvm_fleet_%s"' % name)
            self.assertNotIn("alert_topic", mod)
            self.assertIn("tile_tls_secret", mod)
            self.assertEqual("ami_release_guarded" in mod, name == "east1")
        self.assertIn("count = var.alert_topic && length(local.host_archs) > 0 ? 1 : 0", MOD)
        # Nothing is moved: no staging address changes.
        for text in (TF, MOD, PROD, PROD_DNS, PROD_BC):
            self.assertNotRegex(text, r"(?m)^\s*moved \{")

    def test_a_tile_without_its_own_secret_boots_with_the_same_user_data(self):
        tile = MOD.split("  tile_user_data = base64encode(<<-EOT\n", 1)[1].split("  EOT\n", 1)[0]
        cond = tile[tile.index('    %{if var.tile_tls_secret != ""~}\n'):tile.index("    %{endif~}\n") + len("    %{endif~}\n")]
        # With the condition false the template renders what precedes it (verified byte for byte with terraform 1.10.3).
        self.assertEqual(tile.replace(cond, ""), "    #!/bin/bash\n    set -eu\n    ${var.ip_options_bootstrap}\n")
        self.assertIn("Environment=FLEET_TLS_SECRET=%s", cond)
        self.assertIn("'${var.tile_tls_secret}'", cond)
        self.assertIn("systemctl restart --no-block fcvm-fleet-tls.service", cond)
        self.assertIn("ExecStartPost=/usr/bin/systemctl --no-block start fcvm-fleet-nginx.service", cond)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
