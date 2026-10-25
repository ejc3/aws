#!/usr/bin/env python3
"""Offline tests for fcvm-fleet.tf and modules/fcvm-fleet-region (ejc3/aws#606): the fleet's network admits TCP 443 only,
the GPU hosts and the coordinator stay off until their gates and inputs exist, and the coordinator's role reaches EC2 tiles
only."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-fleet.tf").read_text()
DNS = (ROOT / "fcvm-fleet-dns.tf").read_text()
MOD = (ROOT / "modules" / "fcvm-fleet-region" / "main.tf").read_text()


def block(header, text=TF):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


class NetworkTests(unittest.TestCase):
    def test_the_security_group_admits_tcp_443_and_the_slot_ports_and_nothing_else(self):
        # 443 from anywhere, and the slots' IPv4 TLS ports (SlotV4Tests pins that rule).
        sg = block('resource "aws_security_group" "fleet"', MOD)
        self.assertEqual(sg.count("ingress {"), 2)
        self.assertEqual(sg.count('protocol    = "tcp"') + sg.count('protocol         = "tcp"'), 2)
        self.assertIn("from_port        = 443", sg)
        self.assertIn("to_port          = 443", sg)
        self.assertIn('protocol         = "tcp"', sg)
        self.assertNotIn("22", sg)
        # The coordinator admits its DNS port and nothing else (DnsTests).
        coord = block('resource "aws_security_group" "fcvm_fleet_coordinator"')
        rules = re.findall(r"  ingress \{\n(.*?)\n  \}", coord, re.S)
        self.assertEqual(len(rules), 2, rules)
        for rule in rules:
            self.assertIn("from_port   = local.fcvm_fleet_dns_port", rule)
            self.assertIn("to_port     = local.fcvm_fleet_dns_port", rule)
        self.assertEqual(sorted(re.findall(r'protocol    = "(\w+)"', coord)), ["tcp", "udp"])

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
    def test_hosts_ask_for_a_prefix_only_and_imdsv2_hop_1(self):
        lt = block('resource "aws_launch_template" "host"', MOD)
        self.assertIn("ipv6_prefix_count           = 1", lt)
        self.assertNotIn("ipv6_address_count", lt)  # EC2 refuses a prefix count and an address count together
        self.assertIn('http_tokens                 = "required"', lt)
        self.assertIn("http_put_response_hop_limit = 1", lt)

    def test_the_host_group_is_spot_price_capacity_optimized_and_gated(self):
        asg = block('resource "aws_autoscaling_group" "hosts"', MOD)
        self.assertIn("on_demand_base_capacity                  = 0", asg)
        self.assertIn("on_demand_percentage_above_base_capacity = 0", asg)
        self.assertIn('spot_allocation_strategy                 = "price-capacity-optimized"', asg)
        self.assertIn("capacity_rebalance        = true", asg)
        self.assertIn("desired_capacity          = local.host_desired", asg)
        self.assertIn("host_desired = var.hosts_enabled && length(local.host_archs) > 0 ? 1 : 0", MOD)
        self.assertIn("fcvm_fleet_hosts_enabled       = true", TF)

    def test_hosts_and_tiles_have_amis_only_where_there_is_g_spot_quota(self):
        # One g4dn.metal is the main cost and us-east-1 holds the account's only G spot quota: an AMI elsewhere would start a
        # second always-on host (or a group that can never launch).
        for var in ("fcvm_fleet_host_amis", "fcvm_fleet_tile_amis"):
            default = re.search(r"default = (\{.*\})\n", block('variable "%s"' % var)).group(1)
            self.assertRegex(default, r'^\{ "us-east-1" = \{ x86_64 = "ami-[0-9a-f]{17}" \} \}$')

    def test_only_architectures_with_an_ami_are_in_the_group(self):
        self.assertIn('host_archs = [for a in sort(keys(local.host_types)) : a if try(var.gpu_amis[a], "") != ""]', MOD)
        self.assertIn('host_types = { x86_64 = "g4dn.metal", arm64 = "g5g.metal" }', MOD)


class OnDemandFallbackTests(unittest.TestCase):
    """The spot group's on-demand fallback: a second group, 0 until the coordinator gives it a short spot group's capacity,
    capped at 8 hosts (owner, 2026-10-24: "Up to 8 on-demand max")."""

    def test_the_fallback_group_is_on_demand_capped_and_starts_at_zero(self):
        od = block('resource "aws_autoscaling_group" "hosts_on_demand"', MOD)
        self.assertIn('name                      = "fcvm-fleet-hosts-on-demand"', od)
        self.assertIn("on_demand_percentage_above_base_capacity = 100", od)
        self.assertIn("min_size                  = 0", od)
        self.assertIn("desired_capacity          = 0", od)
        self.assertIn("max_size                  = local.host_desired == 0 ? 0 : var.host_on_demand_max", od)
        self.assertIn("ignore_changes = [desired_capacity]", od)
        self.assertNotIn("capacity_rebalance", od)  # a spot-only setting
        self.assertIn("var.host_on_demand_max >= 0 && var.host_on_demand_max <= 8", block('variable "host_on_demand_max"', MOD))
        self.assertIn("fcvm_fleet_on_demand_max = 8", TF)
        self.assertEqual(TF.count("host_on_demand_max        = local.fcvm_fleet_on_demand_max"), 3)

    def test_eight_is_the_cap_for_the_whole_fleet(self):
        # The cap is per region, so it is the fleet's cap only while one region has a host AMI (and so a group).
        default = re.search(r"default = (\{.*\})\n", block('variable "fcvm_fleet_host_amis"')).group(1)
        self.assertEqual(default.count("ami-"), 1)
        self.assertIn("count = length(local.host_archs) > 0 ? 1 : 0", block('resource "aws_autoscaling_group" "hosts_on_demand"', MOD))

    def test_both_host_groups_launch_only_where_a_host_type_is_offered(self):
        for name in ('resource "aws_autoscaling_group" "hosts"', 'resource "aws_autoscaling_group" "hosts_on_demand"'):
            self.assertIn("vpc_zone_identifier       = local.host_subnets", block(name, MOD), name)
        offerings = block('data "aws_ec2_instance_type_offerings" "host"', MOD)
        self.assertIn('location_type = "availability-zone"', offerings)
        self.assertIn("values = [local.host_types[each.key]]", offerings)

    def test_the_coordinator_falls_back_only_with_an_image_that_has_the_flags(self):
        # The image at 8c96e28e has no --on-demand-group; clap refuses an unknown argument and the coordinator would not
        # start. The flag may only be turned on together with a newer tag.
        tag = re.search(r'fcvm_fleet_coordinator_tag = "([0-9a-f]+)"', TF).group(1)
        on = re.search(r"fcvm_fleet_coordinator_falls_back = (true|false)", TF).group(1)
        if tag == "8c96e28e":
            self.assertEqual(on, "false")
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn("local.fcvm_fleet_coordinator_falls_back ? concat(", cmd)
        self.assertIn('["--on-demand-group", "${r}:fcvm-fleet-hosts-on-demand"] if length(lookup(var.fcvm_fleet_host_amis, r, {})) > 0', cmd)

    def test_the_coordinator_may_scale_the_on_demand_group(self):
        tf = TF.split('Sid    = "ScaleTheHostGroups"', 1)[1].split("},", 1)[0]
        self.assertIn('for g in ["fcvm-fleet-hosts", "fcvm-fleet-hosts-on-demand"]', tf)
        self.assertIn('Action = "autoscaling:SetDesiredCapacity"', tf)

    def test_an_on_demand_host_in_service_mails_the_owner(self):
        alarm = block('resource "aws_cloudwatch_metric_alarm" "on_demand_hosts"', MOD)
        self.assertIn('metric_name         = "GroupInServiceInstances"', alarm)
        self.assertIn("AutoScalingGroupName = aws_autoscaling_group.hosts_on_demand[0].name", alarm)
        self.assertIn("threshold           = 1", alarm)
        self.assertIn("alarm_actions       = [aws_sns_topic.alerts[0].arn]", alarm)
        self.assertIn('"GroupInServiceInstances"', block('resource "aws_autoscaling_group" "hosts_on_demand"', MOD))
        sub = block('resource "aws_sns_topic_subscription" "alerts_email"', MOD)
        self.assertIn('protocol  = "email"', sub)
        self.assertEqual(TF.count("alert_email               = local.browser_manager_owner"), 3)


class CoordinatorTests(unittest.TestCase):
    def test_the_service_runs_the_tagged_image_from_the_fleet_repository(self):
        self.assertIn("fcvm_fleet_coordinator_enabled = true", TF)
        self.assertRegex(TF, r'fcvm_fleet_coordinator_tag = "[0-9a-f]{8,40}"')
        self.assertIn('track_latest = true', TF)
        self.assertNotIn('ignore_changes', block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"'))
        self.assertIn('fcvm_fleet_coordinator_running = local.fcvm_fleet_coordinator_enabled && local.fcvm_fleet_coordinator_tag != ""', TF)
        self.assertIn("image     = local.fcvm_fleet_coordinator_running ? nonsensitive(data.aws_ssm_parameter.fcvm_coordinator_release.value)", TF)
        self.assertIn("desired_count                      = local.fcvm_fleet_coordinator_running ? 1 : 0", block('resource "aws_ecs_service" "fcvm_fleet_coordinator"'))

    def test_the_task_role_reaches_tiles_only(self):
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task"')
        self.assertIn('"ec2:LaunchTemplate"', pol)
        self.assertIn('Condition = { StringNotEquals = { "aws:RequestTag/Purpose" = "fcvm-fleet-tile" } }', pol)
        self.assertIn('Condition = { StringEquals = { "ec2:CreateAction" = "RunInstances" } }', pol)
        self.assertIn('"ec2:ResourceTag/Purpose" = "fcvm-fleet-tile"', pol)
        self.assertIn("Resource  = aws_iam_role.fcvm_fleet_tile.arn", pol)
        self.assertIn('"iam:PassedToService" = "ec2.amazonaws.com"', pol)
        self.assertNotRegex(pol, r'Action\s*=\s*"(ec2|iam|s3):\*"')

    def test_cloudflared_is_pinned_and_takes_its_token_from_secrets_manager(self):
        td = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertRegex(td, r'image\s+= "cloudflare/cloudflared:\$\{local\.cloudflared_version\}"')
        self.assertRegex(td, r'secrets\s+= \[\{ name = "TUNNEL_TOKEN", valueFrom = local\.fcvm_fleet_tunnel_token_ref \}\]')
        self.assertEqual(len(re.findall(r"readonlyRootFilesystem\s*=\s*true", td)), 2)
        self.assertNotIn(":latest", td)


class SecretTests(unittest.TestCase):
    def test_only_the_certificate_container_is_created_here(self):
        names = re.findall(r'resource "aws_secretsmanager_secret" "[^"]+" \{[^}]*?\bname\s+= "([^"]+)"', TF, re.S)
        self.assertEqual(names, ["fcvm-fleet/tls-wildcard"])
        self.assertNotIn("aws_secretsmanager_secret_version", TF)

    def test_hosts_and_tiles_read_only_what_they_need(self):
        host = block('resource "aws_iam_role_policy" "fcvm_fleet_host"')
        self.assertIn('Resource = [local.fcvm_fleet_secret_arn["tls-wildcard"], local.fcvm_fleet_secret_arn["agent-access-token"]]', host)
        self.assertNotIn("ec2:", host)
        tile = block('resource "aws_iam_role_policy" "fcvm_fleet_tile"')
        self.assertIn('Resource = local.fcvm_fleet_secret_arn["tls-wildcard"]', tile)





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
        self.assertIn('"instance", "volume", "network-interface", "spot-instances-request"', self.TF)

    def test_state_object_secrets_and_spot_role(self):
        self.assertIn('Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/state.json"', self.TF)
        secrets = self.stmt("FleetSecrets")
        self.assertIn('cloudflare-dns-token', secrets)
        self.assertIn('tls-wildcard', secrets)
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

    def test_the_coordinator_scales_only_the_host_groups(self):
        s = self.stmt("ScaleTheHostGroups")
        self.assertIn('Action = "autoscaling:SetDesiredCapacity"', s)
        self.assertIn(':autoScalingGroup:*:autoScalingGroupName/${g}"', s)
        self.assertIn('[for g in ["fcvm-fleet-hosts", "fcvm-fleet-hosts-on-demand"] :', s)
        self.assertNotIn('Resource = "*"', s)
        d = self.stmt("DescribeTheHostGroups")
        self.assertIn('Action   = "autoscaling:DescribeAutoScalingGroups"', d)
        self.assertNotRegex(self.TF, r'"autoscaling:\*"')
        group = block('resource "aws_autoscaling_group" "hosts"', MOD)
        self.assertIn("ignore_changes = [desired_capacity]", group)
        self.assertIn("max_size                  = local.host_desired == 0 ? 0 : 2", group)

    def test_an_on_demand_tile_template_is_a_launchable_template(self):
        od = block('resource "aws_launch_template" "tile_on_demand"', MOD)
        spot = block('resource "aws_launch_template" "tile"', MOD)
        self.assertNotIn("instance_market_options", od)
        self.assertIn("instance_market_options", spot)
        self.assertIn('name          = "fcvm-fleet-tile-on-demand-${each.key}"', od)
        self.assertIn('tags          = { Name = "fcvm-fleet-tile", Purpose = "fcvm-fleet-tile" }', od)
        for part in ("iam_instance_profile", "http_tokens                 = \"required\"", "ipv6_address_count          = 1"):
            self.assertIn(part, od, part)
        self.assertIn("[for t in aws_launch_template.tile_on_demand : t.arn]", block('output "tile_template_arns"', MOD))


class ModuleDependencies(unittest.TestCase):
    def test_no_module_level_depends_on(self):
        # A module-level depends_on defers the module's data sources (the AZ list) to apply time, and the subnets'
        # for_each over them then fails to plan; the flow log waits for the bucket policy through an input instead.
        tf = (ROOT / "fcvm-fleet.tf").read_text()
        for name in ("east2", "west2", "east1"):
            start = tf.index('module "fcvm_fleet_%s"' % name)
            body = tf[start:tf.index("\n}\n", start)]
            self.assertNotIn("depends_on", body, name)
            self.assertIn("flow_log_bucket_policy_id = aws_s3_bucket_policy.fcvm_fleet_flow_logs.id", body, name)

class CoordinatorTileTypes(unittest.TestCase):
    def test_the_coordinator_launches_only_the_two_tile_types(self):
        tf = (ROOT / "fcvm-fleet.tf").read_text()
        s = tf.split('Sid       = "TileTypesOnly"', 1)[1].split("},", 1)[0]
        self.assertIn('Effect    = "Deny"', s)
        self.assertIn('Action    = "ec2:RunInstances"', s)
        self.assertIn('"ec2:InstanceType" = ["g4dn.xlarge", "g5g.xlarge"]', s)

class RegistryTests(unittest.TestCase):
    def test_the_bootstrap_image_is_protected_from_the_rolling_retention_rule(self):
        policy = block('resource "aws_ecr_lifecycle_policy" "fcvm_fleet_coordinator"')
        self.assertIn('tagPatternList = [local.fcvm_fleet_coordinator_tag]', policy)
        protected, rolling = policy.split('description  = "Keep the 20 newest images"', 1)
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
                  '"--tls-secret ${aws_secretsmanager_secret.fcvm_fleet_tls.name}"',
                  '"--secret-region ${local.fcvm_fleet_home_region}"',
                  '"--host-from-imds"', '"--ipv6-prefix-from-imds"', '"--frame-zone fcvm.dev"']:
            self.assertIn(a, args)
        self.assertEqual(TF.count("host_agent_args           = local.fcvm_fleet_host_agent_args"), 3)

    def test_boot_writes_the_agent_env_and_starts_the_unit_only_with_arguments(self):
        self.assertIn('%{if var.host_agent_args != ""~}', MOD)
        self.assertIn("> /etc/fcvm-fleet/agent.env", MOD)
        self.assertIn("systemctl enable fcvm-fleet-agent.service", MOD)
        # Not a blocking start from cloud-init: the unit waits for the GPUs and the store.
        self.assertIn("systemctl start --no-block fcvm-fleet-agent.service", MOD)
        self.assertNotIn("systemctl enable --now fcvm-fleet-agent", MOD)

    def test_the_coordinator_scales_the_host_groups_it_has_an_ami_for(self):
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('["--scale-group", "${r}:fcvm-fleet-hosts"] if length(lookup(var.fcvm_fleet_host_amis, r, {})) > 0', cmd)
        self.assertIn('"--allowed-origin", "https://${local.fcvm_dev_site_stage_host}"', cmd)
        self.assertIn('"--ec2-regions", "us-east-1"', cmd)
        self.assertIn('"--dns-token-secret", aws_secretsmanager_secret.fcvm_fleet_dns_token.name', cmd)
        task = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertIn("command   = local.fcvm_fleet_coordinator_running ? local.fcvm_fleet_coordinator_command : null", task)


class TileSubnetTests(unittest.TestCase):
    def test_both_tile_templates_name_a_fleet_subnet(self):
        # Without one, a launch from the template lands in the default VPC's subnet, which the coordinator may not use.
        for name in ('resource "aws_launch_template" "tile"', 'resource "aws_launch_template" "tile_on_demand"'):
            self.assertIn("subnet_id                   = aws_subnet.public[local.tile_az].id", block(name, MOD))
        self.assertIn("tile_az = local.azs[0]", MOD)


class SlotV4Tests(unittest.TestCase):
    """IPv4 viewers reach a GPU host's slots on its one public address, a port per slot."""

    def test_the_slot_ports_are_admitted_over_ipv4_and_nothing_wider(self):
        base = int(re.search(r"fcvm_fleet_slot_tls_port_base = (\d+)", TF).group(1))
        slots = int(re.search(r"fcvm_fleet_host_gpu_slots\s+= (\d+)", TF).group(1))
        self.assertEqual((base, slots), (20200, 8))
        self.assertIn("from = local.fcvm_fleet_slot_tls_port_base", TF)
        self.assertIn("to   = local.fcvm_fleet_slot_tls_port_base + local.fcvm_fleet_host_gpu_slots - 1", TF)
        # The agent forwards from the same base.
        self.assertIn('"--tls-publish-base ${local.fcvm_fleet_slot_tls_port_base}"', TF)
        self.assertIn('"--public-ipv4-from-imds"', TF)
        sg = block('resource "aws_security_group" "fleet"', MOD)
        rules = re.findall(r"  ingress \{\n(.*?)\n  \}", sg, re.S)
        self.assertEqual(len(rules), 2, rules)
        slot = [r for r in rules if "var.slot_tls_ports" in r]
        self.assertEqual(len(slot), 1, rules)
        self.assertIn("from_port   = var.slot_tls_ports.from", slot[0])
        self.assertIn("to_port     = var.slot_tls_ports.to", slot[0])
        self.assertIn('cidr_blocks = ["0.0.0.0/0"]', slot[0])
        self.assertNotIn("ipv6_cidr_blocks", slot[0])
        self.assertEqual(TF.count("slot_tls_ports            = local.fcvm_fleet_slot_tls_ports"), 3)

    def test_the_coordinator_serves_the_launch_names_subzone(self):
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertNotIn("--dns-nameservers", cmd)
        self.assertIn('["--dns-subzone", local.fcvm_fleet_names_zone, "--dns-subzone-ns", join(",", local.fcvm_fleet_names_ns), '
                      '"--dns-listen", "0.0.0.0:${local.fcvm_fleet_dns_port}"]', cmd)
        # The certificate renewal still needs the zone's DNS token.
        self.assertIn('"--tls-secret", aws_secretsmanager_secret.fcvm_fleet_tls.name', cmd)
        self.assertIn('"--dns-token-secret", aws_secretsmanager_secret.fcvm_fleet_dns_token.name', cmd)


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

    def test_the_delegation_is_ns_records_with_unproxied_addresses_and_no_ds(self):
        addr = block('resource "cloudflare_dns_record" "fcvm_fleet_names_ns_address"', DNS)
        self.assertIn('type    = "A"', addr)
        self.assertIn("content = aws_eip.fcvm_fleet_dns[count.index].public_ip", addr)
        self.assertIn("proxied = false", addr)
        ns = block('resource "cloudflare_dns_record" "fcvm_fleet_names_delegation"', DNS)
        self.assertIn('type       = "NS"', ns)
        self.assertIn("name       = local.fcvm_fleet_names_zone", ns)
        self.assertNotRegex(DNS, r'type\s*=\s*"(DS|AAAA)"')


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
