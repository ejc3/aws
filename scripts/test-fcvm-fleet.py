#!/usr/bin/env python3
"""Offline tests for fcvm-fleet.tf and modules/fcvm-fleet-region (ejc3/aws#606): the fleet's network admits TCP 443 only,
the GPU hosts and the coordinator stay off until their gates and inputs exist, and the coordinator's role reaches EC2 tiles
only."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-fleet.tf").read_text()
MOD = (ROOT / "modules" / "fcvm-fleet-region" / "main.tf").read_text()


def block(header, text=TF):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


class NetworkTests(unittest.TestCase):
    def test_the_security_group_admits_tcp_443_and_nothing_else(self):
        sg = block('resource "aws_security_group" "fleet"', MOD)
        self.assertEqual(sg.count("ingress {"), 1)
        self.assertIn("from_port        = 443", sg)
        self.assertIn("to_port          = 443", sg)
        self.assertIn('protocol         = "tcp"', sg)
        self.assertNotIn("22", sg)
        coord = block('resource "aws_security_group" "fcvm_fleet_coordinator"')
        self.assertNotIn("ingress", coord)

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


class CoordinatorTests(unittest.TestCase):
    def test_the_service_runs_the_tagged_image_from_the_fleet_repository(self):
        self.assertIn("fcvm_fleet_coordinator_enabled = true", TF)
        self.assertRegex(TF, r'fcvm_fleet_coordinator_tag = "[0-9a-f]{8,40}"')
        self.assertIn('fcvm_fleet_coordinator_image   = var.fcvm_fleet_coordinator_image != "" ? var.fcvm_fleet_coordinator_image : "${aws_ecr_repository.fcvm_fleet_coordinator.repository_url}:${local.fcvm_fleet_coordinator_tag}"', TF)
        self.assertIn('fcvm_fleet_coordinator_running = local.fcvm_fleet_coordinator_enabled && local.fcvm_fleet_coordinator_tag != ""', TF)
        self.assertIn("image     = local.fcvm_fleet_coordinator_running ? local.fcvm_fleet_coordinator_image", TF)
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
        self.assertIn('Action   = "autoscaling:SetDesiredCapacity"', s)
        self.assertIn(":autoScalingGroup:*:autoScalingGroupName/fcvm-fleet-hosts", s)
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


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
