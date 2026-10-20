#!/usr/bin/env python3
"""Offline checks for security-hub-automation.tf and default-security-groups.tf.

A Security Hub automation rule silences an alert, so it must stay as narrow as its evidence: this account only, Security
Hub control findings only (never a GuardDuty or Inspector finding), exact security-group ARNs for the open-SSH rule and
exactly the nextjs-ct session policies for IAM.1. Adding a new suppression means changing EXPECTED_RULES here on purpose.
The default security groups must declare no rule at all: the provider then removes every rule from them."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTOMATION = ROOT / "security-hub-automation.tf"
DEFAULTS = ROOT / "default-security-groups.tf"
MODULE = ROOT / "modules/pbox-fallback-site/main.tf"
ACCOUNT = "data.aws_caller_identity.current.account_id"

STAGING = "data.aws_caller_identity.staging.account_id"
DOLPHIN = "data.aws_caller_identity.dolphin.account_id"
# Resource name => (provider, the for_each source, the security module it waits for, the one account it matches).
RESOURCES = {
    "by_design": (None, 'local.security_hub_rules_by_region["us-west-1"]', "module.security_main_us_west_1", ACCOUNT),
    "by_design_west2": ("aws.west2", 'local.security_hub_rules_by_region["us-west-2"]', "module.security_main_us_west_2", ACCOUNT),
    "by_design_dr": ("aws.dr", 'local.security_hub_rules_by_region["us-east-1"]', "module.security_main_us_east_1", ACCOUNT),
    # staging runs its own Security Hub (no administrator): its rules are created in it, matching only it
    "by_design_staging": ("aws.staging", "local.security_hub_staging_rules", "module.security_staging_us_west_1", STAGING),
    "by_design_staging_dr": ("aws.staging_dr", "local.security_hub_staging_rules", "module.security_staging_us_east_1", STAGING),
    # Dolphin's findings arrive here (its Security Hub administrator, us-west-1): created here, matching only Dolphin
    "by_design_dolphin": (None, "local.security_hub_dolphin_rules", "module.dolphin_security_us_west_1", DOLPHIN),
}
CALLERS = {"staging": "aws.staging", "dolphin": "aws.dolphin"}

# Every rule, its controls and its resource scope. Account-wide rules have ("", []).
IAM_PREFIX = '"arn:aws:iam::${%s}:policy/nextjs-ct-member-"' % ACCOUNT
EXPECTED_RULES = {
    "ec2-public-ipv4-by-design": (["EC2.9"], "", []),
    "guardduty-protection-plans-off-by-design": (["GuardDuty.%d" % n for n in range(5, 12)], "", []),
    "inspector-ecr-lambda-code-off-by-design": (["Inspector.2", "Inspector.3"], "", []),
    "config-custom-recorder-role-by-design": (["Config.1"], "", []),
    "public-ip-assignment-by-design": (["EC2.25", "ECS.2"], "", []),
    "nextjs-ct-session-policies-by-design": (["IAM.1"], "PREFIX", [IAM_PREFIX]),
}
SSH_RULE = "public-ssh-by-design"
SSH_GROUPS = {
    "us-west-1": [
        "[aws_security_group.nextjs_dev.arn]",
        "aws_security_group.firecracker_dev[*].arn",
        "aws_security_group.x86_dev[*].arn",
        "aws_security_group.jumpbox_2[*].arn",
        "[module.pbox_fallback_us_west_1.security_group_arn]",
    ],
    "us-west-2": ["[module.pbox_fallback_us_west_2.security_group_arn]"],
    "us-east-1": ["[module.pbox_fallback_us_east_1.security_group_arn]"],
}
# The groups the open-SSH rule names, by file: their internet-facing ingress must stay tcp 22 and 2022 only, because
# the suppression is per group and would also hide a new public port on one of them.
SSH_GROUP_SOURCES = {
    "nextjs-dev.tf": "nextjs_dev",
    "firecracker-dev.tf": "firecracker_dev",
    "x86-dev.tf": "x86_dev",
    "jumpbox2.tf": "jumpbox_2",
    "modules/pbox-fallback-site/main.tf": "this",
}
DEFAULT_GROUPS = {
    "main": (None, "local.vpc_id"),
    "west1_default": (None, "data.aws_vpc.west1_default.id"),
    "runner": (None, "aws_vpc.runner[0].id"),
    "pbox_fallback_us_west_1": (None, "module.pbox_fallback_us_west_1.vpc_id"),
    "west2_default": ("aws.west2", "data.aws_vpc.west2_default.id"),
    "pbox_fallback_us_west_2": ("aws.west2", "module.pbox_fallback_us_west_2.vpc_id"),
    "east1_default": ("aws.dr", "data.aws_vpc.east1_default.id"),
    "pbox_fallback_us_east_1": ("aws.dr", "module.pbox_fallback_us_east_1.vpc_id"),
    "staging_west1_default": ("aws.staging", "data.aws_vpc.staging_west1_default.id"),
    "staging_east1_default": ("aws.staging_dr", "data.aws_vpc.staging_east1_default.id"),
    "dolphin_west1_default": ("aws.dolphin", "data.aws_vpc.dolphin_west1_default.id"),
}


def tf_files():
    return sorted(p for p in ROOT.rglob("*.tf") if not {".terraform", ".claude"} & set(p.relative_to(ROOT).parts))


def strip_comments(text):
    return "\n".join(re.sub(r"\s#.*$", "", line) if not line.lstrip().startswith("#") else "" for line in text.splitlines())


def matching(text, start):
    """Index just past the bracket closing the one at text[start], skipping quoted strings."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack, i, quoted = [], start, False
    while i < len(text):
        c = text[i]
        if quoted:
            if c == "\\":
                i += 1
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c in pairs:
            stack.append(pairs[c])
        elif c in "}])":
            if not stack or stack.pop() != c:
                raise ValueError("unbalanced at %d" % i)
            if not stack:
                return i + 1
        i += 1
    raise ValueError("unclosed bracket at %d" % start)


def blocks(text, header):
    """Bodies of every block whose header line matches the regex `header`."""
    out = []
    for m in re.finditer(header + r"\s*\{", text):
        end = matching(text, m.end() - 1)
        out.append((m, text[m.end():end - 1]))
    return out


def attr(body, name):
    m = re.search(r"(?m)^\s*%s\s*=\s*(.+)$" % re.escape(name), body)
    if not m:
        return None
    value = m.group(1).strip()
    if value[0] in "[{(":
        start = body.index(value[0], m.start(1))
        return body[start:matching(body, start)]
    return value


def local_value(text, name):
    m = re.search(r"(?m)^  %s\s*=\s*" % re.escape(name), text)
    if not m:
        raise AssertionError("local %s not found" % name)
    start = m.end()
    return text[start:matching(text, start)]


def items(listtext):
    """Top-level elements of a bracketed HCL list or call argument list."""
    inner, out, depth, cur, quoted = listtext[1:-1], [], 0, "", False
    i = 0
    while i < len(inner):
        c = inner[i]
        if quoted:
            cur += c
            if c == "\\":
                cur += inner[i + 1]
                i += 1
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted, cur = True, cur + c
        elif c in "{[(":
            depth, cur = depth + 1, cur + c
        elif c in "}])":
            depth, cur = depth - 1, cur + c
        elif c == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip():
        out.append(cur.strip())
    return out


def rule_fields(objtext):
    body = objtext[1:-1]
    return {
        "name": attr(body, "name").strip('"'),
        "order": int(attr(body, "order")),
        "controls": [c.strip('"') for c in items(attr(body, "controls"))],
        "resource_comparison": attr(body, "resource_comparison").strip('"'),
        "resource_ids": items(attr(body, "resource_ids")) if attr(body, "resource_ids") else None,
        "note": attr(body, "note"),
        "description": attr(body, "description"),
    }


class SecurityHubAutomation(unittest.TestCase):
    def setUp(self):
        self.text = strip_comments(AUTOMATION.read_text())
        self.rules = [rule_fields(o) for o in items(local_value(self.text, "security_hub_rules"))]
        self.ssh = rule_fields(local_value(self.text, "security_hub_open_ssh_rule"))

    def resources(self):
        return {m.group(1): body for m, body in blocks(self.text, r'resource "aws_securityhub_automation_rule" "([^"]+)"')}

    def test_no_other_automation_rule_anywhere(self):
        for path in tf_files():
            if path == AUTOMATION:
                continue
            self.assertNotIn('"aws_securityhub_automation_rule"', path.read_text(), path)
        self.assertEqual(set(self.resources()), set(RESOURCES))

    def test_every_rule_requires_this_account_security_hub_product_and_active(self):
        for name, body in self.resources().items():
            criteria = blocks(body, r"(?m)^  criteria")
            self.assertEqual(len(criteria), 1, name)
            crit = criteria[0][1]
            fields = {m.group(1): b for m, b in blocks(crit, r"(?m)^    (?:dynamic \")?([a-z_]+)\"?")}
            self.assertEqual(fields.get("aws_account_id", "").split(), ["comparison", "=", '"EQUALS"', "value", "=", RESOURCES[name][3]], name)
            self.assertEqual(fields.get("product_name", "").split(), ["comparison", "=", '"EQUALS"', "value", "=", '"Security', 'Hub"'], name)
            self.assertEqual(fields.get("record_state", "").split(), ["comparison", "=", '"EQUALS"', "value", "=", '"ACTIVE"'], name)
            # Only these fields: nothing that could widen a rule or reach another product's findings.
            self.assertEqual(set(fields), {"aws_account_id", "product_name", "record_state", "compliance_security_control_id", "resource_id"}, name)
            self.assertNotIn("NOT_EQUALS", crit, name)
            self.assertNotIn("CONTAINS", crit, name)
            self.assertIn("comparison = \"EQUALS\"\n        value      = compliance_security_control_id.value", re.sub(r"[ ]+\n", "\n", crit), name)
            self.assertIn("comparison = each.value.resource_comparison", crit, name)
            self.assertIn('status = "SUPPRESSED"', body, name)

    def test_regions_providers_and_preconditions(self):
        for name, body in self.resources().items():
            provider, source, module, _ = RESOURCES[name]
            self.assertEqual(attr(body, "provider"), provider, name)
            self.assertIn(" in %s :" % source, attr(body, "for_each"), name)
            self.assertIn("if local.security_posture_enabled", attr(body, "for_each"), name)
            self.assertIn(module, attr(body, "depends_on"), name)
            self.assertIn("(each.value.resource_comparison == \"\") == (length(each.value.resource_ids) == 0)", body, name)

    def test_rules_are_exactly_the_reviewed_set(self):
        got = {r["name"]: (r["controls"], r["resource_comparison"], r["resource_ids"]) for r in self.rules}
        self.assertEqual(got, EXPECTED_RULES)
        self.assertEqual((self.ssh["name"], self.ssh["controls"], self.ssh["resource_comparison"]), (SSH_RULE, ["EC2.18", "EC2.19"], "EQUALS"))
        self.assertIsNone(self.ssh["resource_ids"])  # filled per region from security_hub_open_ssh_groups

    def test_staging_rules_are_the_three_by_design_main_rules(self):
        src = local_value(self.text, "security_hub_staging_rules")
        names = re.findall(r'"([a-z0-9-]+-by-design)"', src)
        self.assertEqual(names, ["guardduty-protection-plans-off-by-design", "inspector-ecr-lambda-code-off-by-design",
                                 "config-custom-recorder-role-by-design"])
        self.assertTrue(src.startswith("[for r in local.security_hub_rules :"), src[:60])
        for n in names:
            self.assertEqual(EXPECTED_RULES[n][1:], ("", []), n)  # account-wide controls only; no resource-scoped rule

    def test_dolphin_rules_are_the_reviewed_set(self):
        rules = [rule_fields(o) for o in items(local_value(self.text, "security_hub_dolphin_rules"))]
        got = {r["name"]: (r["controls"], r["resource_comparison"], r["resource_ids"], r["order"]) for r in rules}
        self.assertEqual(got, {
            "dolphin-inspector-ecr-lambda-code-off-by-design": (["Inspector.2", "Inspector.3"], "", [], 8),
            "dolphin-redis-public-ip-by-design": (["ECS.2"], "EQUALS",
                                                  ['"arn:aws:ecs:us-west-1:${%s}:service/dolphin/redis"' % DOLPHIN], 9),
        })
        # they share us-west-1 with the main rules: orders and names must not collide
        main = {r["order"] for r in self.rules} | {self.ssh["order"]}
        self.assertFalse(main & {r["order"] for r in rules})
        for r in rules:
            self.assertIn("security-hub-automation.tf", r["note"])
        # Dolphin has no security-forward-audit path: a Config.1 rule there would also hide a stopped recorder
        self.assertNotIn("Config.1", local_value(self.text, "security_hub_dolphin_rules"))

    def test_account_callers_go_through_the_account_providers(self):
        everything = "\n".join(strip_comments(p.read_text()) for p in tf_files())
        for name, provider in CALLERS.items():
            found = blocks(everything, r'data "aws_caller_identity" "%s"' % name)
            self.assertEqual(len(found), 1, name)
            self.assertEqual(attr(found[0][1], "provider"), provider, name)

    def test_iam_prefix_is_exactly_the_session_policies(self):
        rule = next(r for r in self.rules if r["controls"] == ["IAM.1"])
        self.assertEqual(rule["resource_ids"], [IAM_PREFIX])
        self.assertTrue(rule["resource_ids"][0].endswith(':policy/nextjs-ct-member-"'))
        prefixed = [r["name"] for r in self.rules if r["resource_comparison"] == "PREFIX"]
        self.assertEqual(prefixed, [rule["name"]])

    def test_ssh_rule_names_exact_group_arns(self):
        groups = local_value(self.text, "security_hub_open_ssh_groups")
        got = {}
        for m in re.finditer(r'"(us-[a-z]+-\d)"\s*=\s*', groups):
            start = m.end()
            value = groups[start:matching(groups, start)]
            got[m.group(1)] = items(value[len("concat"):]) if value.startswith("concat") else [value]
        self.assertEqual(got, SSH_GROUPS)
        for refs in got.values():
            for ref in refs:
                self.assertNotIn('"', ref)  # a reference to a group, never a literal, prefix or wildcard
                self.assertRegex(ref, r"\.(arn|security_group_arn)\]?$")
        self.assertIn("merge(local.security_hub_open_ssh_rule, { resource_ids = arns })", self.text)
        self.assertIn('value = aws_security_group.this.arn', re.sub(r"\s+", " ", MODULE.read_text()))

    def test_ssh_groups_open_only_ssh_and_et_to_the_internet(self):
        for path, name in SSH_GROUP_SOURCES.items():
            text = strip_comments((ROOT / path).read_text())
            found = blocks(text, r'resource "aws_security_group" "%s"' % name)
            self.assertEqual(len(found), 1, path)
            for _, ingress in blocks(found[0][1], r"(?m)^\s*(?:dynamic \")?ingress\"?"):
                if "0.0.0.0/0" in ingress or "::/0" in ingress:
                    ports = (attr(ingress, "from_port"), attr(ingress, "to_port"), attr(ingress, "protocol"))
                    self.assertIn(ports, {("22", "22", '"tcp"'), ("2022", "2022", '"tcp"')}, (path, ingress))
            # A rule added from outside the resource would escape the check above: there must be none. Root groups are
            # referenced from root files, the module's group from the module's own files.
            scope = [f for f in tf_files() if ("modules" in f.relative_to(ROOT).parts) == path.startswith("modules/")]
            ref = re.compile(r"aws_security_group\.%s(\[\d+\])?\.id" % name)
            for other in scope:
                text = other.read_text()
                for kind in ("aws_security_group_rule", "aws_vpc_security_group_ingress_rule"):
                    for _, body in blocks(text, r'resource "%s" "[^"]+"' % kind):
                        self.assertIsNone(ref.search(body), (other, kind))

    def test_security_hub_limits(self):
        orders = [r["order"] for r in self.rules] + [self.ssh["order"]]
        self.assertEqual(len(orders), len(set(orders)))
        self.assertTrue(all(1 <= o <= 1000 for o in orders))
        names = [r["name"] for r in self.rules] + [self.ssh["name"]]
        self.assertEqual(len(names), len(set(names)))
        for r in self.rules + [self.ssh]:
            self.assertLessEqual(len(r["controls"]), 20, r["name"])
            self.assertLessEqual(len(r["resource_ids"] or []), 20, r["name"])
            self.assertLessEqual(len(r["note"]), 512, r["name"])
            self.assertLessEqual(len(r["description"]), 1024, r["name"])
            self.assertIn("security-hub-automation.tf", r["note"], r["name"])
        self.assertLessEqual(max(len(v) for v in SSH_GROUPS.values()), 20)

    def test_ssh_note_says_when_it_ends(self):
        self.assertIn("Cloudflare VPN replaces public SSH", self.ssh["description"])
        self.assertIn("Cloudflare VPN replaces public SSH", self.ssh["note"])

    def test_ec2_9_rule_moved_not_recreated(self):
        for old, new in (("public_ipv4_by_design", "by_design"), ("public_ipv4_by_design_west2", "by_design_west2"),
                         ("public_ipv4_by_design_dr", "by_design_dr")):
            self.assertRegex(self.text, r'moved \{\s*from = aws_securityhub_automation_rule\.%s\[0\]\s*to\s*= aws_securityhub_automation_rule\.%s\["ec2-public-ipv4-by-design"\]\s*\}' % (old, new))


class DefaultSecurityGroups(unittest.TestCase):
    def test_every_default_group_declares_no_rule(self):
        seen = {}
        for path in tf_files():
            text = strip_comments(path.read_text())
            for m, body in blocks(text, r'resource "aws_default_security_group" "([^"]+)"'):
                self.assertEqual(path, DEFAULTS, m.group(1))
                self.assertNotRegex(body, r"\b(ingress|egress)\b", m.group(1))
                seen[m.group(1)] = (attr(body, "provider"), attr(body, "vpc_id"))
            for kind in ("aws_security_group_rule", "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule"):
                for _, body in blocks(text, r'resource "%s" "[^"]+"' % kind):
                    self.assertNotIn("aws_default_security_group", body, path)
        self.assertEqual(seen, DEFAULT_GROUPS)

    def test_module_exposes_vpc_id(self):
        self.assertIn("value       = aws_vpc.this.id", MODULE.read_text())


if __name__ == "__main__":
    unittest.main()
