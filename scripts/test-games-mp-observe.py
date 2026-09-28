#!/usr/bin/env python3
"""The dev boxes' read-only view of the games pipeline (games-multiplayer-observe.tf).

It is a grant to every account on nextjs-dev, so it must stay read-only and must never
reach anything that returns a credential: secret values, Lambda or CodeBuild project
environments, image layers, Terraform state. Offline: reads the Terraform text only."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "games-multiplayer-observe.tf").read_text()

ALLOWED = {
    "codebuild:BatchGetBuilds", "codebuild:ListBuildsForProject",
    "logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams",
    "ecr:DescribeImages", "ecr:ListImages", "ecr:DescribeRepositories",
    "ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions", "ecs:ListTaskDefinitionFamilies",
    "ecs:ListTasks", "ecs:ListServices", "ecs:DescribeTasks", "ecs:DescribeServices",
    "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable",
    "cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
    "cloudwatch:ListMetrics",
    "secretsmanager:GetSecretValue",   # ReadMpTestKey only (see the test below)
}
# Statements that may use resources = ["*"]: the services give these no resource-level
# permissions. ListGamesTasks is bounded by its ecs:cluster condition instead.
STAR_OK = {"ReadTaskDefinitions", "ListGamesTasks", "ReadMetrics"}


def policy_document():
    m = re.search(r'^data "aws_iam_policy_document" "games_mp_observe" \{\n(.*?)^\}', TF, re.S | re.M)
    assert m, "games_mp_observe policy document not found"
    return m.group(1)


def statements():
    """{sid: body} for every statement block, split by brace depth."""
    doc, out, i = policy_document(), {}, 0
    while True:
        at = doc.find("statement {", i)
        if at < 0:
            return out
        depth, j = 0, doc.index("{", at)
        while True:
            depth += {"{": 1, "}": -1}.get(doc[j], 0)
            j += 1
            if depth == 0:
                break
        body = doc[at:j]
        sid = re.search(r'sid\s*=\s*"([^"]+)"', body)
        assert sid, "every statement needs a sid: %s" % body[:80]
        out[sid.group(1)] = body
        i = j


def actions(body):
    m = re.search(r"actions\s*=\s*\[([^\]]*)\]", body)
    assert m, body[:80]
    return re.findall(r'"([^"]+)"', m.group(1))


class ObserveGrantTests(unittest.TestCase):
    def test_only_read_actions_are_granted(self):
        granted = {a for body in statements().values() for a in actions(body)}
        self.assertTrue(granted, "no actions parsed")
        self.assertEqual(granted - ALLOWED, set(), "an action outside the read-only allowlist")
        for a in granted:
            self.assertNotIn("*", a, "wildcard action %s" % a)
            self.assertRegex(a.split(":", 1)[1], r"^(Get|List|Describe|BatchGet|Filter|Query|Scan)", a)

    def test_nothing_that_returns_a_credential(self):
        doc = policy_document()
        for forbidden in ("ssm:", "lambda:", "s3:", "kms:", "sts:", "iam:",
                          "codebuild:BatchGetProjects", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                          "ecr:GetAuthorizationToken", "logs:StartLiveTail"):
            self.assertNotIn('"%s' % forbidden, doc, forbidden)
        # Terraform state and its lock table.
        self.assertNotIn("terraform", doc.lower().replace("terraform_data", ""))

    def test_the_only_secret_is_the_mp_test_key(self):
        # The owner granted MP_TEST_KEY (hidden games in production) and nothing else.
        for sid, body in statements().items():
            if any(a.startswith("secretsmanager:") for a in actions(body)):
                self.assertEqual(sid, "ReadMpTestKey")
                self.assertEqual(actions(body), ["secretsmanager:GetSecretValue"])
                self.assertRegex(body, r"resources\s*=\s*\[aws_secretsmanager_secret\.games_mp_test_key\.arn\]")
        self.assertIn("ReadMpTestKey", statements())

    def test_the_test_key_resource_policy_admits_exactly_the_dev_roles(self):
        # Its explicit Deny overrides any identity grant: the dev roles must be named there,
        # and only on the test key (the cron secret stays administration-only).
        text = (ROOT / "games-multiplayer-bringup.tf").read_text()
        policy = re.search(r'resource "aws_secretsmanager_secret_policy" "games_mp_admin_only" \{.*?\n\}', text, re.S).group()
        self.assertIn("test_key    = { arn = aws_secretsmanager_secret.games_mp_test_key.arn, "
                      "readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn] }", policy)
        self.assertIn("cron_secret = { arn = aws_secretsmanager_secret.games_mp_cron_secret.arn, readers = [] }", policy)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, each.value.readers)', policy)

    def test_builds_are_readable_by_build_arn(self):
        # BatchGetBuilds is authorized on build/<project>:<id>, not on the project.
        body = statements()["ReadGamesBuilds"]
        self.assertIn(':build/${p.name}:*"', body)
        self.assertIn("p.arn", body)

    def test_every_statement_allows_and_star_only_where_the_service_forces_it(self):
        for sid, body in statements().items():
            self.assertNotRegex(body, r'effect\s*=\s*"Deny"', sid)
            if re.search(r'resources\s*=\s*\["\*"\]', body):
                self.assertIn(sid, STAR_OK, "%s grants on every resource" % sid)
        self.assertRegex(statements()["ListGamesTasks"], r'variable\s*=\s*"ecs:cluster"')

    def test_logs_are_the_pipelines_own_groups_only(self):
        body = statements()["ReadGamesLogs"]
        groups = set(re.findall(r"aws_cloudwatch_log_group\.(\w+)", body))
        self.assertEqual(groups, {
            "games_mp_codebuild", "games_mp_codebuild_preview", "games_mp_migrate", "games_mp_poller",
            "games_mp_release", "games_mp_sweeper", "games_mp_router", "games_engines", "games_mp_launch"})
        self.assertNotIn("games_play_waf", body, "WAF logs hold players' addresses")

    def test_both_dev_roles_get_it_and_nothing_else_does(self):
        # One managed policy (inline would not fit: the roles' inline budget is nearly full),
        # attached to exactly the two dev roles.
        self.assertRegex(TF, r'resource "aws_iam_policy" "games_mp_observe" \{[^}]*policy\s*=\s*data\.aws_iam_policy_document\.games_mp_observe\.json')
        attach = re.search(r'resource "aws_iam_role_policy_attachment" "games_mp_observe" \{(.*?)\n\}', TF, re.S).group(1)
        self.assertEqual(sorted(re.findall(r"aws_iam_role\.(\w+)\.name", attach)), ["dev_server", "nextjs_dev"])
        self.assertIn("policy_arn = aws_iam_policy.games_mp_observe.arn", attach)
        for path in ROOT.glob("*.tf"):
            if path.name != "games-multiplayer-observe.tf":
                text = path.read_text()
                self.assertNotIn("aws_iam_policy_document.games_mp_observe", text, path.name)
                self.assertNotIn("aws_iam_policy.games_mp_observe", text, path.name)

    def test_build_environments_hold_no_plaintext_secret(self):
        # BatchGetBuilds returns each build's environment: a secret must reach a build as a
        # SECRETS_MANAGER or PARAMETER_STORE reference, never as a PLAINTEXT value.
        for name in ("games-multiplayer-bringup.tf", "games-multiplayer-deploy.tf"):
            text = (ROOT / name).read_text()
            for block in re.findall(r"environment_variable \{(.*?)\}", text, re.S):
                var = re.search(r'name\s*=\s*"([^"]+)"', block).group(1)
                kind = re.search(r'type\s*=\s*"([^"]+)"', block)
                if kind is None or kind.group(1) == "PLAINTEXT":
                    self.assertNotRegex(var, r"SECRET|TOKEN|PASSWORD|PASS|KEY|_URL$|CREDENTIAL", "%s: %s" % (name, var))

    def test_task_definitions_hold_no_plaintext_secret(self):
        # DescribeTaskDefinition cannot be scoped: no family may carry a secret in `environment`.
        # The router's (Terraform) and the engines' (games-mp-release registers them from the
        # template in games-multiplayer-deploy.tf and release.py).
        text = "".join(p.read_text() for p in sorted(ROOT.glob("games-multiplayer*.tf")))
        text += (ROOT / "games-multiplayer" / "release.py").read_text()
        names = re.findall(r'\{\s*"?name"?\s*[=:]\s*"([A-Z0-9_]+)",\s*"?value"?\s*[=:]', text)
        self.assertTrue(names, "no container environment parsed")
        for var in names:
            if var in ("MP_TOKEN_PUBLIC_KEYS", "MP_TOKEN_VERIFIER"):
                continue   # public keys, and the verifier's scheme marker (ed25519-v2)
            self.assertNotRegex(var, r"SECRET|TOKEN|PASSWORD|PRIVATE|CREDENTIAL|_KEY$|_KEYS$", var)


if __name__ == "__main__":
    unittest.main(verbosity=2)
