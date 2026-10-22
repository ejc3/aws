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
            self.assertIn("providers            = { aws = aws.%s }" % alias, block('module "fcvm_fleet_%s"' % name))


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
        self.assertIn("fcvm_fleet_hosts_enabled       = false", TF)

    def test_only_architectures_with_an_ami_are_in_the_group(self):
        self.assertIn('host_archs = [for a in sort(keys(local.host_types)) : a if try(var.gpu_amis[a], "") != ""]', MOD)
        self.assertIn('host_types = { x86_64 = "g4dn.metal", arm64 = "g5g.metal" }', MOD)


class CoordinatorTests(unittest.TestCase):
    def test_the_service_is_off_until_enabled_and_given_an_image(self):
        self.assertIn("fcvm_fleet_coordinator_enabled = false", TF)
        self.assertIn('fcvm_fleet_coordinator_running = local.fcvm_fleet_coordinator_enabled && var.fcvm_fleet_coordinator_image != ""', TF)
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
        names = re.findall(r'name\s+= "(fcvm-fleet/[^"]+)"', TF)
        self.assertEqual(names, ["fcvm-fleet/tls-wildcard"])
        self.assertNotIn("aws_secretsmanager_secret_version", TF)

    def test_hosts_and_tiles_read_only_what_they_need(self):
        host = block('resource "aws_iam_role_policy" "fcvm_fleet_host"')
        self.assertIn('Resource = [local.fcvm_fleet_secret_arn["tls-wildcard"], local.fcvm_fleet_secret_arn["agent-access-token"]]', host)
        self.assertNotIn("ec2:", host)
        tile = block('resource "aws_iam_role_policy" "fcvm_fleet_tile"')
        self.assertIn('Resource = local.fcvm_fleet_secret_arn["tls-wildcard"]', tile)



class CoordinatorTileTypes(unittest.TestCase):
    def test_the_coordinator_launches_only_the_two_tile_types(self):
        tf = (ROOT / "fcvm-fleet.tf").read_text()
        s = tf.split('Sid       = "TileTypesOnly"', 1)[1].split("},", 1)[0]
        self.assertIn('Effect    = "Deny"', s)
        self.assertIn('Action    = "ec2:RunInstances"', s)
        self.assertIn('"ec2:InstanceType" = ["g4dn.xlarge", "g5g.xlarge"]', s)

if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
