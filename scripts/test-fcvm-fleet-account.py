#!/usr/bin/env python3
"""Offline tests for fcvm-fleet-account.tf: the fcvm-fleet member account (ejc3/aws#604) and its G spot quotas (#605)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-fleet-account.tf").read_text()
CONST = (ROOT / "constellation-account.tf").read_text()
PEOPLE = (ROOT / "people.tf").read_text()


def block(header, text=TF):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


class AccountTests(unittest.TestCase):
    def test_the_account_is_never_closed_or_replaced_by_terraform(self):
        acct = block('resource "aws_organizations_account" "fcvm_fleet"')
        self.assertIn("close_on_deletion          = false", acct)
        self.assertIn("prevent_destroy = true", acct)
        self.assertIn("email                      = local.fcvm_fleet_account_email", acct)
        self.assertIn("parent_id                  = aws_organizations_organizational_unit.fcvm_fleet.id", acct)

    def test_its_root_address_comes_from_people_addresses(self):
        self.assertIn("fcvm_fleet_account_email    = nonsensitive(local.people.fcvm_fleet_account)", PEOPLE)
        self.assertIn("can(local.people.fcvm_fleet_account)", PEOPLE)
        self.assertNotRegex(TF, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")

    def test_one_provider_alias_per_fleet_region_through_the_access_role(self):
        self.assertIn('fcvm_fleet_regions         = ["us-east-2", "us-west-2", "us-east-1"]', TF)
        for alias, region in (("fcvm_fleet", "us-east-2"), ("fcvm_fleet_west2", "us-west-2"), ("fcvm_fleet_east1", "us-east-1")):
            prov = re.search(r'provider "aws" \{\n  alias  = "%s"\n  region = "%s"\n  assume_role \{\n    role_arn = "([^"]+)"' % (alias, region), TF)
            self.assertIsNotNone(prov, alias)
            self.assertTrue(prov.group(1).endswith(":role/OrganizationAccountAccessRole"))


class GuardrailTests(unittest.TestCase):
    def scp(self):
        return block("fcvm_fleet_scp = {")

    def test_regions_leave_and_iam_users_are_denied_on_the_ou_only(self):
        s = self.scp()
        self.assertIn('Condition = { StringNotEquals = { "aws:RequestedRegion" = local.fcvm_fleet_regions } }', s)
        self.assertIn("NotAction = local.dolphin_region_scp_global_actions", s)
        self.assertIn('Action   = "organizations:LeaveOrganization"', s)
        self.assertIn('"iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile"', s)
        self.assertNotIn('"Allow"', s)
        att = block('resource "aws_organizations_policy_attachment" "fcvm_fleet_guardrails"')
        self.assertIn("target_id = aws_organizations_organizational_unit.fcvm_fleet.id", att)

    def test_nothing_here_trusts_the_fleet_account(self):
        # Fleet-owned resources may name their own account. New main-account
        # factory role trusts are checked separately below, not exempted.
        for f in ROOT.glob("*.tf"):
            if f.name in {"fcvm-fleet-account.tf", "fcvm-fleet.tf",
                          "fcvm-fleet-ami-release.tf", "fcvm-fleet-ami-pipeline.tf"}:
                continue
            if f.name in {"fcvm-fleet-alarms.tf", "fcvm-fleet-budget.tf", "fcvm-fleet-prod.tf", "fcvm-fleet-prod-dns.tf",
                          "fcvm-fleet-prod-broadcast.tf", "fcvm-fleet-prod-alarms.tf", "fcvm-fleet-prod-drills.tf", "fcvm-fleet-lambda.tf"}:
                # Fleet-owned: every resource that names the fleet account is created inside it.
                for r in re.finditer(r'^resource "\w+" "\w+" \{\n.*?^\}', f.read_text(), re.S | re.M):
                    if "local.fcvm_fleet_account_id" in r.group():
                        self.assertRegex(r.group(), r"(?m)^  provider\s+= aws\.fcvm_fleet(_\w+)?$", f.name)
                self.assertNotIn("aws_organizations_account.fcvm_fleet", f.read_text(), f.name)
                continue
            text = f.read_text()
            self.assertNotIn("aws_organizations_account.fcvm_fleet", text, f.name)
            self.assertNotIn("local.fcvm_fleet_account_id", text, f.name)

    def test_factory_main_roles_do_not_trust_fleet_principals(self):
        factory = (ROOT / "fcvm-fleet-ami-pipeline.tf").read_text()
        expected = {
            "fcvm_ami_dispatch": '{ Service = "lambda.amazonaws.com" }',
            "fcvm_ami_codebuild": '{ Service = "codebuild.amazonaws.com" }',
            "fcvm_ami_ci": "{ Federated = aws_iam_openid_connect_provider.github.arn }",
            "fcvm_ami_publish": '{ Service = "lambda.amazonaws.com" }',
        }
        actual = {}
        for match in re.finditer(r'^resource "aws_iam_role" "(\w+)" \{\n.*?^\}', factory, re.S | re.M):
            body = match.group()
            if re.search(r"provider\s*=\s*aws\.fcvm_fleet\w*\b", body):
                continue  # These roles are in Fleet and trust the scoped main broker.
            actual[match.group(1)] = re.findall(r'\bPrincipal\s*=\s*(\{[^}]+\}|"[^"]+")', body)
        self.assertEqual(actual, {name: [principal] for name, principal in expected.items()})
        release = (ROOT / "fcvm-fleet-ami-release.tf").read_text()
        self.assertNotRegex(release, r'\bPrincipal\s*=|^resource "aws_iam_role" ',)


class ApplyRoleTests(unittest.TestCase):
    def test_the_apply_role_trusts_only_fcvm_arm(self):
        role = block('resource "aws_iam_role" "fcvm_fleet_terraform"')
        self.assertEqual(role.count("Principal"), 1)
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", role)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', role)
        self.assertIn("max_session_duration = 3600", role)

    def test_the_dev_box_grant_names_exactly_that_role_and_the_pointer(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"', CONST)
        for sid, res in (("AssumeTheFcvmFleetApplyRoleOnly", "aws_iam_role.fcvm_fleet_terraform.arn"),
                         ("ReadTheFcvmFleetPointerOnly", "aws_ssm_parameter.fcvm_fleet.arn")):
            stmt = grant.split(f'Sid       = "{sid}"', 1)[1].split("}", 2)[0] + grant.split(f'Sid       = "{sid}"', 1)[1].split("}", 2)[1]
            self.assertIn(f"Resource  = {res}", stmt, sid)
            self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', stmt, sid)

    def test_state_objects_are_for_the_appliers_only(self):
        pol = block('resource "aws_s3_bucket_policy" "fcvm_fleet_terraform_state"')
        self.assertIn("StateObjectsForAppliersOnly", pol)
        self.assertIn("[local.fcvm_fleet_terraform_role, local.fcvm_fleet_access_role_arn]", pol)
        self.assertIn('status = "Enabled"', block('resource "aws_s3_bucket_versioning" "fcvm_fleet_terraform_state"'))
        self.assertIn("prevent_destroy = true", block('resource "aws_s3_bucket" "fcvm_fleet_terraform_state"'))

    def test_the_pointer_holds_no_credential(self):
        param = block('resource "aws_ssm_parameter" "fcvm_fleet"')
        self.assertIn('name        = "/infra/fcvm-fleet"', param)
        self.assertIn('type        = "String"', param)
        for leak in ("secret", "token", "password", "key_id"):
            self.assertNotIn(leak, param.lower().replace("no credentials", ""))


class GpuboxRoleTests(unittest.TestCase):
    """ejc3/aws#922: fcvm-arm's role in the fleet account reaches gpuboxes and nothing that holds or uses a secret."""
    ALLOWED = {"ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeInstanceTypes",
               "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation",
               "servicequotas:GetServiceQuota", "ssm:GetParameter", "ec2:RunInstances", "ec2:CreateTags", "ec2:TerminateInstances",
               "ssm:SendCommand", "iam:PassRole", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"}

    def statements(self):
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_terraform_gpubox"')
        self.assertIn("role     = aws_iam_role.fcvm_fleet_terraform.name", pol)
        return {m.group(1): m.group(0) for m in re.finditer(r'Sid\s+= "(\w+)".*?\n      \}', pol, re.S)}

    def actions(self, stmt):
        field = re.search(r'\bAction\s+= (\[[^\]]*\]|"[^"]+")', stmt, re.S)
        return set(re.findall(r'"([a-z0-9]+:[A-Za-z*]+)"', field.group(1))) if field else set()

    def test_no_administrator_access_and_no_other_policy_on_the_role(self):
        self.assertNotIn("policy/AdministratorAccess", TF)
        attached = set()
        for path in ROOT.glob("*.tf"):
            source = path.read_text()
            for header in re.findall(r'resource "aws_iam_role_policy(?:_attachment)?" "[^"]+"', source):
                if re.search(r'\brole\s*=\s*aws_iam_role\.fcvm_fleet_terraform\.(?:name|id)', block(header, source)):
                    attached.add(header.split('"')[3])
        self.assertEqual(attached, {
            "fcvm_fleet_terraform_gpubox", "fcvm_fleet_terraform_g_admission", "fcvm_fleet_terraform_observe",
            "fcvm_fleet_operate_budget_drill", "fcvm_fleet_terraform_ops", "fcvm_fleet_terraform_lambda",
        })

    def test_every_allowed_action_is_a_gpubox_call(self):
        stmts = self.statements()
        allowed = set().union(*(self.actions(s) for s in stmts.values() if re.search(r'Effect\s+= "Allow"', s)))
        self.assertLessEqual(allowed, self.ALLOWED)
        self.assertNotRegex("".join(s for s in stmts.values() if re.search(r'Effect\s+= "Allow"', s)), r'"[a-z0-9]+:\*"')

    def test_instance_actions_need_the_gpubox_tag(self):
        s = self.statements()
        self.assertIn('"aws:RequestTag/Purpose" = local.fcvm_fleet_gpubox_purpose', s["LaunchOnlyGpuboxes"])
        self.assertIn('"ec2:ResourceTag/Purpose" = local.fcvm_fleet_gpubox_purpose', s["EndOnlyGpuboxes"])
        self.assertIn('"ssm:resourceTag/Purpose" = local.fcvm_fleet_gpubox_purpose', s["RunCommandsOnlyOnGpuboxes"])
        self.assertIn('"ec2:CreateAction" = "RunInstances"', s["TagAtLaunch"])
        self.assertRegex(s["TheShellDocument"], r'Resource\s+= "arn:aws:ssm:\*::document/AWS-RunShellScript"')
        self.assertIn('fcvm_fleet_gpubox_purpose = "fcvm-fleet-gpubox"', TF)

    def test_only_the_lease_tag_can_be_changed_after_launch(self):
        statements = self.statements()
        tag_writers = {sid for sid, stmt in statements.items() if "ec2:CreateTags" in self.actions(stmt)}
        self.assertEqual(tag_writers, {"TagAtLaunch", "ExtendOnlyGpuboxLeases"})
        for sid, keys in (("TagAtLaunch", {"Purpose", "Name", "Owner", "LeaseUntil", "Market", "fcvm-admission"}),
                          ("ExtendOnlyGpuboxLeases", {"LeaseUntil"})):
            stmt = statements[sid]
            self.assertIn(':instance/*"', stmt)
            allowed = re.search(r'"ForAllValues:StringEquals"\s*=\s*\{ "aws:TagKeys" = \[(.*?)\]', stmt)
            self.assertIsNotNone(allowed)
            self.assertEqual(set(re.findall(r'"([^"]+)"', allowed[1])), keys)
        self.assertIn('"ec2:ResourceTag/Purpose" = local.fcvm_fleet_gpubox_purpose', statements["ExtendOnlyGpuboxLeases"])

    def test_ssm_agent_core_cannot_read_unrelated_parameters(self):
        attachment = block('resource "aws_iam_role_policy_attachment" "fcvm_fleet_gpubox_ssm"')
        self.assertIn("aws_iam_role.fcvm_fleet_gpubox.name", attachment)
        self.assertIn("policy/AmazonSSMManagedInstanceCore", attachment)
        policy = block('resource "aws_iam_role_policy" "fcvm_fleet_gpubox"')
        self.assertIn("aws_iam_role.fcvm_fleet_gpubox.id", policy)
        deny = re.search(r'Sid\s+= "DenyUnrelatedParameters"(.*?)\n      \}', policy, re.S)
        self.assertIsNotNone(deny)
        self.assertRegex(deny[1], r'Effect\s+= "Deny"')
        self.assertRegex(deny[1], r'Action\s+= "ssm:GetParameter\*"')
        exceptions = re.search(r'NotResource\s+= \[(.*?)\]', deny[1], re.S)
        self.assertEqual(set(re.findall(r'"([^"]+)"', exceptions[1])), {
            "arn:aws:ssm:*::parameter/aws/service/*",
            "arn:aws:ssm:us-east-1:${local.fcvm_fleet_account_id}:parameter/fcvm-fleet/ami-release",
            "arn:aws:ssm:us-east-1:${local.fcvm_fleet_account_id}:parameter/fcvm-fleet/ami-release-copies",
        })

    def test_it_passes_only_the_gpubox_role_and_writes_only_the_gpubox_prefix(self):
        s = self.statements()
        self.assertRegex(s["PassOnlyTheGpuboxRole"], r"Resource\s+= aws_iam_role\.fcvm_fleet_gpubox\.arn\n")
        self.assertRegex(s["PassOnlyTheGpuboxRole"], r'"iam:PassedToService" = "ec2.amazonaws.com"')
        self.assertRegex(s["TheGpuboxPrefix"], r'Resource\s+= "\$\{aws_s3_bucket\.fcvm_fleet_assets\.arn\}/gpubox/\*"')
        for sid, stmt in s.items():
            if "iam:PassRole" in stmt:
                self.assertEqual(sid, "PassOnlyTheGpuboxRole")

    def test_secrets_iam_and_what_workloads_run_are_denied_by_name(self):
        deny = self.statements()["NeverSecretsIamOrWhatWorkloadsRun"]
        self.assertRegex(deny, r'Effect\s+= "Deny"')
        for action in ("secretsmanager:*", "iam:Put*", "iam:Attach*", "iam:Update*", "ssm:StartSession",
                       "ec2:CreateLaunchTemplateVersion", "ec2:ModifyLaunchTemplate", "ec2:ModifyInstanceAttribute"):
            self.assertIn(f'"{action}"', deny)
        self.assertNotIn('"ssm:PutParameter"', deny)
        scoped_deny = self.statements()["NeverWriteOtherParameters"]
        self.assertRegex(scoped_deny, r'Effect\s+= "Deny"')
        self.assertEqual(self.actions(scoped_deny), {"ssm:PutParameter"})
        self.assertRegex(scoped_deny, r'NotResource\s+= local\.fcvm_fleet_ops_parameters')

    def test_a_gpubox_reads_no_secret(self):
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_gpubox"')
        self.assertNotIn("secretsmanager", pol)
        self.assertIn("role     = aws_iam_role.fcvm_fleet_gpubox.name", block('resource "aws_iam_instance_profile" "fcvm_fleet_gpubox"'))
        self.assertIn('name     = "fcvm-fleet-gpubox"', block('resource "aws_iam_instance_profile" "fcvm_fleet_gpubox"'))


class ProjectOperationsTests(unittest.TestCase):
    def setUp(self):
        policy = block('resource "aws_iam_role_policy" "fcvm_fleet_terraform_ops"')
        self.statements = dict(re.findall(r'Sid\s*= "([^"]+)"(.*?)\n      \},', policy, re.S))
        self.assertEqual(set(self.statements), {
            "ListOperationsState", "ReadWriteCurrentState", "LockCurrentState", "DiscoverOperationsMetadata",
            "ManageOperationsParameters", "ManageOperationsLogs", "ReadPendingRegionalGSpotQuota",
        })

    def assert_grant(self, sid, actions, resource):
        statement = self.statements[sid]
        action = re.search(r'\bAction\s*=\s*("[^"]+"|\[[^\]]*\])', statement, re.S)
        self.assertEqual(set(re.findall(r'"([^"]+)"', action[1])), set(actions), sid)
        self.assertRegex(statement, r'\bEffect\s*=\s*"Allow"')
        self.assertEqual(re.search(r'\bResource\s*=\s*([^\n]+)', statement)[1], resource)

    def test_only_current_state_and_lock_are_accessible(self):
        self.assertRegex(TF, r'fcvm_fleet_ops_key\s*= "fleet-ops/terraform.tfstate"')
        self.assertRegex(TF, r'fcvm_fleet_ops_state\s*= "\$\{aws_s3_bucket.fcvm_fleet_terraform_state\.arn\}/\$\{local.fcvm_fleet_ops_key\}"')
        self.assert_grant("ReadWriteCurrentState", ["s3:GetObject", "s3:PutObject"], "local.fcvm_fleet_ops_state")
        self.assert_grant("LockCurrentState", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                          '"${local.fcvm_fleet_ops_state}.tflock"')
        self.assert_grant("ListOperationsState", ["s3:ListBucket"], "aws_s3_bucket.fcvm_fleet_terraform_state.arn")
        self.assertIn('"s3:prefix" = [local.fcvm_fleet_ops_key, "fleet-ops/workspaces/"]', self.statements["ListOperationsState"])
        seed = block('resource "aws_s3_object" "fcvm_fleet_ops_initial_state"')
        self.assertIn("aws_s3_bucket_versioning.fcvm_fleet_terraform_state", seed)
        self.assertRegex(seed, r'key\s*= local.fcvm_fleet_ops_key')
        self.assertRegex(seed, r'ignore_changes\s*= all')
        self.assertIn("prevent_destroy = true", seed)

    def test_operations_writes_and_content_reads_stay_in_new_namespaces(self):
        self.assert_grant("ManageOperationsParameters", [
            "ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter",
            "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource",
        ], "local.fcvm_fleet_ops_parameters")
        self.assertRegex(TF, r'fcvm_fleet_ops_parameters\s*= "arn:aws:ssm:\$\{local.fcvm_fleet_home_region\}:\$\{local.fcvm_fleet_account_id\}:parameter/fcvm/ops/\*"')
        self.assert_grant("ManageOperationsLogs", [
            "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
            "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource",
            "logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents", "logs:GetLogEvents", "logs:FilterLogEvents",
        ], "local.fcvm_fleet_ops_logs")
        self.assertRegex(TF, r'fcvm_fleet_ops_logs\s*= "arn:aws:logs:\$\{local.fcvm_fleet_home_region\}:\$\{local.fcvm_fleet_account_id\}:log-group:/fcvm/ops/\*"')
        self.assert_grant("DiscoverOperationsMetadata", ["logs:DescribeLogGroups", "ssm:DescribeParameters"], '"*"')
        self.assertIn('"aws:RequestedRegion" = local.fcvm_fleet_home_region', self.statements["DiscoverOperationsMetadata"])
        self.assert_grant("ReadPendingRegionalGSpotQuota", ["servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota"],
                          '"arn:aws:servicequotas:us-east-1:${local.fcvm_fleet_account_id}:ec2/L-3819A6DF"')


class ObserveTests(unittest.TestCase):
    """fcvm-arm reads the coordinators and runs the Spot drill (#942); nothing that names a secret, no other write."""

    def test_arm_invokes_only_the_guard_and_has_no_fis_actions(self):
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_terraform_observe"')
        self.assertIn("role     = aws_iam_role.fcvm_fleet_terraform.name", pol)
        action_fields = re.findall(r'\bAction\s*=\s*(\[[^\]]*\]|"[^"]+")', pol)
        actions = {name for field in action_fields for name in re.findall(r'"([^"]+)"', field)}
        self.assertEqual(actions, {"ecs:DescribeClusters", "ecs:DescribeServices", "ecs:ListServices", "ecs:ListTasks",
                                   "ecs:DescribeTasks", "logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams",
                                   "lambda:InvokeFunction"})
        self.assertIn("for broker in aws_lambda_function.fcvm_fleet_prod_spot_drill", pol)
        self.assertIn("Resource = broker.arn", pol)

    def test_drill_guard_serializes_its_receipt_and_stops_only_its_experiments(self):
        drill = (ROOT / "fcvm-fleet-prod-drills.tf").read_text()
        guard = block('resource "aws_lambda_function" "fcvm_fleet_prod_spot_drill"', drill)
        self.assertIn("reserved_concurrent_executions = 1", guard)
        self.assertIn("aws_s3_object.fcvm_fleet_prod_spot_drill[0].key", guard)
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_spot_drill_guard"', drill)
        self.assertIn('/${local.fcvm_prod_drill_state}', pol)
        self.assertIn('experiment-template/${aws_fis_experiment_template.fcvm_fleet_prod_spot_drill[0].id}', pol)
        stop = pol.split('"fis:StopExperiment"', 1)[1].split('},\n', 1)[0]
        self.assertIn('Resource  = local.fcvm_prod_drill_exp_arn', stop)
        self.assertIn('"aws:ResourceTag/Drill" = "fcvm-fleet-prod-spot"', stop)
        self.assertNotIn('fis:UpdateExperimentTemplate', pol)
        self.assertNotIn('ec2:SendSpotInstanceInterruptions', pol)

    def test_the_drill_interrupts_one_running_prod_host_only(self):
        drill = (ROOT / "fcvm-fleet-prod-drills.tf").read_text()
        linked_role = block('resource "aws_iam_service_linked_role" "fcvm_fleet_fis"', drill)
        self.assertIn('aws_service_name = "fis.amazonaws.com"', linked_role)
        trust = block('resource "aws_iam_role" "fcvm_fleet_prod_spot_drill"', drill)
        self.assertIn('"aws:SourceAccount" = local.fcvm_fleet_account_id', trust)
        self.assertIn('"aws:SourceArn" = "arn:aws:fis:us-east-1:${local.fcvm_fleet_account_id}:experiment/*"', trust)
        role = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_spot_drill"', drill)
        self.assertIn('"ec2:ResourceTag/Fleet"   = "prod"', role)
        self.assertIn('"ec2:ResourceTag/Purpose" = "fcvm-fleet-host"', role)
        self.assertEqual(set(re.findall(r'"(ec2:[A-Za-z]+)"', role)), {"ec2:SendSpotInstanceInterruptions", "ec2:DescribeInstances"})
        tpl = block('resource "aws_fis_experiment_template" "fcvm_fleet_prod_spot_drill"', drill)
        self.assertIn("aws_iam_service_linked_role.fcvm_fleet_fis,", tpl)
        self.assertIn("aws_iam_role_policy.fcvm_fleet_prod_spot_drill,", tpl)
        self.assertIn('selection_mode = "COUNT(1)"', tpl)
        self.assertIn('action_id = "aws:ec2:send-spot-instance-interruptions"', tpl)
        self.assertRegex(tpl, r'key\s+= "Fleet"\n\s+value = "prod"')

    def test_the_dev_box_reads_and_simulates_only_the_fcvm_ci_roles(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"', CONST)
        stmt = grant.split('Sid       = "ReadTheFcvmCiRoles"', 1)[1].split("}, {", 1)[0]
        self.assertEqual(set(re.findall(r'"(iam:[A-Za-z]+)"', stmt)), {"iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies",
                                                                     "iam:ListAttachedRolePolicies", "iam:SimulatePrincipalPolicy"})
        self.assertIn('for r in ["fcvm-stage-ci", "fcvm-prod-ci"]', stmt)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', stmt)

    def test_ecs_actions_are_limited_to_the_two_coordinator_clusters(self):
        pol = block('resource "aws_iam_role_policy" "fcvm_fleet_terraform_observe"')
        clusters = "local.fcvm_fleet_observed_clusters"
        cluster_arns = "local.fcvm_fleet_observed_cluster_arns"
        ecs = "${local.fcvm_fleet_observed_ecs}"
        self.assertRegex(TF, r'fcvm_fleet_observed_clusters\s*= \[local\.fcvm_fleet_stacks\.stage\.prefix, local\.fcvm_fleet_stacks\.prod\.prefix\]')
        self.assertRegex(TF, r'fcvm_fleet_observed_ecs\s*= "arn:aws:ecs:\$\{local\.fcvm_fleet_home_region\}:\$\{local\.fcvm_fleet_account_id\}"')
        self.assertIn(f'[for c in {clusters} : "{ecs}:cluster/${{c}}"]', TF)
        resources = {
            'ecs:DescribeClusters': cluster_arns,
            'ecs:ListServices': '"*"',
            'ecs:DescribeServices': f'[for c in {clusters} : "{ecs}:service/${{c}}/${{c}}-coordinator"]',
            'ecs:ListTasks': f'[for c in {clusters} : "{ecs}:container-instance/${{c}}/*"]',
            'ecs:DescribeTasks': f'[for c in {clusters} : "{ecs}:task/${{c}}/*"]',
        }
        for action, resource in resources.items():
            with self.subTest(action=action):
                stmt = next(s for s in pol.split('\n      },') if f'"{action}"' in s)
                self.assertRegex(stmt, r'Action\s+= "' + re.escape(action) + '"')
                self.assertRegex(stmt, r'(?m)^ *Resource\s+= ' + re.escape(resource) + r'$')
                if action.startswith('ecs:List'):
                    self.assertIn('ArnEquals = { "ecs:cluster" = ' + cluster_arns + ' }', stmt)
        self.assertIn(f'[for c in {clusters} :\n        "arn:aws:logs:${{local.fcvm_fleet_home_region}}:'
                      '${local.fcvm_fleet_account_id}:log-group:/ecs/${c}-coordinator:*"]', pol)

    def test_the_dev_box_reads_only_the_two_prod_release_log_groups_in_the_management_region(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"', CONST)
        stmt = grant.split('"ReadTheFcvmReleaseLogs"', 1)[1].split("}, {", 1)[0]
        self.assertEqual(set(re.findall(r'"(logs:[A-Za-z]+)"', stmt)), {"logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"})
        resource = re.search(r'Resource\s+= \[(.*?)\]', stmt, re.S).group(1)
        self.assertEqual(set(re.findall(r'"([^"]+)"', resource)), {
            'arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/' + name + ':*'
            for name in ('fcvm-coordinator-release-prod', 'fcvm-fleet-ami-prod-release')
        })
        self.assertNotIn("lambda:", stmt)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', stmt)


class LambdaGrantTests(unittest.TestCase):
    source = (ROOT / "fcvm-fleet-lambda.tf").read_text()
    coordinator = block('resource "aws_iam_policy" "fcvm_lambda_coordinator"', source)
    push = block('resource "aws_iam_policy" "fcvm_fleet_terraform_lambda"', source)

    def statement(self, policy, sid):
        return re.split(r'Sid\s+= "' + sid + '"', policy, maxsplit=1)[1].split("},", 1)[0]

    def test_the_coordinator_runs_only_its_own_stacks_functions(self):
        # Exactly what ejc3/fcvm's LambdaDriver calls, on fcvm-lam-<fleet>-* only.
        fns = self.statement(self.coordinator, "ItsTileFunctions")
        self.assertCountEqual(re.findall(r'"(lambda:[^"]+)"', fns), [
            "lambda:GetFunctionConfiguration", "lambda:CreateFunction", "lambda:UpdateFunctionConfiguration",
            "lambda:PublishVersion", "lambda:DeleteFunction", "lambda:TagResource",
        ])
        self.assertIn("Resource = [local.fcvm_lambda_function_arns[each.key]]", fns)
        self.assertIn('function:fcvm-lam-${s.fleet}-*"', self.source)
        self.assertNotRegex(self.coordinator, r'lambda:(?:\*|ListFunctions|.*FunctionUrl.*|AddPermission|InvokeFunction|(?:Create|Update|Delete)CapacityProvider)'
                            r'|iam:(?:Create|Update|Put|Attach)|ecr:(?:Put|Set|Delete|Create)')

    def test_each_stack_passes_only_its_own_role_and_provider(self):
        cap = self.statement(self.coordinator, "ItsCapacityProvider")
        self.assertIn('Action   = "lambda:PassCapacityProvider"', cap)
        self.assertIn('Resource = aws_cloudformation_stack.fcvm_lambda[each.key].outputs["CapacityProviderArn"]', cap)
        role = self.statement(self.coordinator, "ItsExecutionRole")
        self.assertIn('Action    = "iam:PassRole"', role)
        self.assertIn("Resource  = aws_iam_role.fcvm_lambda_tile[each.key].arn", role)
        self.assertIn('"iam:PassedToService" = "lambda.amazonaws.com"', role)
        att = block('resource "aws_iam_role_policy_attachment" "fcvm_lambda_coordinator"', self.source)
        self.assertIn("role       = each.value.coordinator_role", att)
        self.assertIn("depends_on = [aws_iam_policy.fcvm_fleet_spend_cap]", att)
        self.assertIn("coordinator_role = aws_iam_role.fcvm_fleet_coordinator_task.name", self.source)
        self.assertIn("coordinator_role = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].name", self.source)

    def test_fcvm_arm_only_pushes_the_tile_image(self):
        self.assertCountEqual(re.findall(r'"((?:ecr|lambda|iam|logs):[^"]+)"', self.push), [
            "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
            "ecr:PutImage", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:GetAuthorizationToken",
        ])
        self.assertIn("Resource = aws_ecr_repository.fcvm_fleet_lambda_tile.arn", self.push)
        att = block('resource "aws_iam_role_policy_attachment" "fcvm_fleet_terraform_lambda"', self.source)
        self.assertIn("role       = aws_iam_role.fcvm_fleet_terraform.name", att)

    def test_the_ceiling_covers_three_instances_a_tile_and_only_terraform_sets_it(self):
        provider = block('resource "aws_cloudformation_stack" "fcvm_lambda"', self.source)
        self.assertIn("for_each = local.fcvm_lambda_stacks", provider)
        self.assertRegex(provider, r'AllowedInstanceTypes\s*= \[local.fcvm_lambda_instance_type\]')
        self.assertRegex(self.source, r'fcvm_lambda_instance_type\s*= "g4dn.xlarge"')
        self.assertRegex(provider, r'ScalingMode\s*= "Auto"')
        self.assertRegex(provider, r'MaxVCpuCount\s*= local.fcvm_lambda_max_vcpus')
        self.assertIn("fcvm_lambda_max_tiles = 4", self.source)
        self.assertIn("fcvm_lambda_max_vcpus = local.fcvm_lambda_max_tiles * 3 * 4", self.source)
        self.assertIn('CapacityProviderOperatorRoleArn = aws_iam_role.fcvm_fleet_lambda_operator.arn', provider)
        self.assertRegex(provider, r'SecurityGroupIds\s*= \[aws_security_group.fcvm_lambda_tile\[each.key\].id\]')
        self.assertNotRegex(self.source, r'lambda:(?:Create|Update|Delete)CapacityProvider')
        self.assertNotIn("fcvm_fleet_spike", self.source)
        self.assertRegex(block('resource "aws_ssm_parameter" "fcvm_fleet"'), r'gpu_lambda\s*= local.fcvm_fleet_gpu_lambda')
        # The instances are on-demand G in us-east-1: both stacks' ceilings are in the quota.
        self.assertRegex(TF, r"fcvm_fleet_on_demand_g_vcpus\s*= local\.fcvm_fleet_on_demand_g_base_vcpus \+ 2 \* local\.fcvm_lambda_max_vcpus")

    def test_managed_gpu_resources_carry_their_stacks_existing_budget_tags(self):
        provider = block('resource "aws_cloudformation_stack" "fcvm_lambda"', self.source)
        self.assertIn('Type = "AWS::Lambda::CapacityProvider"', provider)
        self.assertNotIn('resource "aws_lambda_capacity_provider"', self.source)
        self.assertRegex(provider, r'PropagateTags = \{\s+Mode\s*= "Explicit"\s+ExplicitTags\s*= local.fcvm_lambda_billing_tags\[each.key\]\s+\}')
        self.assertRegex(provider, r'\bTags\s*= local.fcvm_lambda_billing_tags\[each.key\]')
        self.assertRegex(self.source, r'fcvm_lambda_billing_tags = \{\s+for k, s in local.fcvm_lambda_stacks : k => \[\s+'
                         r'\{ Key = "Purpose", Value = "fcvm-fleet-tile" \},\s+\{ Key = "Fleet", Value = s.fleet \}')
        self.assertIn('"Fn::GetAtt" = ["Provider", "Arn"]', provider)
        self.assertIn('aws_cloudformation_stack.fcvm_lambda[k].outputs["CapacityProviderArn"]', self.source)

    def test_tiles_only_dial_out(self):
        sg = block('resource "aws_security_group" "fcvm_lambda_tile"', self.source)
        self.assertNotIn("ingress", sg)
        self.assertIn("vpc_id      = each.value.vpc_id", sg)
        egress = sg.split("egress {", 1)[1]
        self.assertIn("from_port        = 443", egress)
        self.assertIn("to_port          = 443", egress)
        self.assertIn('ipv6_cidr_blocks = ["::/0"]', egress)

    def test_gpu_subnets_must_be_offered_and_supported_by_lambda(self):
        zones = block('data "aws_availability_zones" "fcvm_lambda"', self.source)
        self.assertIn('exclude_zone_ids = ["use1-az3"]', zones)
        offerings = block('data "aws_ec2_instance_type_offerings" "fcvm_lambda_gpu"', self.source)
        self.assertIn('location_type = "availability-zone"', offerings)
        self.assertIn('values = [local.fcvm_lambda_instance_type]', offerings)
        self.assertIn('aws.fcvm_fleet_east1', zones)
        self.assertIn('aws.fcvm_fleet_east1', offerings)
        selection = self.source.split("fcvm_lambda_subnets = {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("for az, id in s.subnet_ids_by_az : id", selection)
        self.assertIn("if contains(data.aws_availability_zones.fcvm_lambda.names, az)", selection)
        self.assertIn("&& contains(data.aws_ec2_instance_type_offerings.fcvm_lambda_gpu.locations, az)", selection)
        self.assertIn("subnet_ids_by_az = module.fcvm_fleet_east1.subnet_ids_by_az", self.source)
        self.assertIn("for m in module.fcvm_fleet_prod_east1 : \"prod\"", self.source)
        provider = block('resource "aws_cloudformation_stack" "fcvm_lambda"', self.source)
        self.assertRegex(provider, r'SubnetIds\s*= local.fcvm_lambda_subnets\[each.key\]')
        self.assertIn("length(local.fcvm_lambda_subnets[each.key]) > 0", provider)

    def test_a_tile_reads_its_own_stacks_certificate_and_writes_its_own_logs(self):
        runtime = block('resource "aws_iam_role_policy" "fcvm_lambda_tile"', self.source)
        self.assertIn('["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]', runtime)
        self.assertIn("Resource = local.fcvm_lambda_repositories[each.key].arn", runtime)
        self.assertIn("log-group:/aws/lambda/fcvm-lam-${each.value.fleet}-*:log-stream:*", runtime)
        self.assertIn('Action   = "secretsmanager:GetSecretValue"', runtime)
        self.assertIn("Resource = each.value.tls_secret_arn", runtime)
        self.assertNotIn('Resource = "*"', runtime)
        self.assertNotIn("logs:CreateLogGroup", runtime)
        self.assertIn("tls_secret_arn   = aws_secretsmanager_secret.fcvm_fleet_stage_tls.arn", self.source)
        self.assertIn("tls_secret_arn   = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn", self.source)
        # Each certificate's deny-by-default policy admits its own stack's tile role and no other.
        stage = block('resource "aws_secretsmanager_secret_policy" "fcvm_fleet_stage_tls"', (ROOT / "fcvm-fleet-stage-cert.tf").read_text())
        prod = block('resource "aws_secretsmanager_secret_policy" "fcvm_fleet_prod_tls"', (ROOT / "fcvm-fleet-prod.tf").read_text())
        self.assertIn("local.fcvm_lambda_tile_role_arns.stage,", stage)
        self.assertNotIn("fcvm_lambda_tile_role_arns.prod", stage)
        self.assertIn("local.fcvm_lambda_tile_role_arns.prod,", prod)
        self.assertNotIn("fcvm_lambda_tile_role_arns.stage", prod)
        logs = block('resource "aws_cloudwatch_log_group" "fcvm_lambda_tile"', self.source)
        self.assertIn("retention_in_days = 14", logs)
        self.assertIn('name              = "/aws/lambda/fcvm-lam-${each.value}-${split("-", each.key)[1]}"', logs)

    def test_only_lambda_pulls_the_tile_image_for_the_tile_functions(self):
        repo_policy = block('resource "aws_ecr_repository_policy" "fcvm_fleet_lambda_tile"', self.source)
        self.assertIn('repository = aws_ecr_repository.fcvm_fleet_lambda_tile.name', repo_policy)
        self.assertIn('aws.fcvm_fleet_east1', repo_policy)
        self.assertIn('Principal = { Service = "lambda.amazonaws.com" }', repo_policy)
        self.assertIn('Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]', repo_policy)
        self.assertIn('"aws:sourceArn" = local.fcvm_lambda_function_arns.stage', repo_policy)
        self.assertIn('Sid       = "LambdaPullsTheTileImage"', repo_policy)
        # The spike's repository is adopted, not recreated.
        self.assertIn('import {\n  to = aws_ecr_repository.fcvm_fleet_lambda_tile\n  id = "fcvm-fleet/lambda-tile"\n}', self.source)

    def test_staging_ci_pushes_and_records_the_lane_image_and_deletes_nothing(self):
        # ejc3/fcvm .github/workflows/fleet-lambda-image.yml: the fleet-stage-deploy job pushes rev-<commit> and moves
        # keep-built (fleet/lambda/release.py publish): push, read the image back, put a tag. Never delete an image.
        ci = (ROOT / "fcvm-fleet-ci.tf").read_text()
        role = block('resource "aws_iam_role_policy" "fcvm_stage_ci"', ci)
        lane = self.statement(role, "PushAndRecordTheLaneImage")
        self.assertCountEqual(re.findall(r'"(ecr:[^"]+)"', lane), [
            "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
            "ecr:PutImage", "ecr:DescribeImages", "ecr:BatchGetImage",
        ])
        self.assertIn("Resource = aws_ecr_repository.fcvm_fleet_lambda_tile.arn", lane)
        repo_policy = block('resource "aws_ecr_repository_policy" "fcvm_fleet_lambda_tile"', self.source)
        ci_pull = self.statement(repo_policy, "StagingCiPushesTheLaneImage")
        self.assertIn("Principal = { AWS = aws_iam_role.fcvm_stage_ci.arn }", ci_pull)
        self.assertCountEqual(re.findall(r'"(ecr:[^"]+)"', ci_pull), re.findall(r'"(ecr:[^"]+)"', lane))
        self.assertNotRegex(role + repo_policy, r"ecr:(?:BatchDeleteImage|Delete|Set|Create|\*)")
        secrets = block('resource "github_actions_environment_secret" "fcvm_stage"', ci)
        self.assertIn('"LAMBDA_TILE_REPO"', secrets)
        self.assertIn("LAMBDA_TILE_REPO        = aws_ecr_repository.fcvm_fleet_lambda_tile.repository_url", secrets)

    def test_an_image_a_stack_runs_or_rolls_back_to_is_never_expired(self):
        # ejc3/fcvm fleet/lambda/release.py moves the keep- tags (each stack's current, previous, next); they are the only
        # mutable tags, and its LIFECYCLE (whose tests model this policy) is the policy below.
        self.assertRegex(self.source, r'fcvm_lambda_protected_tags\s+= "keep-\*"')
        repo = block('resource "aws_ecr_repository" "fcvm_fleet_lambda_tile"', self.source)
        self.assertIn('image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"', repo)
        self.assertRegex(repo, r'image_tag_mutability_exclusion_filter \{\s+filter\s+= local.fcvm_lambda_protected_tags\s+'
                         r'filter_type = "WILDCARD"\s+\}')
        policy = block('resource "aws_ecr_lifecycle_policy" "fcvm_fleet_lambda_tile"', self.source)
        rules = re.findall(r"rulePriority = (\d+)(.*?)action\s+= \{ type = \"expire\" \}", policy, re.S)
        self.assertEqual([p for p, _ in rules], ["1", "2"])
        # Rule 1 selects every protected image and expires none: fewer than ten carry a protected tag. An image a rule's tags
        # select is never expired by a lower-priority rule.
        self.assertRegex(rules[0][1], r'tagStatus\s+= "tagged"\s+tagPatternList = \[local.fcvm_lambda_protected_tags\]\s+'
                         r'countType\s+= "imageCountMoreThan"\s+countNumber\s+= 100\s+\}')
        # Rule 2 keeps cleaning up: every image outside the newest five that no protected tag holds.
        self.assertIn('selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }', rules[1][1])


class LaneRepositoryPerStackTests(unittest.TestCase):
    """One lane repository per stack (owner, 2026-11-23): staging's fcvm-fleet/lambda-tile and prod's
    fcvm-fleet/lambda-tile-prod. Each admits only its own stack's roles and its own stack's Lambda pull, and nothing in
    staging names the production repository."""
    source = (ROOT / "fcvm-fleet-lambda.tf").read_text()
    STAGE_REPO = "aws_ecr_repository.fcvm_fleet_lambda_tile."
    PROD_REPO = "aws_ecr_repository.fcvm_fleet_lambda_tile_prod"

    def test_production_has_its_own_repository_only_while_it_is_enabled(self):
        self.assertRegex(self.source, r'fcvm_lambda_prod_repo_name\s+= "fcvm-fleet/lambda-tile-prod"')
        repo = block('resource "aws_ecr_repository" "fcvm_fleet_lambda_tile_prod"', self.source)
        self.assertIn("count                = local.fcvm_fleet_prod", repo)
        self.assertIn("name                 = local.fcvm_lambda_prod_repo_name", repo)
        self.assertIn('image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"', repo)
        self.assertRegex(repo, r'image_tag_mutability_exclusion_filter \{\s+filter\s+= local.fcvm_lambda_prod_protected_tags\s+'
                         r'filter_type = "WILDCARD"\s+\}')
        self.assertIn("scan_on_push = true", repo)
        self.assertRegex(self.source, r'fcvm_lambda_prod_protected_tags\s+= "keep-prod-\*"')
        policy = block('resource "aws_ecr_lifecycle_policy" "fcvm_fleet_lambda_tile_prod"', self.source)
        self.assertIn("repository = aws_ecr_repository.fcvm_fleet_lambda_tile_prod[0].name", policy)
        self.assertRegex(policy, r'tagStatus\s+= "tagged"\s+tagPatternList = \[local.fcvm_lambda_prod_protected_tags\]\s+'
                         r'countType\s+= "imageCountMoreThan"\s+countNumber\s+= 100\s+\}')
        self.assertIn('selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }', policy)

    def test_each_repository_admits_only_its_own_stacks_lambda_pull(self):
        stage = block('resource "aws_ecr_repository_policy" "fcvm_fleet_lambda_tile"', self.source)
        prod = block('resource "aws_ecr_repository_policy" "fcvm_fleet_lambda_tile_prod"', self.source)
        self.assertIn('"aws:sourceArn" = local.fcvm_lambda_function_arns.stage', stage)
        self.assertIn('"aws:sourceArn" = local.fcvm_lambda_function_arns.prod', prod)
        self.assertIn("count      = local.fcvm_fleet_prod", prod)
        self.assertIn("repository = aws_ecr_repository.fcvm_fleet_lambda_tile_prod[0].name", prod)
        self.assertNotIn("fcvm_lambda_function_arns.prod", stage)
        self.assertNotIn("values(local.fcvm_lambda_function_arns)", self.source)
        # Staging's repository admits staging's CI; prod's admits nothing of staging's.
        self.assertIn("Principal = { AWS = aws_iam_role.fcvm_stage_ci.arn }", stage)
        self.assertNotRegex(prod, r"fcvm_stage_ci|fcvm_fleet_coordinator_task\b|fcvm_fleet_terraform")
        # Two statements: prod's Lambda functions pull, and prod CI copies the tested image in and moves keep-prod-*
        # (#1002). The only AWS principal is prod CI's role, with no delete or policy action; no wildcard.
        self.assertEqual(prod.count("Effect"), 2)
        self.assertEqual(re.findall(r"AWS = ([^ }]+)", prod), ["aws_iam_role.fcvm_prod_ci[0].arn"])
        self.assertNotRegex(prod, r'Principal\s*=\s*"\*"')
        self.assertNotRegex(prod, r'ecr:(BatchDelete|Delete|Set|Put(Lifecycle|Registry|Replication|ImageTag|ImageScanning))')

    def test_a_stack_pulls_only_from_its_own_repository(self):
        self.assertRegex(self.source, r'fcvm_lambda_repositories = merge\(\s+\{ stage = aws_ecr_repository.fcvm_fleet_lambda_tile \},\s+'
                         r'\{ for r in aws_ecr_repository.fcvm_fleet_lambda_tile_prod : "prod" => r \},\s+\)')
        runtime = block('resource "aws_iam_role_policy" "fcvm_lambda_tile"', self.source)
        self.assertIn("Resource = local.fcvm_lambda_repositories[each.key].arn", runtime)
        coordinator = block('resource "aws_iam_policy" "fcvm_lambda_coordinator"', self.source)
        pull = coordinator.split('Sid      = "PullTheTileImage"', 1)[1].split("},", 1)[0]
        self.assertIn("Resource = local.fcvm_lambda_repositories[each.key].arn", pull)
        self.assertNotIn(self.STAGE_REPO + "arn", runtime + coordinator)

    def test_staging_has_no_statement_on_the_production_repository(self):
        ci = (ROOT / "fcvm-fleet-ci.tf").read_text()
        stage_ci = block('resource "aws_iam_role_policy" "fcvm_stage_ci"', ci)
        self.assertNotIn(self.PROD_REPO, stage_ci)
        self.assertNotIn("lambda-tile-prod", stage_ci)
        secrets = block('resource "github_actions_environment_secret" "fcvm_stage"', ci)
        self.assertNotIn(self.PROD_REPO, secrets)
        # fcvm-arm's hand push names staging's repository only.
        push = block('resource "aws_iam_policy" "fcvm_fleet_terraform_lambda"', self.source)
        self.assertNotIn(self.PROD_REPO, push)

    def test_the_fleet_parameter_names_each_stacks_repository(self):
        lane = self.source.split("fcvm_fleet_gpu_lambda = {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("repository_url        = local.fcvm_lambda_repositories[k].repository_url", lane)


class QuotaTests(unittest.TestCase):
    def test_on_demand_waits_for_the_open_base_request_and_preserves_larger_grants(self):
        self.assertRegex(TF, r"fcvm_fleet_on_demand_g_base_vcpus\s*= 768\b")
        source = block('data "aws_servicequotas_service_quota" "fcvm_fleet_on_demand_g_east1"')
        quota = block('resource "aws_servicequotas_service_quota" "fcvm_fleet_on_demand_g_east1"')
        for section in (source, quota):
            self.assertIn('provider     = aws.fcvm_fleet_east1', section)
            self.assertIn('quota_code   = "L-DB2E81BA"', section)
        self.assertIn("fcvm_fleet_on_demand_g_granted = data.aws_servicequotas_service_quota.fcvm_fleet_on_demand_g_east1.value", TF)
        self.assertIn(
            "value = ( local.fcvm_fleet_on_demand_g_granted < local.fcvm_fleet_on_demand_g_base_vcpus "
            "? local.fcvm_fleet_on_demand_g_base_vcpus : max(local.fcvm_fleet_on_demand_g_vcpus, local.fcvm_fleet_on_demand_g_granted) )",
            " ".join(quota.split()),
        )

    def test_the_renders_builder_has_standard_on_demand_in_the_build_region(self):
        # One m8gd.metal-24xl (96 vCPUs) at a time: the renders validator launches after the builder has terminated.
        self.assertIn("fcvm_fleet_factory_standard_vcpus = 96", TF)
        source = block('data "aws_servicequotas_service_quota" "fcvm_fleet_on_demand_standard_west2"')
        quota = block('resource "aws_servicequotas_service_quota" "fcvm_fleet_on_demand_standard_west2"')
        for section in (source, quota):
            self.assertIn("provider     = aws.fcvm_fleet_west2", section)
            self.assertIn('quota_code   = "L-1216C47A"', section)
        self.assertIn("max(local.fcvm_fleet_factory_standard_vcpus, "
                      "data.aws_servicequotas_service_quota.fcvm_fleet_on_demand_standard_west2.value)", quota)
        pipeline = (Path(__file__).resolve().parents[1] / "fcvm-fleet-ami-pipeline.tf").read_text()
        self.assertIn("fcvm_ami_build_region   = \"us-west-2\"", pipeline)

    def test_g_spot_is_480_in_us_east_1_and_never_lowered(self):
        # #913: prod's two hosts (192) and a replacement (96), staging's host (96), an image-factory host (96).
        self.assertIn("fcvm_fleet_spot_g_vcpus = 480", TF)
        for suffix, alias in (("east1", "fcvm_fleet_east1"),):
            q = block(f'resource "aws_servicequotas_service_quota" "fcvm_fleet_spot_g_{suffix}"')
            self.assertIn(f"provider     = aws.{alias}", q)
            self.assertIn('quota_code   = "L-3819A6DF"', q)
            self.assertIn(f"max(local.fcvm_fleet_spot_g_vcpus, data.aws_servicequotas_service_quota.fcvm_fleet_spot_g_{suffix}.value)", q)
        # One region is enough (owner, 2026-10-22): the other two are forgotten, never destroyed.
        for gone in ("east2", "west2"):
            self.assertNotIn(f'resource "aws_servicequotas_service_quota" "fcvm_fleet_spot_g_{gone}"', TF)
            self.assertIn(f"from = aws_servicequotas_service_quota.fcvm_fleet_spot_g_{gone}", TF)
        self.assertEqual(TF.count("destroy = false"), 2)

    def test_standard_quotas_hold_the_renders_hosts_in_us_east_1_and_are_never_lowered(self):
        # The renders host class (fcvm-fleet.tf) is a m8gd.metal-24xl, 96 vCPUs of the Standard families, not G: one host
        # per stack awake (2 x 96) and per stack the replacement that overlaps it during a spot interruption (2 x 96).
        self.assertRegex(TF, r"\n  fcvm_fleet_standard_vcpus = 384\n")
        comment = TF.split("  fcvm_fleet_standard_vcpus = 384\n", 1)[0].rsplit("fcvm_fleet_on_demand_g_vcpus", 1)[1]
        for part in ("m8gd.metal-24xl", "2 x 96 = 192", "spot first", "us-west-2"):
            self.assertIn(part, " ".join(comment.replace("#", " ").split()), part)
        # Spot first, on demand when spot is refused: the same number in both markets' quotas.
        for name, code in (("spot_standard", "L-34B43A08"), ("on_demand_standard", "L-1216C47A")):
            source = block(f'data "aws_servicequotas_service_quota" "fcvm_fleet_{name}_east1"')
            quota = block(f'resource "aws_servicequotas_service_quota" "fcvm_fleet_{name}_east1"')
            for section in (source, quota):
                self.assertIn("provider     = aws.fcvm_fleet_east1", section)
                self.assertIn('service_code = "ec2"', section)
                self.assertIn(f'quota_code   = "{code}"', section)
            # A quota AWS granted above the target is kept: a request can only raise one.
            self.assertIn(f"value        = max(local.fcvm_fleet_standard_vcpus, data.aws_servicequotas_service_quota.fcvm_fleet_{name}_east1.value)", quota)
        # Once per region: us-east-1 where the class runs, and Standard on demand in us-west-2 for the image factory's
        # renders builder (test_the_renders_builder_has_standard_on_demand_in_the_build_region).
        owners = {code: sorted(re.search(r"provider\s+= (\S+)", body).group(1) for body in
                               re.findall(r'resource "aws_servicequotas_service_quota" "\w+" \{\n([^}]*"%s"[^}]*)\}' % code, TF))
                  for code in ("L-34B43A08", "L-1216C47A")}
        self.assertEqual(owners, {"L-34B43A08": ["aws.fcvm_fleet_east1"], "L-1216C47A": ["aws.fcvm_fleet_east1", "aws.fcvm_fleet_west2"]})
        # The current values are not in the repository: the file says to read them before applying.
        self.assertIn("aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A --region us-east-1", TF)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
