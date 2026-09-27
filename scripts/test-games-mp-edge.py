#!/usr/bin/env python3
"""Pins the public edge of play.cc-games.app (games-multiplayer-edge.tf and friends).

play.cc-games.app is the first AWS resource outside users reach, so the protections in front
of it must not quietly regress: the WAF and its rules, access logs, router redundancy, engine
egress, the engine ceiling and the lobby's launch limits. Offline; reads the Terraform source.

Run from the repo root:  python3 -S -B scripts/test-games-mp-edge.py
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDGE = (ROOT / "games-multiplayer-edge.tf").read_text()
GAMES = (ROOT / "games-multiplayer.tf").read_text()
BRINGUP = (ROOT / "games-multiplayer-bringup.tf").read_text()


def block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (re.escape(kind), re.escape(name)), text, re.S | re.M)
    if m is None:
        raise AssertionError("missing %s.%s" % (kind, name))
    return m.group()


def rule(acl, name):
    m = re.search(r'  rule \{\n    name     = "%s".*?\n  \}\n' % re.escape(name), acl, re.S)
    if m is None:
        raise AssertionError("WAF rule %s missing" % name)
    return m.group()


class WafTests(unittest.TestCase):
    def setUp(self):
        self.acl = block(EDGE, "aws_wafv2_web_acl", "games_play")

    def test_the_web_acl_guards_the_games_alb(self):
        assoc = block(EDGE, "aws_wafv2_web_acl_association", "games_play")
        self.assertIn("aws_lb.games_play.arn", assoc)
        self.assertIn('scope       = "REGIONAL"', self.acl)
        self.assertRegex(self.acl, r"default_action \{\n\s+allow \{\}")

    def test_per_ip_rate_limit_blocks(self):
        r = rule(self.acl, "rate-per-ip")
        self.assertRegex(r, r"action \{\n\s+block \{\}")
        self.assertIn("limit                 = 6000", r)
        self.assertIn("evaluation_window_sec = 60", r)
        self.assertIn('aggregate_key_type    = "IP"', r)

    def test_managed_rules_block_except_common_which_counts(self):
        for name, group in (("ip-reputation", "AWSManagedRulesAmazonIpReputationList"),
                            ("bad-inputs", "AWSManagedRulesKnownBadInputsRuleSet")):
            r = rule(self.acl, name)
            self.assertIn(group, r)
            self.assertRegex(r, r"override_action \{\n\s+none \{\}", name + " must block")
        common = rule(self.acl, "common")
        self.assertIn("AWSManagedRulesCommonRuleSet", common)
        self.assertRegex(common, r"override_action \{\n\s+count \{\}")

    def test_blocks_and_counts_are_logged(self):
        group = block(EDGE, "aws_cloudwatch_log_group", "games_play_waf")
        self.assertRegex(group, r'name\s+= "aws-waf-logs-')
        self.assertRegex(group, r"retention_in_days = \d+")
        logging = block(EDGE, "aws_wafv2_web_acl_logging_configuration", "games_play")
        self.assertIn('action = "BLOCK"', logging)
        self.assertIn('action = "COUNT"', logging)


class EdgeTests(unittest.TestCase):
    def test_alb_access_logs_are_on_and_expire(self):
        alb = block(GAMES, "aws_lb", "games_play")
        self.assertRegex(alb, r"access_logs \{[^}]*enabled = true")
        self.assertIn("aws_s3_bucket.games_play_alb_logs.id", alb)
        life = block(EDGE, "aws_s3_bucket_lifecycle_configuration", "games_play_alb_logs")
        self.assertRegex(life, r"expiration \{ days = \d+ \}")
        pab = block(EDGE, "aws_s3_bucket_public_access_block", "games_play_alb_logs")
        self.assertEqual(pab.count("= true"), 4)
        self.assertIn("data.aws_elb_service_account.current.arn", EDGE)

    def test_router_keeps_two_tasks_and_autoscaling_owns_the_count(self):
        target = block(EDGE, "aws_appautoscaling_target", "games_mp_router")
        self.assertIn("min_capacity       = var.mp_router_min_count", target)
        self.assertRegex(GAMES, r'variable "mp_router_min_count" \{[^}]*default\s+= 2')
        svc = block(GAMES, "aws_ecs_service", "games_mp_router")
        self.assertIn("ignore_changes = [desired_count]", svc)

    def test_engines_reach_only_https_and_task_metadata(self):
        egress = block(GAMES, "aws_vpc_security_group_egress_rule", "games_engine")
        self.assertIn('ip_protocol       = "tcp"', egress)
        self.assertIn("from_port         = 443", egress)
        self.assertIn("to_port           = 443", egress)
        self.assertNotIn('ip_protocol       = "-1"', egress)
        meta = block(GAMES, "aws_vpc_security_group_egress_rule", "games_engine_task_metadata")
        self.assertIn('"169.254.170.2/32"', meta)

    def test_health_alarms_exist(self):
        for name in ("games_play_unhealthy_router", "games_play_no_healthy_router", "games_play_5xx_rate"):
            self.assertIn('resource "aws_cloudwatch_metric_alarm" "%s"' % name, EDGE)


class CostTests(unittest.TestCase):
    def test_lobby_limits_are_pinned_in_vercel_not_left_to_code_defaults(self):
        for key in ('"MP_MAX_ACTIVE_MATCHES/production"', '"MP_MAX_ACTIVE_MATCHES/preview"',
                    '"MP_IP_MAX_ACTIVE"', '"MP_IP_MAX_PER_HOUR"'):
            self.assertIn(key, BRINGUP)

    def test_engine_alarm_and_ecs_budget(self):
        alarm = block(GAMES, "aws_cloudwatch_metric_alarm", "games_mp_engines_over_lobby_caps")
        self.assertIn('metric_name         = "RunningEngines"', alarm)
        self.assertIn("threshold           = local.games_mp_lobby_engines_max", alarm)
        budget = block(GAMES, "aws_budgets_budget", "games_ecs_daily")
        self.assertIn('"Amazon Elastic Container Service"', budget)


if __name__ == "__main__":
    unittest.main()
