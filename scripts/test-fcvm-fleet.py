#!/usr/bin/env python3
"""Offline tests for fcvm-fleet.tf and modules/fcvm-fleet-region (ejc3/aws#606): the fleet's network admits TCP 443 only,
the GPU hosts and the coordinator stay off until their gates and inputs exist, and the coordinator's role reaches GPU hosts
and EC2 tiles from their templates only."""
import re
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

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
    """Spot first, on demand in the same pass when spot is refused, at most 8 on-demand hosts across the fleet (owner,
    2026-10-24: "Up to 8 on-demand max"): the coordinator's cap, handed to it in its environment."""

    def test_the_on_demand_cap_is_eight_for_the_whole_fleet(self):
        self.assertIn("fcvm_fleet_on_demand_max = 8", TF)
        td = block('resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"')
        self.assertIn('{ name = "FLEET_HOST_ON_DEMAND_MAX", value = tostring(local.fcvm_fleet_on_demand_max) }', td)
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
    TOPICS = ['arn:aws:sns:us-east-1:123456789012:fcvm-fleet-alerts',
              'arn:aws:sns:us-west-2:123456789012:fcvm-fleet-alerts']

    def setUp(self):
        self.sdk = mock.Mock()
        spec = importlib.util.spec_from_file_location('fleet_notify', ROOT / 'scripts/fcvm-fleet-on-demand-notify.py')
        self.notify = importlib.util.module_from_spec(spec)
        with mock.patch.dict('sys.modules', {'boto3': self.sdk}):
            spec.loader.exec_module(self.notify)
        env = mock.patch.dict('os.environ', {'ALARM_ARN': self.ALARM, 'TOPIC_ARNS': json.dumps(self.TOPICS)})
        env.start()
        self.addCleanup(env.stop)

    def event(self, state='ALARM', alarm=None, source='aws.cloudwatch'):
        return {'source': source, 'alarmArn': alarm or self.ALARM,
                'alarmData': {'state': {'value': state, 'reason': 'accepted on-demand launch'}}}

    def test_accepted_launch_alarm_uses_each_existing_topics_regional_endpoint(self):
        self.assertEqual(self.notify.handler(self.event(), None), {'notified': 2})
        self.assertEqual(self.sdk.client.call_args_list,
                         [mock.call('sns', region_name='us-east-1'), mock.call('sns', region_name='us-west-2')])
        calls = self.sdk.client.return_value.publish.call_args_list
        self.assertEqual([c.kwargs['TopicArn'] for c in calls], self.TOPICS)
        self.assertTrue(all('not a running-host count' in c.kwargs['Message'] for c in calls))

    def test_ok_other_alarms_and_other_event_sources_do_not_send(self):
        for event in (self.event(state='OK'), self.event(alarm='unrelated'), self.event(source='unrelated'), {}):
            self.assertEqual(self.notify.handler(event, None), {'notified': 0})
        self.sdk.client.assert_not_called()

    def test_publish_failures_raise_for_lambda_asynchronous_retry(self):
        self.sdk.client.return_value.publish.side_effect = RuntimeError('publish unavailable')
        with self.assertRaisesRegex(RuntimeError, 'publish unavailable'):
            self.notify.handler(self.event(), None)


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
        self.assertIn('"instance", "volume", "network-interface"', self.TF)
        spot = self.stmt("LaunchTaggedTileSpotRequestsOnly")
        self.assertIn('Resource = local.fcvm_fleet_tile_spot_request_arns', spot)
        self.assertIn('"aws:RequestTag/Purpose" = "fcvm-fleet-tile"', spot)
        self.assertIn('"aws:RequestedRegion"', spot)
        self.assertNotIn('"ec2:LaunchTemplate"', spot)

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

    def test_the_coordinator_launches_hosts_only_from_their_templates(self):
        fleet = self.stmt("LaunchHostsAsInstantFleets")
        self.assertIn('Action    = "ec2:CreateFleet"', fleet)
        self.assertIn('"aws:RequestedRegion" = local.fcvm_fleet_regions', fleet)
        self.assertIn('Resource  = concat(local.fcvm_fleet_host_fleet_arns, local.fcvm_fleet_host_volume_arns)', fleet)
        self.assertNotIn('Resource  = "*"', fleet)
        instances = self.stmt("LaunchFleetInstancesAsTaggedHostsOnly")
        self.assertIn('Resource = local.fcvm_fleet_host_instance_arns', instances)
        self.assertIn('"aws:RequestTag/Purpose" = "fcvm-fleet-host"', instances)
        self.assertIn('"ec2:InstanceType"', instances)
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
        self.assertIn('"aws:RequestedRegion" = local.fcvm_fleet_regions', referenced)
        self.assertIn('ArnLikeIfExists = { "ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition }', referenced)
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
    def test_the_coordinator_launches_only_the_fleet_types_and_tile_templates_only_tile_types(self):
        stmt = CoordinatorLaunchSplit.stmt
        s = stmt(CoordinatorLaunchSplit, "FleetTypesOnly")
        self.assertIn('Effect    = "Deny"', s)
        self.assertIn('Action    = ["ec2:RunInstances", "ec2:CreateFleet"]', s)
        self.assertIn('"ec2:InstanceType" = concat(local.fcvm_fleet_tile_types, local.fcvm_fleet_host_types)', s)
        tagged = stmt(CoordinatorLaunchSplit, "FleetInstancesAreTagged")
        self.assertIn('Action    = ["ec2:RunInstances", "ec2:CreateFleet"]', tagged)
        t = stmt(CoordinatorLaunchSplit, "TileTemplatesLaunchTileTypesOnly")
        self.assertIn('ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_tile_template_condition }', t)
        self.assertIn('StringNotEquals = { "ec2:InstanceType" = local.fcvm_fleet_tile_types }', t)
        self.assertIn('fcvm_fleet_tile_types              = ["g4dn.xlarge", "g5g.xlarge"]', CoordinatorLaunchSplit.TF)

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

    def test_the_coordinator_sleeps_and_runs_the_ec2_side(self):
        cmd = TF.split("fcvm_fleet_coordinator_command = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertIn('["--sleep-when-idle"]', cmd)
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


class SlotV4Tests(unittest.TestCase):
    """IPv4 viewers reach a GPU host's slots on its one public address, a port per slot."""

    def test_the_slot_ports_are_admitted_over_ipv4_and_nothing_wider(self):
        base = int(re.search(r"fcvm_fleet_slot_tls_port_base = (\d+)", TF).group(1))
        slots = int(re.search(r"fcvm_fleet_host_gpu_slots\s+= (\d+)", TF).group(1))
        self.assertEqual((base, slots), (20200, 8))
        self.assertIn("from = local.fcvm_fleet_slot_tls_port_base", TF)
        # The slots' ports, and with aggregated streaming the host aggregator's port after them.
        self.assertIn(
            "to   = local.fcvm_fleet_aggregated ? local.fcvm_fleet_aggregate_tls_port : "
            "local.fcvm_fleet_slot_tls_port_base + local.fcvm_fleet_host_gpu_slots - 1", TF)
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
        self.assertIn("count    = local.fcvm_fleet_aggregated ? 1 : 0", dual)

    def test_the_host_aggregator_port_follows_the_slots(self):
        self.assertIn(
            "fcvm_fleet_aggregate_tls_port = local.fcvm_fleet_slot_tls_port_base + local.fcvm_fleet_host_gpu_slots", TF)
        priv = int(re.search(r"fcvm_fleet_aggregate_port\s+= (\d+)", TF).group(1))
        # The private listener is outside the admitted range.
        self.assertFalse(20200 <= priv <= 20208, priv)

if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
