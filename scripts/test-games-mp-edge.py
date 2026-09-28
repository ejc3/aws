#!/usr/bin/env python3
"""Pins the public edge of play.cc-games.app (games-multiplayer-edge.tf and friends).

play.cc-games.app is the first AWS resource outside users reach, so the protections in front
of it must not quietly regress: the WAF and its rules, access logs, router redundancy, engine
egress, the engine ceiling and the lobby's launch limits. Offline; reads the Terraform source.

Run from the repo root:  python3 -S -B scripts/test-games-mp-edge.py
"""
import ipaddress
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


MAIN = (ROOT / "main.tf").read_text()
# Stand-ins for the two values the ACL reads from AWS; the real ones are the same shape.
VPC4 = "10.0.0.0/16"
VPC6 = "2600:1f1c:494:200::/56"
PROTOCOLS = {"tcp": 6, "udp": 17, "icmp": 1, "58": 58, "-1": None}


def cidrsubnet(prefix, newbits, num):
    return str(list(ipaddress.ip_network(prefix).subnets(prefixlen_diff=newbits))[num])


def layout():
    """games_subnet_layout -> {key: {engine: cidr, router: cidr}} as Terraform renders it."""
    m = re.search(r"games_subnet_layout = \{\n(.*?)\n  \}\n", GAMES, re.S)
    if m is None:
        raise AssertionError("games_subnet_layout missing")
    out = {}
    for key, body in re.findall(r"^    (\w+) = \{ (.*?) \}$", m.group(1), re.M):
        idx = dict(re.findall(r"(engine|router) = (\d+)", body))
        out[key] = {role: cidrsubnet(VPC4, 8, int(n)) for role, n in idx.items()}
    return out


def acl_rules(acl, direction):
    """Every ingress/egress rule of an aws_network_acl block, with expressions resolved."""
    router = [layout()[k]["router"] for k in sorted(layout())]
    subst = {
        "data.aws_vpc.selected.cidr_block": VPC4,
        "aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block": VPC6,
        "local.mp_port": "8080",
    }
    rules = []

    def parse(body, extra=None):
        r = dict(extra or {})
        for k, v in re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", body, re.M):
            v = v.strip('"')
            r[k] = subst.get(v, v)
        return r

    for body in re.findall(r"^  %s \{\n(.*?)\n  \}" % direction, acl, re.S | re.M):
        rules.append(parse(body))
    dyn = re.search(r'^  dynamic "%s" \{\n(.*?)\n  \}' % direction, acl, re.S | re.M)
    if dyn:
        self_ref = direction + ".value"
        if "for i, s in local.mp_router_subnets : i => s.cidr_block" not in dyn.group(1):
            raise AssertionError("dynamic %s must range over the router subnets" % direction)
        content = re.search(r"content \{\n(.*?)\n    \}", dyn.group(1), re.S).group(1)
        for i, cidr in enumerate(router):
            r = parse(content)
            r["rule_no"] = str(eval(r["rule_no"].replace(direction + ".key", str(i))))
            r["cidr_block"] = cidr if r.get("cidr_block") == self_ref else r.get("cidr_block")
            rules.append(r)
    for r in rules:
        r["rule_no"] = int(r["rule_no"])
    return sorted(rules, key=lambda r: r["rule_no"])


def acl_verdict(rules, proto, peer, port=None, icmp=None):
    """What an AWS network ACL does with one packet: first matching rule by number, else deny."""
    addr = ipaddress.ip_address(peer)
    for r in rules:
        cidr = r.get("cidr_block") if addr.version == 4 else r.get("ipv6_cidr_block")
        if cidr is None or addr not in ipaddress.ip_network(cidr):
            continue
        p = PROTOCOLS[r["protocol"]]
        if p is not None and p != proto:
            continue
        if p in (6, 17) and not int(r["from_port"]) <= port <= int(r["to_port"]):
            continue
        if p in (1, 58) and (int(r["icmp_type"]), int(r["icmp_code"])) != icmp:
            continue
        return r["action"]
    return "deny"


class NetworkTests(unittest.TestCase):
    """Engines and the router live in their own subnets, off the I/O box peer, fenced from the VPC."""

    def test_games_subnets_are_their_own_and_disjoint_from_the_dev_fleet(self):
        net = layout()
        self.assertEqual(sorted(net), ["a", "b"])
        self.assertEqual({k: v["engine"] for k, v in net.items()}, {"a": "10.0.64.0/24", "b": "10.0.65.0/24"})
        self.assertEqual({k: v["router"] for k, v in net.items()}, {"a": "10.0.66.0/24", "b": "10.0.67.0/24"})
        dev = [ipaddress.ip_network(c) for c in re.findall(r'cidr_block\s*=\s*"(10\.0\.\d+\.0/24)"', MAIN)]
        self.assertEqual(len(dev), 2)
        for v in net.values():
            for c in v.values():
                self.assertFalse(any(ipaddress.ip_network(c).overlaps(d) for d in dev), c)
        self.assertIn("dev_fleet_subnets = [aws_subnet.subnet_a, aws_subnet.subnet_b]", MAIN)
        for kind in ("games_engine", "games_router"):
            subnet = block(GAMES, "aws_subnet", kind)
            self.assertIn("for_each = local.games_subnet_layout", subnet)
            self.assertIn("assign_ipv6_address_on_creation = true", subnet)
            self.assertRegex(subnet, r"map_public_ip_on_launch\s*=\s*false")
        self.assertIn("mp_engine_subnets = values(aws_subnet.games_engine)", GAMES)
        self.assertIn("mp_router_subnets = values(aws_subnet.games_router)", GAMES)
        self.assertNotIn("local.mp_subnets", GAMES)

    def test_games_route_table_has_internet_and_no_peer_route(self):
        rt = block(GAMES, "aws_route_table", "games")
        routes = re.findall(r"  route \{\n(.*?)\n  \}", rt, re.S)
        self.assertEqual(len(routes), 2, routes)
        self.assertIn('cidr_block = "0.0.0.0/0"', routes[0])
        self.assertIn('ipv6_cidr_block = "::/0"', routes[1])
        for r in routes:
            self.assertRegex(r, r"gateway_id\s+= aws_internet_gateway\.main\.id")
        for forbidden in ("vpc_peering_connection_id", "transit_gateway_id", "nat_gateway_id",
                          "west2_default", "io_box", "vpc_endpoint_id", "network_interface_id"):
            self.assertNotIn(forbidden, rt)
        # No standalone aws_route anywhere in the repo can be bolted onto it either.
        for tf in ROOT.glob("*.tf"):
            for route in re.findall(r'^resource "aws_route" "\w+" \{\n.*?^\}', tf.read_text(), re.S | re.M):
                self.assertNotIn("aws_route_table.games.", route, tf.name)
        for kind in ("games_engine", "games_router"):
            assoc = block(GAMES, "aws_route_table_association", kind)
            self.assertIn("for_each       = aws_subnet.%s" % kind, assoc)
            self.assertIn("route_table_id = aws_route_table.games.id", assoc)
        # Sanity: the dev subnets' table is the one that carries the peer (so the check above means something).
        self.assertIn("vpc_peering_connection_id = aws_vpc_peering_connection.io_box.id",
                      block(MAIN, "aws_route_table", "public"))

    def test_engine_acl_admits_only_the_router_and_internet_replies(self):
        acl = block(GAMES, "aws_network_acl", "games_engine")
        self.assertIn("subnet_ids = [for s in local.mp_engine_subnets : s.id]", acl)
        rules = acl_rules(acl, "ingress")
        router_a, router_b = (layout()[k]["router"] for k in ("a", "b"))
        ra, rb = (str(ipaddress.ip_network(c)[20]) for c in (router_a, router_b))
        cases = [
            ((6, ra, 8080), "allow"), ((6, rb, 8080), "allow"),
            ((6, ra, 22), "deny"), ((6, ra, 40000), "deny"),          # router: 8080 only
            ((6, "10.0.1.72", 8080), "deny"),                        # jumpbox / dev boxes
            ((6, "10.0.2.20", 40000), "deny"),                       # no fake "replies" from the VPC
            ((6, "10.0.64.9", 8080), "deny"),                        # engine in the other AZ
            ((6, "2600:1f1c:494:201::5", 40000), "deny"),            # dev box over IPv6
            ((6, "203.0.113.7", 40000), "allow"), ((6, "2001:db8::7", 40000), "allow"),
            ((6, "203.0.113.7", 8080), "deny"), ((6, "2001:db8::7", 8080), "deny"),  # public IP
            ((6, "203.0.113.7", 22), "deny"),
            ((17, "203.0.113.7", 40000), "deny"),
        ]
        for (proto, peer, port), want in cases:
            self.assertEqual(acl_verdict(rules, proto, peer, port), want, (proto, peer, port))
        self.assertEqual(acl_verdict(rules, 1, "203.0.113.7", icmp=(3, 4)), "allow")
        self.assertEqual(acl_verdict(rules, 1, "203.0.113.7", icmp=(8, 0)), "deny")
        self.assertEqual(acl_verdict(rules, 58, "2001:db8::7", icmp=(2, 0)), "allow")
        self.assertEqual(acl_verdict(rules, 1, "10.0.1.72", icmp=(3, 4)), "deny")

    def test_engine_acl_sends_only_https_out_and_replies_to_the_router(self):
        acl = block(GAMES, "aws_network_acl", "games_engine")
        rules = acl_rules(acl, "egress")
        ra = str(ipaddress.ip_network(layout()["a"]["router"])[20])
        cases = [
            ((6, ra, 45000), "allow"), ((6, ra, 22), "deny"),
            ((6, "203.0.113.7", 443), "allow"), ((6, "2001:db8::7", 443), "allow"),
            ((6, "10.0.1.68", 443), "deny"), ((6, "10.0.2.20", 45000), "deny"),
            ((6, "2600:1f1c:494:201::5", 443), "deny"),
            ((6, "172.31.48.10", 2049), "deny"), ((6, "172.31.48.10", 22), "deny"),
            ((6, "203.0.113.7", 22), "deny"), ((6, "203.0.113.7", 80), "deny"),
            ((17, "203.0.113.7", 443), "deny"),
        ]
        for (proto, peer, port), want in cases:
            self.assertEqual(acl_verdict(rules, proto, peer, port), want, (proto, peer, port))

    def test_engines_and_router_use_the_games_subnets(self):
        fn = block(GAMES, "aws_lambda_function", "games_mp_launch")
        self.assertRegex(fn, r'SUBNETS\s+= join\(",", \[for s in local\.mp_engine_subnets : s\.id\]\)')
        # The launch function switches subnets only after every router accepts them.
        self.assertIn("depends_on = [\n    aws_route_table_association.games_engine, aws_network_acl.games_engine, "
                      "terraform_data.games_mp_healthy,", fn)
        router_td = block(GAMES, "aws_ecs_task_definition", "games_mp_router")
        self.assertIn('{ name = "MP_TARGET_CIDRS", value = join(",", local.mp_router_target_cidrs) }', router_td)
        # The routers accept exactly the engine subnets, nothing in the dev fleet.
        self.assertIn("mp_router_target_cidrs = [for s in local.mp_engine_subnets : s.cidr_block]", GAMES)
        self.assertNotIn("mp_legacy_engine_cidrs", GAMES)
        svc = block(GAMES, "aws_ecs_service", "games_mp_router")
        self.assertIn("subnets         = [for s in local.mp_router_subnets : s.id]", svc)
        self.assertIn("aws_route_table_association.games_router", svc)
        alb = block(GAMES, "aws_lb", "games_play")
        self.assertIn("subnets                    = [for s in local.mp_alb_subnets : s.id]", alb)
        self.assertIn("mp_alb_subnets = local.dev_fleet_subnets", GAMES)


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


class SecondDomainTests(unittest.TestCase):
    """cc-games.net is for networks that block cc-games.app: it must SERVE, never redirect there."""
    VERCEL = (ROOT / "vercel-cc-games.tf").read_text()

    def test_cc_games_net_serves_and_its_www_stays_on_net(self):
        apex = block(self.VERCEL, "vercel_project_domain", "cc_games_net")
        self.assertIn('domain     = "cc-games.net"', apex)
        self.assertNotIn("redirect", apex, "cc-games.net must serve the site, not redirect to .app")
        www = block(self.VERCEL, "vercel_project_domain", "www_cc_games_net")
        self.assertIn("redirect             = vercel_project_domain.cc_games_net.domain", www)
        for name in ("cc_games_net_apex", "cc_games_net_www"):
            record = block(self.VERCEL, "cloudflare_dns_record", name)
            self.assertIn("zone_id = var.cc_games_net_zone_id", record)
            self.assertIn("proxied = false", record)

    def test_play_cc_games_net_reaches_the_same_router_with_its_own_certificate(self):
        self.assertIn('mp_play_domain_net = "play.cc-games.net"', GAMES)
        self.assertIn('"https://cc-games.net",', GAMES, "the router must accept the .net page's origin")
        listener_cert = block(GAMES, "aws_lb_listener_certificate", "games_play_net")
        self.assertIn("listener_arn    = aws_lb_listener.games_play_https.arn", listener_cert)
        self.assertIn("aws_acm_certificate_validation.games_play_net.certificate_arn", listener_cert)
        # Production hands .net pages the .net entry (first) and calls engines back on .net;
        # previews set no entry of their own.
        self.assertIn('"MP_PUBLIC_ENTRY" = { targets = ["production"], sensitive = false, '
                      'value = "wss://${local.mp_play_domain_net},wss://${local.mp_play_domain}" }', BRINGUP)
        self.assertIn('"MP_API/production" = { targets = ["production"], sensitive = false, value = "https://cc-games.net" }', BRINGUP)
        dns = block(GAMES, "cloudflare_dns_record", "games_play_net")
        self.assertIn("zone_id = var.cc_games_net_zone_id", dns)
        self.assertIn("content = aws_lb.games_play.dns_name", dns)
        self.assertIn("proxied = false", dns)


if __name__ == "__main__":
    unittest.main()
