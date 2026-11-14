#!/usr/bin/env python3
"""Factory failure/ownership tests. Every AWS client is an in-memory fake."""
import copy
import datetime
import fnmatch
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import Mock, patch


class ClientError(Exception):
    def __init__(self, response, operation):
        super().__init__(operation)
        self.response = response


# CI deliberately disables site packages. Never import an SDK or discover
# credentials: an accidentally unmocked client must fail before reaching AWS.
sdk = types.ModuleType("boto3")
sdk.client = Mock(side_effect=AssertionError("Unmocked AWS client in offline factory test"))
sdk.Session = Mock(side_effect=AssertionError("Unmocked AWS session in offline factory test"))
stubs = {"boto3": sdk, "botocore": types.ModuleType("botocore"),
         "botocore.config": types.SimpleNamespace(Config=lambda **kwargs: None),
         "botocore.exceptions": types.SimpleNamespace(ClientError=ClientError)}

SOURCE = Path(__file__).resolve().parents[1] / "fcvm-fleet-ami-pipeline" / "dispatch.py"
SPEC = importlib.util.spec_from_file_location("factory_dispatch", SOURCE)
mod = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, stubs):
    SPEC.loader.exec_module(mod)
NOW = 1793476800
BUILD = "factory:11111111-1111-1111-1111-111111111111"
CAP = "c" * 43
SOURCE_SHA = "1" * 40
INPUT_SHA = "2" * 64


class Missing(Exception):
    pass


class Store:
    exceptions = types.SimpleNamespace(ParameterNotFound=Missing, InvocationDoesNotExist=Missing)

    def __init__(self):
        self.values = {}
        self.writes = []
        self.commands = []
        self.pending = False

    def get_parameter(self, Name, **kwargs):
        if Name not in self.values:
            raise Missing()
        return {"Parameter": {"Value": json.dumps(self.values[Name])}}

    def put_parameter(self, Name, Value, **kwargs):
        self.writes.append(Name)
        self.values[Name] = json.loads(Value)
        return {"Version": 1}

    def get_command_invocation(self, **kwargs):
        self.commands.append(kwargs)
        if self.pending:
            raise Missing()
        return {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": '{"healthy":true}', "StandardErrorContent": ""}


class CodeBuild:
    def __init__(self):
        self.builds = {BUILD: self.build(BUILD)}
        self.starts = []
        self.stops = []
        self.lose_start = False

    def build(self, build_id, variables=None):
        return {"id": build_id, "projectName": "factory", "buildStatus": "IN_PROGRESS",
                "startTime": datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc), "resolvedSourceVersion": SOURCE_SHA,
                "timeoutInMinutes": 360,
                "environment": {"environmentVariables": variables or [
                    {"name": "SOURCE_SHA", "value": SOURCE_SHA}, {"name": "INPUT_SHA256", "value": INPUT_SHA},
                    {"name": "FLEET_AMI_CAPABILITY", "value": CAP}]}}

    def batch_get_builds(self, ids):
        return {"builds": [self.builds[i] for i in ids if i in self.builds]}

    def list_builds_for_project(self, **kwargs):
        return {"ids": list(self.builds)}

    def start_build(self, **kwargs):
        self.starts.append(kwargs)
        digit = str(len(self.starts) + 1)
        build_id = "factory:" + "-".join(digit * n for n in (8, 4, 4, 4, 12))
        build = self.build(build_id, kwargs["environmentVariablesOverride"])
        build["resolvedSourceVersion"] = kwargs["sourceVersion"]
        build["startTime"] = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc)
        build["timeoutInMinutes"] = kwargs.get("timeoutInMinutesOverride", 240)
        self.builds[build_id] = build
        if self.lose_start:
            self.lose_start = False
            raise TimeoutError("lost StartBuild response")
        return {"build": build}

    def stop_build(self, id):
        self.stops.append(id)
        self.builds[id]["buildStatus"] = "STOPPED"
        return {"build": self.builds[id]}


class Publisher:
    def __init__(self):
        self.calls = []
        self.lose_response = False
        self.observe = lambda: None

    def invoke(self, **arguments):
        self.observe()
        self.calls.append(arguments)
        if self.lose_response:
            raise TimeoutError("lost asynchronous publisher receipt")
        return {"StatusCode": 202}


class EC2:
    def __init__(self, env):
        self.env = env
        self.instances = {}
        self.images = {"ami-canonical": {"ImageId": "ami-canonical", "OwnerId": "099720109477", "Public": True,
                                        "State": "available", "Architecture": "x86_64", "RootDeviceName": "/dev/sda1"}}
        self.launches = []
        self.seals = []
        self.terminations = []
        self.lifecycle = []
        self.lose_launch = False
        self.lose_seal = False
        self.deleted_images = []
        self.deleted_snapshots = []

    def describe_instances(self, Filters=None, **kwargs):
        rows = list(self.instances.values())
        for f in Filters or []:
            if f["Name"].startswith("tag:"):
                key = f["Name"][4:]
                rows = [i for i in rows if {t["Key"]: t["Value"] for t in i["Tags"]}.get(key) in f["Values"]]
            elif f["Name"] == "instance-state-name":
                rows = [i for i in rows if i["State"]["Name"] in f["Values"]]
            elif f["Name"] == "instance-id":
                rows = [i for i in rows if i["InstanceId"] in f["Values"]]
            else:
                raise AssertionError("unexpected fixture filter")
        return {"Reservations": [{"OwnerId": self.env["FLEET_ACCOUNT"], "Instances": copy.deepcopy(rows)}]}

    def describe_images(self, ImageIds=None, Filters=None, **kwargs):
        rows = [self.images[i] for i in ImageIds] if ImageIds else list(self.images.values())
        for f in Filters or []:
            if f["Name"] == "name":
                rows = [i for i in rows if i.get("Name") in f["Values"]]
        return {"Images": copy.deepcopy(rows)}

    def run_instances(self, **kwargs):
        self.launches.append(kwargs)
        instance_id = "i-" + str(len(self.launches)).zfill(17)
        self.instances[instance_id] = {"InstanceId": instance_id, "State": {"Name": "running"},
            "InstanceType": kwargs["InstanceType"], "IamInstanceProfile": kwargs["IamInstanceProfile"], "ImageId": kwargs["ImageId"],
            "Tags": copy.deepcopy(kwargs["TagSpecifications"][0]["Tags"]),
            "LaunchTime": datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)}
        if self.lose_launch:
            self.lose_launch = False
            raise TimeoutError("lost RunInstances response")
        return {"Instances": [{"InstanceId": instance_id}]}

    def create_image(self, **kwargs):
        self.seals.append(kwargs)
        image_id = "ami-" + str(len(self.seals)).zfill(17)
        self.images[image_id] = {"ImageId": image_id, "Name": kwargs["Name"], "OwnerId": self.env["FLEET_ACCOUNT"],
            "Public": False, "State": "available", "Architecture": "x86_64", "RootDeviceName": "/dev/sda1",
            "Tags": kwargs["TagSpecifications"][0]["Tags"]}
        if self.lose_seal:
            self.lose_seal = False
            raise TimeoutError("lost CreateImage response")
        return {"ImageId": image_id}

    def terminate_instances(self, InstanceIds):
        self.terminations.append(InstanceIds)
        for instance_id in InstanceIds:
            self.instances[instance_id]["State"]["Name"] = "shutting-down"
        return {}

    def stop_instances(self, InstanceIds):
        self.lifecycle.append(("stop", InstanceIds))
        for instance_id in InstanceIds:
            self.instances[instance_id]["State"]["Name"] = "stopped"
        return {}

    def start_instances(self, InstanceIds):
        self.lifecycle.append(("start", InstanceIds))
        return {}

    def reboot_instances(self, InstanceIds):
        self.lifecycle.append(("reboot", InstanceIds))
        return {}

    def deregister_image(self, ImageId):
        self.deleted_images.append(ImageId)
        del self.images[ImageId]

    def delete_snapshot(self, SnapshotId):
        self.deleted_snapshots.append(SnapshotId)


class STS:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {"Credentials": {"AccessKeyId": "fixture-access", "SecretAccessKey": "fixture-secret", "SessionToken": "fixture-session",
            "Expiration": datetime.datetime.fromtimestamp(NOW + 3600, datetime.timezone.utc)}}


class Dynamo:
    exceptions = types.SimpleNamespace(ConditionalCheckFailedException=Missing)

    def __init__(self):
        self.held = False
        self.busy = False
        self.info = None

    def put_item(self, **kwargs):
        if self.busy or self.held:
            raise Missing()
        self.held = True
        self.info = kwargs["Item"]["Info"]

    def delete_item(self, **kwargs):
        if kwargs["ExpressionAttributeValues"][":info"] != self.info:
            raise Missing()
        self.held = False
        self.info = None


class FactoryTests(unittest.TestCase):
    def test_codebuild_metadata_and_stop_authorize_only_the_factory_project(self):
        # AWS authorizes these APIs against the PROJECT ARN, not the build ID/ARN.
        # https://docs.aws.amazon.com/service-authorization/latest/reference/list_codebuild.html
        source = (SOURCE.parents[1] / "fcvm-fleet-ami-pipeline.tf").read_text()
        project = "arn:aws:codebuild:us-west-1:111111111111:project/fcvm-fleet-image-factory"
        foreign = project + "-unrelated"
        for role, actions in (("fcvm_ami_dispatch", ("codebuild:BatchGetBuilds", "codebuild:StopBuild")),
                              ("fcvm_ami_publish", ("codebuild:BatchGetBuilds",))):
            policy = re.search(r'^resource "aws_iam_role_policy" "' + role + r'" \{\n.*?^\}',
                               source, re.S | re.M).group()
            for action in actions:
                with self.subTest(role=role, action=action):
                    grants = []
                    for line in policy.splitlines():
                        match = re.search(r'Effect\s*=\s*"Allow"\s*,\s*Action\s*=\s*(\[[^\]]+\]|"[^"]+")\s*,\s*Resource\s*=\s*(aws_codebuild_project\.fcvm_ami\.arn|"[^"]+")', line)
                        if not match:
                            continue
                        allowed = json.loads(match[1])
                        if action not in ([allowed] if isinstance(allowed, str) else allowed):
                            continue
                        resource = match[2]
                        if resource == "aws_codebuild_project.fcvm_ami.arn":
                            resource = project
                        else:
                            resource = json.loads(resource).replace("${var.aws_region}", "us-west-1").replace(
                                "${data.aws_caller_identity.current.account_id}", "111111111111").replace(
                                "${local.fcvm_ami_project}", "fcvm-fleet-image-factory")
                        grants.append(resource)
                    self.assertEqual(len(grants), 1)
                    self.assertTrue(fnmatch.fnmatchcase(project, grants[0]), "the factory's project must be authorized")
                    self.assertFalse(fnmatch.fnmatchcase(foreign, grants[0]), "foreign projects must remain unauthorized")

    def test_staging_s3_resources_and_endpoints_obey_the_actual_fleet_region_scp(self):
        root = SOURCE.parents[1]
        factory = (root / "fcvm-fleet-ami-pipeline.tf").read_text()
        account = (root / "fcvm-fleet-account.tf").read_text()
        globals_source = (root / "dolphin-region-scp.tf").read_text()
        regions = json.loads(re.search(r"fcvm_fleet_regions\s*=\s*(\[[^\]]+\])", account).group(1))
        home = re.search(r'fcvm_fleet_home_region\s*=\s*"([^"]+)"', account).group(1)
        default = re.search(r'variable "aws_region" \{.*?default\s*=\s*"([^"]+)"',
                            (root / "variables.tf").read_text(), re.S).group(1)
        self.assertIn('region = var.aws_region', (root / "main.tf").read_text())
        ohio = re.search(r'provider "aws" \{\n\s*alias\s*=\s*"ohio".*?^\}',
                         (root / "ohio.tf").read_text(), re.S | re.M).group()
        ohio_region = re.search(r'region\s*=\s*"([^"]+)"', ohio).group(1)
        self.assertNotIn("assume_role", ohio, "staging must stay in the main account")
        self.assertEqual(ohio_region, home)
        self.assertIn('"aws:RequestedRegion" = local.fcvm_fleet_regions', account)
        global_actions = re.findall(r'"([^"]+)"', re.search(
            r"dolphin_region_scp_global_actions\s*=\s*\[(.*?)\n  \]", globals_source, re.S).group(1))
        def denied(region, action):
            return region not in regions and not any(fnmatch.fnmatchcase(action, pattern) for pattern in global_actions)
        for action in ("s3:GetObject", "s3:PutObject"):
            self.assertTrue(denied(default, action), "the existing Fleet SCP must stay closed outside its regions")
        resources = list(re.finditer(r'^resource "(aws_s3_[^"]+)" "fcvm_ami_pipeline" \{\n.*?^\}',
                                     factory, re.S | re.M))
        self.assertTrue(resources)
        for resource in resources:
            with self.subTest(resource=resource.group(1)):
                provider = re.search(r"^  provider\s*=\s*([\w.]+)", resource.group(), re.M)
                alias = provider.group(1) if provider else "aws"
                region = {"aws": default, "aws.ohio": ohio_region}.get(alias)
                self.assertFalse(denied(region, "s3:GetObject"))
                self.assertFalse(denied(region, "s3:PutObject"))
                self.assertEqual(alias, "aws.ohio", "every bucket configuration must use the same main-account provider")
        asset = re.search(r'name\s*=\s*"FLEET_AMI_ASSET_REGION"\s+value\s*=\s*([^\n]+)', factory)
        self.assertIsNotNone(asset)
        self.assertEqual(asset.group(1).strip(), "local.fcvm_fleet_home_region")
        publisher = re.search(r'^resource "aws_lambda_function" "fcvm_ami_publish" \{\n.*?^\}', factory, re.S | re.M).group()
        self.assertRegex(publisher, r"PIPELINE_REGION\s*=\s*local\.fcvm_fleet_home_region")

    def setUp(self):
        self.time = NOW
        self.env = {"ENABLED": "true", "STATE_PREFIX": "/factory", "PROJECT_NAME": "factory", "CONTROL_REGION": "us-west-1",
            "COMPUTE_REGION": "us-east-1", "FLEET_ACCOUNT": "123456789012", "BUILDER_PROFILE": "arn:aws:iam::123456789012:instance-profile/factory",
            "FLEET_ROLE": "arn:aws:iam::123456789012:role/broker", "WORKER_ROLE": "arn:aws:iam::123456789012:role/worker",
            "PIPELINE_BUCKET": "fixture-factory", "RELEASE_PARAMETER": "/release", "CANONICAL_AMI": "ami-canonical",
            "SUBNET_ID": "subnet-fixture", "SECURITY_GROUP": "sg-fixture", "GUARD_SHA256": "a" * 64}
        self.env.update(LOCK_TABLE="fixture-locks", LOCK_ID="fixture-state/terraform.tfstate")
        self.env.update(CONTROL_ACCOUNT="999999999999", PUBLISH_FUNCTION="arn:aws:lambda:us-west-1:999999999999:function:factory-publish")
        self.env["TEMPLATES_JSON"] = "[]"
        self.store, self.fleet_ssm, self.sts = Store(), Store(), STS()
        self.ddb = Dynamo()
        self.cb = CodeBuild()
        self.cb.clock = lambda: self.time
        self.publisher = Publisher()
        self.publisher.observe = lambda: self.assertFalse(self.ddb.held, "publisher must be invoked outside the backend lock")
        self.ec2 = EC2(self.env)
        clients = {("ssm", False): self.store, ("ssm", True): self.fleet_ssm, ("ec2", True): self.ec2,
                   ("codebuild", False): self.cb, ("sts", False): self.sts, ("dynamodb", False): self.ddb,
                   ("lambda", False): self.publisher}
        self.factory = mod.Factory(self.env, clients, clock=lambda: self.time)
        self.factory.client = lambda name, fleet=False: clients[(name, fleet)]  # Unexpected clients fail, never reach AWS.
        self.store.values["/factory/control"] = {"active": BUILD, "pending": None}
        self.store.values["/factory/requests/" + INPUT_SHA] = {"request_id": INPUT_SHA, "status": "building", "source_sha": SOURCE_SHA,
            "input_sha256": INPUT_SHA, "priority": "normal", "build_id": BUILD}
        self.queue_prepared = False

    def succeed(self, duration=120):
        self.factory.journal(BUILD)
        ended = NOW + duration
        self.cb.builds[BUILD].update(buildStatus="SUCCEEDED", buildComplete=True,
            endTime=datetime.datetime.fromtimestamp(ended, datetime.timezone.utc))
        self.time = ended + 1
        self.fleet_ssm.values["/release"] = {"build_id": "previous"}
        return ended

    def call(self, action, slot=None, **extra):
        event = {"action": action, "build_id": BUILD, "capability": CAP, **extra}
        if slot:
            event["slot"] = slot
        return self.factory.broker(event)

    def launch(self, slot="host-build"):
        return self.call("launch", slot)["instance_id"]

    def seal(self, slot="host-build"):
        instance_id = self.launch(slot)
        self.ec2.instances[instance_id]["State"]["Name"] = "stopped"
        return self.call("seal", slot)

    def request(self, priority="normal", input_sha=INPUT_SHA, source_sha=SOURCE_SHA, comparison=None):
        if not self.queue_prepared and not self.ec2.launches:
            self.store.values.pop("/factory/control", None)
            self.store.values.pop("/factory/requests/" + INPUT_SHA, None)
        self.queue_prepared = True
        def response(path):
            if path == "git/ref/heads/main":
                return {"object": {"sha": source_sha}}
            base = "compare/" + mod.EXTENDED_TIMEOUT_SOURCE + "..." + source_sha
            if path in (base, base + "?per_page=1&page=2"):
                if isinstance(comparison, Exception):
                    raise comparison
                # GitHub puts every changed file's patch on the first page of a comparison: over a
                # long history that page passes public_github's 2 MiB bound (2.24 MB for the
                # extended-timeout source...20fc62c4 on 2026-11-14), and only page two is small.
                if isinstance(comparison, dict) and comparison.get("oversized"):
                    if path == base:
                        raise mod.Refused("trusted CI metadata exceeds its fixed bound")
                    return {k: v for k, v in comparison.items() if k != "oversized"}
                return comparison
            return {"workflow_runs": [self.ci_run(head_sha=source_sha)]}
        with patch.object(mod, "public_github", side_effect=response):
            return self.factory.request({"action": "request", "source_sha": source_sha, "input_sha256": input_sha, "priority": priority})

    def ci_run(self, **changes):
        return {"id": 99, "head_sha": SOURCE_SHA, "head_branch": "main", "event": "push", "status": "completed",
                "conclusion": "success", "head_repository": {"full_name": "ejc3/fcvm"}, "path": ".github/workflows/ci.yml", **changes}

    def test_missing_wrong_and_other_build_capability_precede_mutations(self):
        for capability in (None, "wrong"):
            event = {"action": "launch", "build_id": BUILD, "slot": "host-build"}
            if capability is not None:
                event["capability"] = capability
            with self.assertRaises(mod.Refused):
                self.factory.broker(event)
        other = "factory:33333333-3333-3333-3333-333333333333"
        self.cb.builds[other] = self.cb.build(other)
        self.cb.builds[other]["environment"]["environmentVariables"][-1]["value"] = "different" * 6
        with self.assertRaises(mod.Refused):
            self.factory.broker({"action": "launch", "build_id": other, "slot": "host-build", "capability": CAP})
        self.assertEqual((self.ec2.launches, self.store.writes, self.sts.calls), ([], [], []))

    def test_caller_cannot_supply_ami_profile_type_count_or_commands(self):
        for key in ("image_id", "profile", "instance_type", "max_count", "commands"):
            with self.assertRaises(mod.Refused):
                self.call("launch", "host-build", **{key: "override"})
        self.assertEqual(self.ec2.launches, [])

    def test_fixed_launch_uses_one_slot_ipv6_imdsv2_and_encrypted_root(self):
        self.launch()
        p = self.ec2.launches[0]
        self.assertEqual((p["MinCount"], p["MaxCount"], p["InstanceType"]), (1, 1, "g4dn.metal"))
        self.assertEqual(p["IamInstanceProfile"], {"Arn": self.env["BUILDER_PROFILE"]})
        self.assertEqual(p["MetadataOptions"]["HttpTokens"], "required")
        self.assertEqual(p["NetworkInterfaces"][0]["Ipv6PrefixCount"], 1)
        self.assertTrue(p["BlockDeviceMappings"][0]["Ebs"]["Encrypted"])
        self.assertEqual({t["ResourceType"] for t in p["TagSpecifications"]}, {"instance", "volume", "network-interface"})
        self.call("launch", "host-build")
        self.assertEqual(len(self.ec2.launches), 1)
        self.launch("tile-build")
        self.assertEqual(self.ec2.launches[-1]["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"], 32)

    def test_the_host_image_root_has_room_for_the_store_and_a_host_kernel(self):
        # The host image keeps its packed store (about 28 GB) on the root disk as the S3
        # restore's fallback. With 64 GB the root of a gpubox from the cad0ddad image had 416 MB
        # free, and installing a 7.3.3 host kernel there failed ("No space left on device"). The
        # host builder's root, which every host booted from the image inherits, is 96 GB, and
        # the role allows exactly that.
        self.launch()
        self.assertEqual(self.ec2.launches[0]["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"], 96)
        tf = (Path(__file__).resolve().parent.parent / "fcvm-fleet-ami-pipeline.tf").read_text()
        cap = re.search(r'Sid = "FixedRoot".*?NumericLessThanEquals = \{ "ec2:VolumeSize" = (\d+) \}', tf, re.S)
        self.assertIsNotNone(cap, "no root volume size cap in the builder role")
        self.assertEqual(int(cap.group(1)), max(mod.VOLUMES.values()))

    def test_definite_capacity_rejection_frees_queue_without_retrying_slot(self):
        def rejected(**kwargs):
            raise mod.ClientError({"Error": {"Code": "InsufficientInstanceCapacity", "Message": "fixture"}}, "RunInstances")
        self.ec2.run_instances = rejected
        with self.assertRaises(mod.Refused):
            self.launch()
        suffix, journal = self.factory.journal(BUILD)
        self.assertTrue(self.factory.gone(suffix, journal))
        self.assertEqual(self.call("launch", "host-build")["state"], "rejected")

    def test_lost_launch_is_recovered_without_a_second_paid_call(self):
        self.ec2.lose_launch = True
        with self.assertRaises(TimeoutError):
            self.launch()
        result = self.call("status", "host-build")
        self.assertEqual(result["state"], "running")
        _, recovered = self.factory.journal(BUILD)
        self.assertEqual(recovered["slots"]["host-build"].get("instance_id"), result["instance_id"])
        self.assertEqual(self.call("launch", "host-build")["instance_id"], result["instance_id"])
        self.assertEqual(len(self.ec2.launches), 1)

    def test_accepted_launch_keeps_its_prefix_before_describe_visibility(self):
        with patch.object(self.ec2, "describe_instances", return_value={"Reservations": []}):
            result = self.call("launch", "host-build")
            instance_id = result["instance_id"]
            _, journal = self.factory.journal(BUILD)
            self.assertEqual(journal["slots"]["host-build"]["instance_id"], instance_id)
            prefix = "builders/arn:aws:ec2:" + self.env["COMPUTE_REGION"] + ":" + self.env["FLEET_ACCOUNT"] + ":instance/" + instance_id + "/"
            self.assertEqual(result["asset_prefix"], prefix)
            self.assertEqual(result["state"], "uncertain")
            self.assertEqual(self.call("launch", "host-build"), result)
            with self.assertRaises(mod.Refused):
                self.call("command_status", "host-build", command_id="11111111-1111-1111-1111-111111111111")
            self.assertEqual(self.fleet_ssm.commands, [])
            self.assertEqual(len(self.ec2.launches), 1)
        visible = self.call("status", "host-build")
        self.assertEqual((visible["instance_id"], visible["asset_prefix"], visible["state"]),
                         (instance_id, prefix, "running"))
        self.assertEqual(len(self.ec2.launches), 1)

    def test_unknown_launch_receipt_has_no_prefix_and_is_not_replayed_while_hidden(self):
        self.ec2.lose_launch = True
        with patch.object(self.ec2, "describe_instances", return_value={"Reservations": []}):
            with self.assertRaises(TimeoutError):
                self.call("launch", "host-build")
            for action in ("status", "launch"):
                result = self.call(action, "host-build")
                self.assertIsNone(result["instance_id"])
                self.assertIsNone(result["asset_prefix"])
                self.assertEqual(result["state"], "uncertain")
            self.assertEqual(len(self.ec2.launches), 1)
        recovered = self.call("status", "host-build")
        self.assertEqual(recovered["state"], "running")
        self.assertTrue(recovered["asset_prefix"].endswith("/" + recovered["instance_id"] + "/"))
        self.assertEqual(len(self.ec2.launches), 1)

    def test_known_prefix_does_not_authorize_an_instance_with_the_wrong_profile(self):
        accepted = self.call("launch", "host-build")
        self.ec2.instances[accepted["instance_id"]]["IamInstanceProfile"] = {"Arn": "foreign-profile"}
        result = self.call("status", "host-build")
        self.assertEqual(result["instance_id"], accepted["instance_id"])
        self.assertEqual(result["asset_prefix"], accepted["asset_prefix"])
        self.assertEqual(result["state"], "uncertain")
        with self.assertRaises(mod.Refused):
            self.call("command_status", "host-build", command_id="11111111-1111-1111-1111-111111111111")
        self.assertEqual(self.fleet_ssm.commands, [])
        self.assertEqual(self.call("launch", "host-build"), result)
        self.assertEqual(len(self.ec2.launches), 1)

    def test_known_terminated_instance_without_a_profile_persists_its_terminal_receipt(self):
        instance_id = self.launch()
        instance = self.ec2.instances[instance_id]
        instance["State"]["Name"] = "terminated"
        del instance["IamInstanceProfile"]  # Actual terminal DescribeInstances shape.
        writes = len(self.store.writes)
        result = self.call("status", "host-build")
        self.assertEqual((result["instance_id"], result["state"]), (instance_id, "terminated"))
        self.assertEqual(len(self.store.writes), writes + 1)
        suffix, journal = self.factory.journal(BUILD)
        self.assertTrue(journal["slots"]["host-build"]["terminated_seen"])
        self.assertTrue(self.factory.gone(suffix, journal))
        self.assertEqual(self.call("cleanup")["slots"]["host-build"]["state"], "terminated")
        self.assertEqual(self.ec2.terminations, [])
        del self.ec2.instances[instance_id]
        suffix, journal = self.factory.journal(BUILD)
        self.assertTrue(self.factory.gone(suffix, journal), "retain the observed terminal proof after EC2 forgets the row")
        self.assertEqual(len(self.ec2.launches), 1)

    def test_profileless_terminal_receipts_cannot_bypass_identity_or_active_profile_guards(self):
        variants = ("running", "pending", "stopped", "shutting-down", "wrong-profile", "empty-profile",
                    "unknown-id", "different-id", "wrong-type", "Purpose", "FactoryBuild", "FactorySlot")
        for variant in variants:
            with self.subTest(variant=variant):
                case = FactoryTests()
                case.setUp()
                instance_id = case.launch()
                instance = case.ec2.instances[instance_id]
                instance["State"]["Name"] = "terminated"
                del instance["IamInstanceProfile"]
                suffix, journal = case.factory.journal(BUILD)
                if variant in ("running", "pending", "stopped", "shutting-down"):
                    instance["State"]["Name"] = variant
                elif variant in ("wrong-profile", "empty-profile"):
                    instance["IamInstanceProfile"] = {"Arn": "foreign-profile"} if variant == "wrong-profile" else {}
                elif variant in ("unknown-id", "different-id"):
                    journal["slots"]["host-build"].pop("instance_id")
                    if variant == "different-id":
                        journal["slots"]["host-build"]["instance_id"] = "i-" + "f" * 17
                    case.factory.save(suffix, journal)
                elif variant == "wrong-type":
                    instance["InstanceType"] = "g4dn.xlarge"
                else:
                    next(tag for tag in instance["Tags"] if tag["Key"] == variant)["Value"] = "foreign"
                self.assertEqual(case.call("status", "host-build")["state"], "uncertain")
                self.assertEqual(case.call("launch", "host-build")["state"], "uncertain")
                with self.assertRaises(mod.Refused):
                    case.call("command_status", "host-build", command_id="11111111-1111-1111-1111-111111111111")
                suffix, journal = case.factory.journal(BUILD)
                self.assertFalse(journal["slots"]["host-build"].get("terminated_seen"))
                self.assertFalse(case.factory.gone(suffix, journal))
                self.assertEqual(case.fleet_ssm.commands, [])
                self.assertEqual(case.ec2.terminations, [])
                self.assertEqual(len(case.ec2.launches), 1)

    def test_expired_early_builders_allow_verification_and_full_final_cleanup(self):
        for kind in ("host", "tile"):
            image = self.seal(kind + "-build")
            instance_id = image["instance_id"]
            self.ec2.instances[instance_id]["State"]["Name"] = "terminated"
            self.call("status", kind + "-build")  # Persist the actual observed termination.
            del self.ec2.instances[instance_id]
        self.time += 3701
        try:
            for kind in ("host", "tile"):
                instance_id = self.call("launch", kind + "-verify")["instance_id"]
                self.ec2.instances[instance_id]["State"]["Name"] = "terminated"
                self.call("status", kind + "-verify")
            result = self.call("cleanup")
        except mod.Refused as error:
            self.fail("expired metadata blocked the known terminated builders: " + str(error))
        self.assertEqual(set(result["slots"]), set(mod.SLOTS))
        self.assertTrue(all(value["state"] == "terminated" for value in result["slots"].values()))
        status = self.call("status")
        self.assertTrue(all(value["state"] == "terminated" for value in status["slots"].values()))
        self.assertEqual((len(self.ec2.launches), self.ec2.terminations), (4, []))

    def test_absent_instance_requires_a_strict_recorded_termination(self):
        for variant in ("missing", "false", "string", "attempted-false", "attempted-string", "invalid-id", "no-instance", "rejected"):
            with self.subTest(variant=variant):
                case = FactoryTests(); case.setUp()
                instance_id = case.launch()
                case.ec2.instances[instance_id]["State"]["Name"] = "terminated"
                case.call("status", "host-build")
                del case.ec2.instances[instance_id]
                suffix, journal = case.factory.journal(BUILD)
                entry = journal["slots"]["host-build"]
                if variant == "missing": entry.pop("terminated_seen")
                elif variant == "false": entry["terminated_seen"] = False
                elif variant == "string": entry["terminated_seen"] = "true"
                elif variant == "attempted-false": entry["attempted"] = False
                elif variant == "attempted-string": entry["attempted"] = "true"
                elif variant == "invalid-id": entry["instance_id"] = "not-an-instance"
                elif variant == "no-instance": entry["no_instance"] = True
                else: entry["rejected"] = True
                case.factory.save(suffix, journal)
                self.assertNotEqual(case.call("status", "host-build")["state"], "terminated")
                self.assertFalse(case.factory.gone(suffix, journal))
                self.assertEqual((len(case.ec2.launches), case.ec2.terminations), (1, []))

    def test_recorded_termination_never_masks_returned_conflicting_rows(self):
        for variant in ("running", "stopped", "shutting-down", "wrong-type", "wrong-profile", "empty-profile",
                        "Purpose", "FactoryBuild", "FactorySlot", "wrong-id", "wrong-owner"):
            with self.subTest(variant=variant):
                case = FactoryTests(); case.setUp()
                instance_id = case.launch()
                case.ec2.instances[instance_id]["State"]["Name"] = "terminated"
                case.call("status", "host-build")
                original = case.ec2.describe_instances
                def contradict(**arguments):
                    reply = original(**arguments)
                    reservation = reply["Reservations"][0]
                    row = reservation["Instances"][0]
                    if variant in ("running", "stopped", "shutting-down"): row["State"]["Name"] = variant
                    elif variant == "wrong-type": row["InstanceType"] = "g4dn.xlarge"
                    elif variant == "wrong-profile": row["IamInstanceProfile"] = {"Arn": "foreign"}
                    elif variant == "empty-profile": row["IamInstanceProfile"] = {}
                    elif variant == "wrong-id": row["InstanceId"] = "i-" + "f" * 17
                    elif variant == "wrong-owner": reservation["OwnerId"] = "foreign"
                    else: next(t for t in row["Tags"] if t["Key"] == variant)["Value"] = "foreign"
                    return reply
                with patch.object(case.ec2, "describe_instances", side_effect=contradict):
                    with self.assertRaises(mod.Refused): case.call("status", "host-build")
                    with self.assertRaises(mod.Refused): case.call("cleanup")
                self.assertEqual((len(case.ec2.launches), case.ec2.terminations), (1, []))

    def test_termination_receipt_does_not_swallow_ec2_errors(self):
        instance_id = self.launch()
        self.ec2.instances[instance_id]["State"]["Name"] = "terminated"
        self.call("status", "host-build")
        with patch.object(self.ec2, "describe_instances", side_effect=RuntimeError("AccessDenied")):
            with self.assertRaisesRegex(RuntimeError, "AccessDenied"): self.call("status", "host-build")
        self.assertEqual((len(self.ec2.launches), self.ec2.terminations), (1, []))

    def test_successful_build_is_redriven_after_busy_lock_and_status_polls_are_throttled(self):
        self.succeed()
        self.ddb.busy = True
        self.factory.tick()
        self.assertEqual(self.publisher.calls, [])
        self.time += 301
        self.ddb.busy = False
        self.factory.tick()
        self.assertEqual(len(self.publisher.calls), 1)
        call = self.publisher.calls[0]
        self.assertEqual((call["FunctionName"], call["InvocationType"]), (self.env["PUBLISH_FUNCTION"], "Event"))
        event = json.loads(call["Payload"])
        self.assertEqual(event, {"source": "aws.codebuild", "detail-type": "CodeBuild Build State Change",
            "account": self.env["CONTROL_ACCOUNT"], "region": self.env["CONTROL_REGION"],
            "detail": {"build-status": "SUCCEEDED", "project-name": "factory",
                "build-id": "arn:aws:codebuild:us-west-1:999999999999:build/" + BUILD}})
        for _ in range(5):
            self.factory.request({"action": "status", "request_id": INPUT_SHA})
        self.time += 299
        self.factory.tick()
        self.assertEqual(len(self.publisher.calls), 1)
        self.time += 1
        self.factory.tick()
        self.assertEqual(len(self.publisher.calls), 2)
        self.assertEqual(self.cb.starts, [])

    def test_uncertain_publication_invoke_is_redriven_only_after_the_fixed_interval(self):
        self.succeed()
        self.publisher.lose_response = True
        with self.assertRaises(TimeoutError):
            self.factory.reconcile()
        self.publisher.lose_response = False
        self.factory.reconcile()
        self.assertEqual(len(self.publisher.calls), 1)
        self.time += 300
        self.factory.reconcile()
        self.assertEqual(len(self.publisher.calls), 2)
        self.assertEqual(self.publisher.calls[0], self.publisher.calls[1])

    def test_success_near_compute_deadline_keeps_a_separate_publication_lease(self):
        ended = self.succeed(21600 - 2)
        self.time = NOW + 21600 + 10
        self.factory.reconcile()
        record = self.factory.request_record(INPUT_SHA)
        self.assertEqual(record["status"], "awaiting-publish")
        self.assertEqual(record["publication_deadline_unix"], ended + 86400)
        self.assertEqual(self.store.values["/factory/control"]["active"], BUILD)
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.cb.stops, [])

    def test_success_waits_for_late_actual_termination_before_publication(self):
        for kind in ("host", "tile"):
            image = self.seal(kind + "-build")
            self.ec2.instances[image["instance_id"]]["State"]["Name"] = "terminated"
            self.call("status", kind + "-build")
            instance_id = self.launch(kind + "-verify")
            self.ec2.instances[instance_id]["State"]["Name"] = "shutting-down"
        ended = self.succeed(21600 - 2)
        self.factory.reconcile()
        self.assertEqual(self.publisher.calls, [], "a successful executor is not termination proof")
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "awaiting-publish")
        self.time = NOW + 21600 + 360
        for instance in self.ec2.instances.values():
            instance["State"]["Name"] = "terminated"
            instance.pop("IamInstanceProfile", None)  # Actual terminal EC2 shape.
        self.factory.reconcile()
        journal = self.factory.journal(BUILD, active=False)[1]
        self.assertTrue(all(value.get("terminated_seen") is True for value in journal["slots"].values()))
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.factory.request_record(INPUT_SHA)["publication_deadline_unix"], ended + 86400)
        self.assertEqual((len(self.ec2.launches), self.cb.starts, self.cb.stops), (4, [], []))

    def test_committed_release_keeps_retrying_retirement_past_publication_deadline(self):
        ended = self.succeed()
        self.fleet_ssm.values["/release"] = {"build_id": BUILD}
        self.time = ended + 86400 + 10
        self.factory.reconcile()
        record = self.factory.request_record(INPUT_SHA)
        self.assertEqual(record["status"], "awaiting-publish")
        self.assertEqual(record["publication_deadline_unix"], ended + 86400)
        self.assertEqual(self.store.values["/factory/control"]["active"], BUILD)
        self.assertEqual(self.fleet_ssm.values["/release"], {"build_id": BUILD})
        self.assertEqual(len(self.publisher.calls), 1)

    def test_uncommitted_publication_lease_expires_without_a_new_build_or_pointer_write(self):
        ended = self.succeed()
        self.time = ended + 86400 + 1
        self.factory.reconcile()
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "failed")
        self.assertIsNone(self.store.values["/factory/control"]["active"])
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self.cb.starts, [])
        self.assertEqual(self.fleet_ssm.writes, [])

    def test_failed_normal_input_is_terminal_but_a_fresh_critical_nonce_can_run(self):
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        self.factory.reconcile()
        previous = copy.deepcopy(self.factory.request_record(INPUT_SHA))
        self.assertEqual(previous["status"], "failed")
        self.queue_prepared = True
        self.assertEqual(self.request("critical")["status"], "failed")
        fresh = self.request("critical", "4" * 64)
        self.assertEqual(fresh["status"], "building")
        self.assertEqual(fresh["source_sha"], SOURCE_SHA)
        self.assertEqual(self.factory.request_record(INPUT_SHA), previous)
        self.assertEqual(len(self.cb.starts), 1)
        self.assertEqual(self.cb.starts[0]["sourceVersion"], SOURCE_SHA)

    def test_lost_create_image_is_observed_without_repeating_create(self):
        instance_id = self.launch()
        self.ec2.instances[instance_id]["State"]["Name"] = "stopped"
        self.ec2.lose_seal = True
        with self.assertRaises(TimeoutError):
            self.call("seal", "host-build")
        result = self.call("seal", "host-build")
        self.assertEqual(result["image_state"], "available")
        self.assertEqual(len(self.ec2.seals), 1)

    def test_verify_waits_for_actual_termination_of_same_kind(self):
        image = self.seal()
        with self.assertRaises(mod.Refused):
            self.call("launch", "host-verify")
        self.call("cleanup")
        with self.assertRaises(mod.Refused):
            self.call("launch", "host-verify")
        self.ec2.instances[image["instance_id"]]["State"]["Name"] = "terminated"
        verified = self.call("launch", "host-verify")
        self.assertEqual(self.ec2.launches[-1]["ImageId"], image["image_id"])
        self.assertNotEqual(verified["instance_id"], image["instance_id"])

    def test_deadline_cannot_be_reset_but_cleanup_still_works(self):
        instance_id = self.launch()
        self.time += 21600 + 1
        for action in ("launch", "start", "reboot", "credentials"):
            with self.assertRaises(mod.Refused):
                self.call(action, "host-build")
        self.call("cleanup")
        self.assertEqual(self.ec2.terminations, [[instance_id]])

    def test_global_lease_survives_240_minutes_and_expires_at_exactly_360(self):
        # 240 minutes never reached a tile builder (ejc3/aws#823): three builds' host sides took
        # 2 h 58 min to 3 h 14 min from launch to CreateImage.
        instance_id = self.launch()
        self.time = NOW + 14401
        try:
            self.call("credentials")
        except mod.Refused:
            self.fail("the approved 360-minute lease expired at the old 240-minute limit")
        self.factory.tick()
        self.assertEqual((self.cb.stops, self.ec2.terminations), ([], []))
        self.assertEqual(self.factory.journal(BUILD)[1]["deadline_unix"], NOW + 21600)
        self.time = NOW + 21600
        issued = len(self.sts.calls)
        with self.assertRaises(mod.Refused):
            self.call("credentials")
        self.assertEqual(len(self.sts.calls), issued)
        self.factory.tick()
        self.assertEqual(self.cb.stops, [BUILD])
        self.assertIn(instance_id, [name for batch in self.ec2.terminations for name in batch])
        self.assertEqual(len(self.ec2.launches), 1)

    def test_legacy_receipt_journals_and_expires_at_240_under_new_broker(self):
        self.cb.builds[BUILD]["timeoutInMinutes"] = 240
        instance_id = self.launch()
        self.assertEqual(self.factory.journal(BUILD)[1]["deadline_unix"], NOW + 14400)
        self.time = NOW + 14400
        with self.assertRaises(mod.Refused):
            self.call("credentials")
        self.factory.tick()
        self.assertEqual(self.cb.stops, [BUILD])
        self.assertTrue(all(ids == [instance_id] for ids in self.ec2.terminations))
        self.assertTrue(self.ec2.terminations)

    def test_missing_receipt_cannot_authorize_more_work_or_block_cleanup(self):
        instance_id = self.launch()
        del self.cb.builds[BUILD]["timeoutInMinutes"]
        with self.assertRaises(mod.Refused):
            self.call("launch", "tile-build")
        self.factory.tick()
        self.assertEqual(self.cb.stops, [BUILD])
        self.assertIn([instance_id], self.ec2.terminations)
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "failed")

    def test_orphan_builder_without_journal_keeps_the_shorter_sweep_bound(self):
        instance_id = self.launch()
        self.store.values.pop("/factory/builds/" + mod.digest(BUILD))
        self.time = NOW + 14400
        self.factory.tick()
        self.assertEqual(self.ec2.terminations, [[instance_id]])

    def test_codebuild_default_keeps_the_legacy_and_separate_command_bounds(self):
        text = (SOURCE.parents[1] / "fcvm-fleet-ami-pipeline.tf").read_text()
        self.assertRegex(text, r"fcvm_ami_build_minutes\s*=\s*240\b")
        self.assertRegex(text, r"build_timeout\s*=\s*local\.fcvm_ami_build_minutes")
        document = re.search(r'^resource "aws_ssm_document" "fcvm_ami_build" \{\n.*?^\}', text, re.S | re.M).group()
        self.assertIn('default = "9000"', document)
        self.assertIn('allowedPattern = "^([1-9][0-9]{0,2}|[1-8][0-9]{3}|9000)$"', document)

    def test_sealing_cannot_mark_an_image_runtime_validated(self):
        self.seal()
        tags = {t["Key"]: t["Value"] for t in self.ec2.seals[0]["TagSpecifications"][0]["Tags"]}
        self.assertEqual(tags["Purpose"], "fcvm-fleet")
        self.assertEqual(tags["PipelineSanitized"], "true")
        self.assertEqual(tags["SourceCommit"], SOURCE_SHA)
        self.assertEqual(tags["InputSHA256"], INPUT_SHA)
        self.assertNotIn("FleetImageValidated", tags)

    def test_sealed_session_has_immutable_build_tag_and_only_owned_prefixes(self):
        instance_id = self.launch()
        value = self.call("credentials")
        request = self.sts.calls[-1]
        self.assertEqual(request["RoleArn"], self.env["WORKER_ROLE"])
        self.assertEqual(request["Tags"], [{"Key": "FactoryBuild", "Value": mod.digest(BUILD)}])
        policy = json.loads(request["Policy"])
        objects = policy["Statement"][0]["Resource"]
        self.assertTrue(any("/runs/" + mod.digest(BUILD) + "/*" in r for r in objects))
        self.assertTrue(any(instance_id in r for r in objects))
        self.assertFalse(any("/runs/*" in r or "/builders/*" in r for r in objects))
        self.assertIn("credentials", value)
        public = self.call("status")
        self.assertNotIn("credentials", public)
        self.assertNotIn("capability", public)

    def test_command_status_pins_instance_and_handles_ssm_propagation(self):
        instance_id = self.launch()
        command_id = "44444444-4444-4444-4444-444444444444"
        self.fleet_ssm.pending = True
        result = self.call("command_status", "host-build", command_id=command_id)
        self.assertEqual(result["status"], "Pending")
        self.assertEqual(self.fleet_ssm.commands[-1], {"CommandId": command_id, "InstanceId": instance_id})
        with self.assertRaises(mod.Refused):
            self.call("command_status", "host-build", command_id="not-a-command")

    def test_normal_debounce_and_same_input_dedupe_do_not_launch_early(self):
        first = self.request()
        self.assertEqual(first["status"], "queued")
        self.assertEqual(self.cb.starts, [])
        self.request()
        self.time += 21599
        self.factory.reconcile()
        self.assertEqual(self.cb.starts, [])
        self.time += 1
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 1)
        self.assertEqual(self.cb.starts[0]["sourceVersion"], SOURCE_SHA)

    def test_latest_normal_replacement_starts_once_at_the_first_deadline(self):
        first = self.request()
        deadline = first["not_before"]
        self.assertEqual(deadline, NOW + 21600)
        self.time += 7200
        second = self.request(input_sha="3" * 64, source_sha="4" * 40)
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "superseded")
        self.time = deadline - 1
        latest_source = "5" * 40
        latest_input = "6" * 64
        latest = self.request(input_sha=latest_input, source_sha=latest_source)
        self.assertEqual(self.factory.request_record(second["request_id"])["status"], "superseded")
        self.assertEqual(latest["status"], "queued")
        self.assertEqual(self.cb.starts, [])

        self.time = deadline
        self.factory.reconcile()
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 1)
        self.assertEqual(self.cb.starts[0]["sourceVersion"], latest_source)
        self.assertEqual(self.cb.starts[0]["idempotencyToken"], latest_input)
        self.assertEqual(self.factory.request_record(latest_input)["status"], "building")
        self.assertEqual(latest["not_before"], deadline)

    def test_lost_start_response_never_starts_another_build(self):
        self.cb.builds.clear()
        self.cb.lose_start = True
        with self.assertRaises(TimeoutError):
            self.request("critical", source_sha=mod.EXTENDED_TIMEOUT_SOURCE)
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 1)
        record = self.factory.request_record(INPUT_SHA)
        self.assertEqual(record["status"], "building")
        self.assertEqual(record["build_id"], "factory:22222222-2222-2222-2222-222222222222")
        self.assertEqual(record["deadline_unix"], NOW + 21600)

    def test_six_hour_override_requires_a_compatible_executor_source(self):
        supported = mod.EXTENDED_TIMEOUT_SOURCE
        proof = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": supported}}
        for source, compare, expected in (
            (supported, None, 360),
            ("1" * 40, proof, 360),
            ("2" * 40, {**proof, "status": "behind"}, 240),
            ("3" * 40, {**proof, "merge_base_commit": {"sha": "9" * 40}}, 240),
            ("4" * 40, OSError("GitHub unavailable"), 240),
            # A long history whose first comparison page is over the response bound (generation
            # fifteen's request expired at 240 minutes on 2026-11-14 for this reason).
            ("5" * 40, {**proof, "oversized": True}, 360),
        ):
            with self.subTest(source=source):
                self.setUp()
                result = self.request("critical", source_sha=source, comparison=compare)
                self.assertEqual(self.cb.starts[0]["timeoutInMinutesOverride"], expected)
                self.assertEqual(result["deadline_unix"], NOW + expected * 60)

    def test_request_queued_before_upgrade_keeps_legacy_project_timeout(self):
        result = self.request(source_sha=mod.EXTENDED_TIMEOUT_SOURCE)
        del self.store.values["/factory/requests/" + INPUT_SHA]["build_timeout_minutes"]
        self.time = result["not_before"]
        self.factory.reconcile()
        self.assertEqual(self.cb.starts[0]["timeoutInMinutesOverride"], 240)
        self.assertEqual(self.factory.request_record(INPUT_SHA)["deadline_unix"], self.time + 14400)

    def test_definitive_start_rejection_releases_only_its_intent_for_new_critical_work(self):
        for code in ("AccountLimitExceededException", "InvalidInputException", "ResourceNotFoundException", "AccessDeniedException"):
            with self.subTest(code=code):
                self.setUp()
                self.cb.builds.clear()
                def reject(**arguments):
                    self.cb.starts.append(arguments)
                    raise mod.ClientError({"Error": {"Code": code}}, "StartBuild")
                with patch.object(self.cb, "start_build", side_effect=reject):
                    try:
                        self.request("critical")
                    except mod.ClientError:
                        pass  # The original bug raises while retaining its queued intent.
                record = self.factory.request_record(INPUT_SHA)
                self.assertEqual(record["status"], "failed")
                self.assertTrue(record["no_build"])
                self.assertIsNone(self.store.values["/factory/control"]["active"])
                self.time += 86400
                result = self.request("critical", "3" * 64)
                self.assertEqual(result["status"], "building")
                self.assertEqual(len(self.cb.starts), 2)
                self.assertEqual(self.cb.starts[-1]["environmentVariablesOverride"][1]["value"], "3" * 64)

    def test_rejected_start_receipt_survives_busy_lock_and_preserves_new_pending_critical(self):
        self.cb.builds.clear()
        def reject(**arguments):
            self.cb.starts.append(arguments)
            self.ddb.busy = True
            raise mod.ClientError({"Error": {"Code": "AccountLimitExceededException"}}, "StartBuild")
        with patch.object(self.cb, "start_build", side_effect=reject):
            try:
                self.request("critical")
            except mod.ClientError:
                pass
        self.assertTrue(self.factory.request_record(INPUT_SHA).get("no_build"))
        self.assertEqual(self.store.values["/factory/control"]["active"], {"intent": INPUT_SHA})
        self.request("critical", "3" * 64)
        self.assertEqual(self.store.values["/factory/control"]["pending"], "3" * 64)
        self.ddb.busy = False
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 2)
        self.assertEqual(self.factory.request_record("3" * 64)["status"], "building")
        self.assertEqual(self.cb.starts[-1]["environmentVariablesOverride"][1]["value"], "3" * 64)

    def test_unknown_start_error_keeps_the_intent_and_discovers_without_replay(self):
        self.cb.builds.clear()
        start = self.cb.start_build
        def uncertain(**arguments):
            start(**arguments)
            raise mod.ClientError({"Error": {"Code": "InternalFailure"}}, "StartBuild")
        with patch.object(self.cb, "start_build", side_effect=uncertain), self.assertRaises(mod.ClientError):
            self.request("critical")
        self.assertFalse(self.factory.request_record(INPUT_SHA).get("no_build"))
        self.assertEqual(self.store.values["/factory/control"]["active"], {"intent": INPUT_SHA})
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 1)
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "building")

    def test_critical_upgrades_same_queued_input_without_six_hour_delay(self):
        self.cb.builds.clear()
        self.request("normal")
        result = self.request("critical")
        self.assertEqual(result["priority"], "critical")
        self.assertEqual(result["status"], "building")
        self.assertEqual(len(self.cb.starts), 1)

    def test_backend_lock_busy_defers_without_starting_or_stopping(self):
        self.ddb.busy = True
        result = self.request("critical")
        self.assertEqual(result["status"], "queued")
        self.assertEqual((self.cb.starts, self.cb.stops, self.ec2.terminations), ([], [], []))

    def test_start_build_network_call_does_not_hold_backend_lock(self):
        original = self.cb.start_build
        def start(**kwargs):
            self.assertFalse(self.ddb.held)
            control = self.store.values["/factory/control"]
            self.assertEqual(control["active"], {"intent": INPUT_SHA})
            return original(**kwargs)
        self.cb.start_build = start
        self.request("critical")
        self.assertFalse(self.ddb.held)

    def test_critical_replacement_waits_for_actual_termination(self):
        instance_id = self.launch()
        self.store.values["/factory/control"] = {"active": BUILD, "pending": None}
        self.store.values["/factory/requests/" + INPUT_SHA] = {"request_id": INPUT_SHA, "source_sha": SOURCE_SHA,
            "input_sha256": INPUT_SHA, "priority": "normal", "status": "building", "build_id": BUILD}
        self.request("critical", "3" * 64)
        self.assertEqual(self.cb.stops, [BUILD])
        self.assertEqual(self.cb.starts, [])
        self.assertEqual(self.store.values["/factory/control"]["active"], BUILD)
        self.ec2.instances[instance_id]["State"]["Name"] = "terminated"
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 1)
        self.assertNotEqual(self.store.values["/factory/control"]["active"], BUILD)

    def test_new_critical_request_is_preserved_through_uncertain_start_binding(self):
        self.cb.builds.clear()
        self.cb.lose_start = True
        with self.assertRaises(TimeoutError):
            self.request("critical")
        self.request("critical", "3" * 64)
        self.assertEqual(self.store.values["/factory/control"]["pending"], "3" * 64)
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 2)
        self.assertEqual(self.cb.starts[-1]["environmentVariablesOverride"][1]["value"], "3" * 64)

    def test_build_cannot_launch_before_actual_id_is_fenced(self):
        self.store.values["/factory/control"]["active"] = {"intent": INPUT_SHA}
        self.assertFalse(self.call("status")["ready"])
        with self.assertRaises(mod.Refused):
            self.launch()
        self.assertEqual(self.ec2.launches, [])

    def test_failed_download_without_resolved_source_cannot_wedge_the_queue(self):
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        self.cb.builds[BUILD]["resolvedSourceVersion"] = None
        self.factory.reconcile()
        self.assertIsNone(self.store.values["/factory/control"]["active"])
        self.assertEqual(self.ec2.launches, [])

    def test_disabled_requests_do_not_start_or_write(self):
        self.factory.env["ENABLED"] = "false"
        result = self.request("critical")
        self.assertEqual(result["status"], "disabled")
        self.assertEqual((self.cb.starts, self.store.writes), ([], []))

    def test_status_response_does_not_disclose_internal_values(self):
        self.store.values["/factory/requests/" + INPUT_SHA] = {"request_id": INPUT_SHA, "status": "promoted", "capability": "must-not-leak", "credentials": {"secret": "must-not-leak"}}
        result = self.factory.request({"action": "status", "request_id": INPUT_SHA})
        self.assertEqual(result, {"request_id": INPUT_SHA, "status": "promoted"})

    def test_expiry_reaper_is_independent_of_the_executor(self):
        instance_id = self.launch()
        foreign = copy.deepcopy(self.ec2.instances[instance_id])
        foreign["InstanceId"] = "i-99999999999999999"
        foreign["IamInstanceProfile"]["Arn"] = "arn:aws:iam::123456789012:instance-profile/production"
        self.ec2.instances[foreign["InstanceId"]] = foreign
        self.time += 21600 + 1
        self.factory.tick()
        self.assertEqual(self.ec2.terminations, [[instance_id]])

    def test_failed_factory_image_is_collected_only_after_termination(self):
        image = self.seal()
        self.fleet_ssm.values["/release"] = {"host_amis": {}, "tile_amis": {}, "previous": None}
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        self.cb.builds[BUILD]["endTime"] = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
        self.ec2.images[image["image_id"]]["BlockDeviceMappings"] = [{"Ebs": {"SnapshotId": "snap-fixture"}}]
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [])
        self.ec2.instances[image["instance_id"]]["State"]["Name"] = "terminated"
        self.time = NOW + 86400 - 1
        self.factory.collect_images()
        self.assertEqual((self.ec2.deleted_images, self.ec2.deleted_snapshots), ([], []))
        self.time += 1
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [image["image_id"]])
        self.assertEqual(self.ec2.deleted_snapshots, ["snap-fixture"])

    def test_image_retention_preserves_release_references(self):
        image = self.seal()
        self.ec2.instances[image["instance_id"]]["State"]["Name"] = "terminated"
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        self.cb.builds[BUILD]["endTime"] = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
        self.time = NOW + 86400
        self.fleet_ssm.values["/release"] = {"host_amis": {"us-east-1": {"x86_64": image["image_id"]}}, "tile_amis": {}, "previous": None}
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [])

    def test_image_retention_preserves_the_copy_records_images_and_reads_templates_in_their_region(self):
        # The factory builds in us-west-2 and releases from copies in us-east-1: the release pointer
        # names only the copies, and the launch templates are in us-east-1.
        image = self.seal()
        self.ec2.instances[image["instance_id"]]["State"]["Name"] = "terminated"
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        self.cb.builds[BUILD]["endTime"] = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
        self.time = NOW + 86400
        self.factory.env.update(COMPUTE_REGION="us-west-2", TEMPLATES_JSON=json.dumps(
            [{"region": "us-east-1", "id": "lt-00000000000000001", "kind": "host", "arch": "x86_64"}]))
        self.fleet_ssm.values["/release"] = {"host_amis": {"us-east-1": {"x86_64": "ami-" + "1" * 17}}, "tile_amis": {}, "previous": None}
        self.fleet_ssm.values["/release-copies"] = {"images": {"us-east-1": {"host": "ami-" + "1" * 17},
                                                              "us-west-2": {"host": image["image_id"]}}}
        asked = []
        class Templates:
            def describe_launch_template_versions(self, LaunchTemplateId, Versions):
                asked.append(LaunchTemplateId)
                return {"LaunchTemplateVersions": [{"LaunchTemplateData": {"ImageId": "ami-" + "1" * 17}}]}
        clients = self.factory.client
        self.factory.client = lambda name, fleet=False, region=None: Templates() if region == "us-east-1" else clients(name, fleet)
        self.factory.collect_images()
        self.assertEqual((self.ec2.deleted_images, asked), ([], ["lt-00000000000000001"]))
        # The previous release's images, kept for rollback, are kept too.
        self.fleet_ssm.values["/release-copies"] = {"images": {}, "previous_images": {"us-west-2": {"host": image["image_id"]}}}
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [])
        del self.fleet_ssm.values["/release-copies"]
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [image["image_id"]], "the record is what kept it")

    def test_the_build_region_bases_a_build_on_the_copy_records_image(self):
        tags = [{"Key": k, "Value": v} for k, v in {"Purpose": "fcvm-fleet", "PipelineSanitized": "true", "FleetImageValidated": "true",
                "FleetImageKind": "host", "GuardHelperSHA256": self.env["GUARD_SHA256"]}.items()]
        self.ec2.images["ami-" + "2" * 17] = {"ImageId": "ami-" + "2" * 17, "OwnerId": self.env["FLEET_ACCOUNT"], "Public": False,
            "State": "available", "Architecture": "x86_64", "RootDeviceName": "/dev/sda1", "Tags": tags}
        self.factory.env["COMPUTE_REGION"] = "us-west-2"
        self.fleet_ssm.values["/release"] = {"host_amis": {"us-east-1": {"x86_64": "ami-" + "1" * 17}}, "tile_amis": {}, "previous": None}
        self.assertEqual(self.factory.base_image("host")["ImageId"], "ami-canonical")
        self.fleet_ssm.values["/release-copies"] = {"images": {"us-west-2": {"host": "ami-" + "2" * 17}}}
        self.assertEqual(self.factory.base_image("host")["ImageId"], "ami-" + "2" * 17)
        self.ec2.images["ami-" + "2" * 17]["State"] = "pending"
        self.assertEqual(self.factory.base_image("host")["ImageId"], "ami-canonical")

    def test_release_parameters_are_read_in_the_release_region(self):
        # The factory builds in us-west-2; the release and its copy record are in us-east-1.
        east = Store()
        east.values["/release"] = {"host_amis": {"us-east-1": {"x86_64": "ami-" + "1" * 17}}, "tile_amis": {}, "previous": None}
        east.values["/release-copies"] = {"images": {"us-west-2": {"host": "ami-" + "2" * 17}}}
        tags = [{"Key": k, "Value": v} for k, v in {"Purpose": "fcvm-fleet", "PipelineSanitized": "true", "FleetImageValidated": "true",
                "FleetImageKind": "host", "GuardHelperSHA256": self.env["GUARD_SHA256"]}.items()]
        self.ec2.images["ami-" + "2" * 17] = {"ImageId": "ami-" + "2" * 17, "OwnerId": self.env["FLEET_ACCOUNT"], "Public": False,
            "State": "available", "Architecture": "x86_64", "RootDeviceName": "/dev/sda1", "Tags": tags}
        self.factory.env.update(COMPUTE_REGION="us-west-2", RELEASE_REGION="us-east-1")
        clients = self.factory.client
        self.factory.client = lambda name, fleet=False, region=None: east if (name, fleet, region) == ("ssm", True, "us-east-1") else clients(name, fleet)
        self.assertEqual(self.factory.base_image("host")["ImageId"], "ami-" + "2" * 17)

    def test_failed_foreign_pr_and_docs_summary_ci_cannot_admit(self):
        for run in [self.ci_run(conclusion="failure"), self.ci_run(event="pull_request"),
                    self.ci_run(head_repository={"full_name": "other/fcvm"}),
                    self.ci_run(path=".github/workflows/ci-docs-summary.yml")]:
            self.store.values.clear()
            self.store.writes.clear()
            responses = [{"object": {"sha": SOURCE_SHA}}, {"workflow_runs": [run]}]
            with patch.object(mod, "public_github", side_effect=responses), self.assertRaises(mod.Refused):
                self.factory.request({"action": "request", "source_sha": SOURCE_SHA, "input_sha256": INPUT_SHA, "priority": "critical"})
            self.assertEqual((self.cb.starts, self.store.writes), ([], []))

    def test_a_green_revision_main_has_moved_past_is_admitted(self):
        # A busy main rarely holds still for its CI: the head's run is cancelled by the next
        # merge or fails on a download, and a head-only rule then builds nothing. A recent
        # ancestor of main with its own successful CI is admitted, even when GitHub lists
        # the newer head's successful run first. No documentation fallback is needed.
        head = "8" * 40
        self.store.values.clear()
        responses = [{"object": {"sha": head}},
            {"status": "ahead", "ahead_by": 3, "behind_by": 0, "merge_base_commit": {"sha": SOURCE_SHA}},
            {"workflow_runs": [self.ci_run(head_sha=head, id=100), self.ci_run()]},
            {"status": "behind", "ahead_by": 0, "behind_by": 3, "merge_base_commit": {"sha": SOURCE_SHA}}]
        with (patch.object(mod, "public_github", side_effect=responses) as github,
              patch.object(mod, "execution_timeout_minutes", return_value=240, create=True)):
            result = self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                "input_sha256": INPUT_SHA, "priority": "critical"})
        self.assertEqual(github.call_count, 3)
        self.assertEqual(result["status"], "building")
        self.assertEqual((result["source_sha"], result["ci_head_sha"], result["ci_run_id"]), (SOURCE_SHA, SOURCE_SHA, 99))
        self.assertEqual(len(self.cb.starts), 1)

    def test_a_revision_off_main_or_far_behind_it_is_refused(self):
        head = "8" * 40
        for comparison in ({"status": "diverged", "ahead_by": 1, "behind_by": 1, "merge_base_commit": {"sha": "7" * 40}},
                           {"status": "behind", "ahead_by": 0, "behind_by": 2, "merge_base_commit": {"sha": head}},
                           {"status": "ahead", "ahead_by": 251, "behind_by": 0, "merge_base_commit": {"sha": SOURCE_SHA}}):
            with self.subTest(comparison=comparison["status"]):
                self.setUp()
                self.store.values.clear()
                responses = [{"object": {"sha": head}}, comparison, {"workflow_runs": [self.ci_run()]}]
                with patch.object(mod, "public_github", side_effect=responses), self.assertRaises(mod.Refused):
                    self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                        "input_sha256": INPUT_SHA, "priority": "critical"})
                self.assertEqual((self.cb.starts, self.store.writes), ([], []))

    def test_a_revision_must_descend_from_the_last_admitted_source(self):
        head, newer = "8" * 40, "6" * 40
        for comparison in ({"status": "behind", "ahead_by": 0, "behind_by": 1, "merge_base_commit": {"sha": SOURCE_SHA}},
                           {"status": "diverged", "ahead_by": 1, "behind_by": 1, "merge_base_commit": {"sha": "7" * 40}}):
            with self.subTest(status=comparison["status"]):
                self.store.values.clear()
                self.factory.save("/control", {"last_source": newer})
                self.store.writes.clear()
                responses = [{"object": {"sha": head}},
                    {"status": "ahead", "ahead_by": 3, "behind_by": 0, "merge_base_commit": {"sha": SOURCE_SHA}},
                    comparison]
                with patch.object(mod, "public_github", side_effect=responses), self.assertRaisesRegex(mod.Refused, "already admitted"):
                    self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                        "input_sha256": INPUT_SHA, "priority": "critical"})
                self.assertEqual((self.cb.starts, self.store.writes), ([], []))

    def test_legacy_control_keeps_newer_work_when_an_old_critical_request_arrives(self):
        older, head, old_input = "9" * 40, "8" * 40, "7" * 64
        for slot, control in (("active", {"active": BUILD}),
                              ("intent", {"active": {"intent": INPUT_SHA}}),
                              ("pending", {"pending": INPUT_SHA})):
            with self.subTest(slot=slot):
                self.setUp()
                self.store.values["/factory/control"] = control
                if slot == "active":
                    self.factory.journal(BUILD)
                if slot == "pending":
                    self.store.values["/factory/requests/" + INPUT_SHA]["status"] = "queued"
                self.store.writes.clear()
                before = copy.deepcopy(self.store.values)

                def response(path):
                    if path == "git/ref/heads/main":
                        return {"object": {"sha": head}}
                    if path.startswith("compare/" + older + "..." + head):
                        return {"status": "ahead", "ahead_by": 3, "behind_by": 0, "merge_base_commit": {"sha": older}}
                    if path.startswith("compare/" + SOURCE_SHA + "..." + older):
                        return {"status": "behind", "ahead_by": 0, "behind_by": 1, "merge_base_commit": {"sha": older}}
                    return {"workflow_runs": [self.ci_run(head_sha=older)]}

                with patch.object(mod, "public_github", side_effect=response), self.assertRaisesRegex(mod.Refused, "already admitted"):
                    self.factory.request({"action": "request", "source_sha": older, "input_sha256": old_input, "priority": "critical"})
                self.assertEqual((self.cb.starts, self.cb.stops, self.store.writes), ([], [], []))
                self.assertEqual(self.store.values, before)

    def test_legacy_control_with_unverifiable_work_refuses_a_non_head_request(self):
        for control in ({"pending": "7" * 64}, {"active": {}}, {"active": BUILD}):
            with self.subTest(control=control):
                self.setUp()
                self.store.values["/factory/control"] = control
                self.cb.builds[BUILD].pop("resolvedSourceVersion")
                responses = [{"object": {"sha": "8" * 40}},
                    {"status": "ahead", "ahead_by": 1, "behind_by": 0, "merge_base_commit": {"sha": SOURCE_SHA}}]
                with patch.object(mod, "public_github", side_effect=responses), self.assertRaises(mod.Refused):
                    self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                        "input_sha256": "7" * 64, "priority": "critical"})
                self.assertEqual((self.cb.starts, self.cb.stops, self.store.writes), ([], [], []))

    def test_later_green_revision_can_replace_legacy_queued_work(self):
        later, head, later_input = "6" * 40, "8" * 40, "7" * 64
        self.store.values["/factory/control"] = {"pending": INPUT_SHA}
        self.store.values["/factory/requests/" + INPUT_SHA]["status"] = "queued"
        responses = [{"object": {"sha": head}},
            {"status": "ahead", "ahead_by": 2, "behind_by": 0, "merge_base_commit": {"sha": later}},
            {"status": "ahead", "ahead_by": 1, "behind_by": 0, "merge_base_commit": {"sha": SOURCE_SHA}},
            {"workflow_runs": [self.ci_run(head_sha=later)]}]
        with (patch.object(mod, "public_github", side_effect=responses),
              patch.object(mod, "execution_timeout_minutes", return_value=240, create=True)):
            result = self.factory.request({"action": "request", "source_sha": later, "input_sha256": later_input, "priority": "critical"})
        self.assertEqual((result["status"], result["source_sha"]), ("building", later))
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "superseded")
        self.assertEqual(self.factory.load("/control")["last_source"], later)

    def test_current_head_does_not_need_legacy_admission_proof(self):
        self.store.values.clear()
        self.store.values["/factory/control"] = {"last_source": "unverified"}
        responses = [{"object": {"sha": SOURCE_SHA}}, {"workflow_runs": [self.ci_run()]}]
        with (patch.object(mod, "public_github", side_effect=responses),
              patch.object(mod, "execution_timeout_minutes", return_value=240, create=True)):
            result = self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                "input_sha256": INPUT_SHA, "priority": "critical"})
        self.assertEqual(result["status"], "building")
        self.assertEqual(self.factory.load("/control")["last_source"], SOURCE_SHA)

    def test_green_ancestor_covers_only_complete_docs_changes(self):
        previous = "9" * 40
        run = self.ci_run(head_sha=previous)
        def check(files, **changes):
            comparison = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": previous}, "total_commits": 1,
                          "files": files, **changes}
            with patch.object(mod, "public_github", side_effect=[{"workflow_runs": [run]}, comparison]):
                return mod.trusted_ci(SOURCE_SHA)
        self.assertEqual(check([{"filename": "README.md"}])["ci_head_sha"], previous)
        for files in ([{"filename": "src/main.rs"}], [{"filename": "README.md", "previous_filename": "fleet/old.sh"}],
                      [{"filename": "README.md"}] * 300):
            with self.assertRaises(mod.Refused):
                check(files)
        with self.assertRaises(mod.Refused):
            check([{"filename": "README.md"}], total_commits=251)

    def test_green_ancestor_cannot_admit_changes_to_any_local_rust_dependency(self):
        previous = "9" * 40
        for directory in ("fuse-pipe", "exec-proto", "failpoint"):
            with self.subTest(directory=directory):
                self.setUp()
                self.store.values.clear()
                comparison = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": previous},
                    "total_commits": 1, "files": [{"filename": directory + "/src/lib.rs"}]}
                responses = [{"object": {"sha": SOURCE_SHA}},
                    {"workflow_runs": [self.ci_run(head_sha=previous)]}, comparison]
                with patch.object(mod, "public_github", side_effect=responses), self.assertRaises(mod.Refused):
                    self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                        "input_sha256": INPUT_SHA, "priority": "critical"})
                self.assertEqual((self.cb.starts, self.store.writes), ([], []))

    def test_ancestor_ci_refuses_build_controls_and_wrapper_renames_before_writes(self):
        previous = "9" * 40
        for changed in ({"filename": ".cargo/config.toml"}, {"filename": "rust-toolchain.toml"},
                        {"filename": "nested.sh"}, {"filename": "docs/nested.md", "previous_filename": "nested.sh"},
                        {"filename": "docs/build.sh"}, {"filename": ".claude/CLAUDE.md"}):
            with self.subTest(changed=changed):
                self.setUp()
                self.store.values.clear()
                comparison = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": previous},
                    "total_commits": 1, "files": [changed]}
                responses = [{"object": {"sha": SOURCE_SHA}},
                    {"workflow_runs": [self.ci_run(head_sha=previous)]}, comparison]
                with patch.object(mod, "public_github", side_effect=responses), self.assertRaises(mod.Refused):
                    self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                        "input_sha256": INPUT_SHA, "priority": "critical"})
                self.assertEqual((self.cb.starts, self.store.writes), ([], []))

    def test_ancestor_ci_admits_only_root_and_docs_markdown_including_safe_renames(self):
        previous = "9" * 40
        for changed in ({"filename": "README.md"}, {"filename": "docs/guide.md"},
                        {"filename": "docs/new.md", "previous_filename": "docs/old.md"}):
            with self.subTest(changed=changed):
                self.setUp()
                self.store.values.clear()
                comparison = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": previous},
                    "total_commits": 1, "files": [changed]}
                responses = [{"object": {"sha": SOURCE_SHA}},
                    {"workflow_runs": [self.ci_run(head_sha=previous)]}, comparison, {"status": "behind"}]
                with patch.object(mod, "public_github", side_effect=responses):
                    result = self.factory.request({"action": "request", "source_sha": SOURCE_SHA,
                        "input_sha256": INPUT_SHA, "priority": "critical"})
                self.assertEqual(result["status"], "building")
                self.assertEqual(self.factory.request_record(INPUT_SHA)["ci_head_sha"], previous)
                self.assertEqual(len(self.cb.starts), 1)


    # The fleet account's agents read why a request failed from FAILURE_PARAMETER (ejc3/aws#823):
    # fixed failure codes and validated phase metadata, without raw log text.
    def failure_fixture(self, *, logs_fail=False, diagnostics_fail=False, version_id="fixture-version"):
        self.factory.env.update(FAILURE_PARAMETER="/fcvm-fleet/ami-factory-failure", PIPELINE_REGION="us-east-2")
        instance_id = self.launch()
        self.cb.builds[BUILD].update(buildStatus="FAILED", logs={"groupName": "/aws/codebuild/factory", "streamName": "stream-1"})
        tail = "\n".join([
            "== build start 2026-11-12T21:58:40Z",
            "make: Leaving directory '/home/ubuntu/fcvm/fleet/phone'",
            "fetching https://fixture.invalid/object?X-Amz-Signature=abc&X-Amz-Credential=AKIDFIXTURE from 10.21.41.120",
            "Authorization: Bearer fixture-bearer-value",
            "Error: rdp rejected Q3Z4mW9P2h8N",
            '{"error":"M7vSp8R4K2dQ"}',
            "Error: no space left on device D3vmQ8w2R5pN",
            "host ip-10-21-41-120 arn:aws:ec2:us-west-2:123456789012:instance/" + instance_id + " prefix 2600:1f14:abc:de00::/56",
            "warm-phone: cold boot ok=0 streaming_ms=61234 gpu=0000:18:00.0",
            '{"error":"the app did not start: adb could not install /work/robotpark-0123456789abcdef.apk: '
            'adb: failed to install /work/robotpark-0123456789abcdef.apk: Failure [INSTALL_FAILED_FIXTURE: reason]"}',
            "PHASE build FAILED (exit 1)",
        ]) + "\n"
        events = [{"message": "[Container] 2026/11/12 23:14:30 Running command python fleet/ami/pipeline.py"},
                  {"message": json.dumps({"phaseDiagnostics": {"kind": "host", "phase": "build", "sha256": "e" * 64,
                                                               "size": 1234, "version_id": version_id}})},
                  {"message": "Traceback (most recent call last):"},
                  {"message": "__main__.BuilderCommandFailed: the isolated builder command failed S8nV9kQ2R4zM"}]
        reads = []
        lock = self.ddb
        test = self

        class Logs:
            def get_log_events(self, **kwargs):
                reads.append(("logs", kwargs))
                if logs_fail:
                    raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetLogEvents")
                return {"events": events}

        class S3:
            def get_object(self, **kwargs):
                reads.append(("s3", kwargs))
                if diagnostics_fail:
                    raise ClientError({"Error": {"Code": "NoSuchVersion"}}, "GetObject")
                return {"Body": io.BytesIO(tail.encode())}

        class FleetSSM(Store):
            def put_parameter(self, Name, Value, **kwargs):
                test.assertFalse(lock.held, "the failure is published outside the backend lock")
                return super().put_parameter(Name, Value, **kwargs)

        fleet_ssm = FleetSSM()
        fleet_ssm.values = self.fleet_ssm.values
        clients = {("ssm", False): self.store, ("ssm", True): fleet_ssm, ("ec2", True): self.ec2,
                   ("codebuild", False): self.cb, ("sts", False): self.sts, ("dynamodb", False): self.ddb,
                   ("lambda", False): self.publisher, ("logs", False): Logs()}
        def client(name, fleet=False, region=None):
            if name == "s3":
                self.assertEqual((fleet, region), (False, "us-east-2"), "diagnostics are read from the factory's bucket")
                return S3()
            return clients[(name, fleet)]
        self.factory.client = client
        self.factory.reconcile()
        return instance_id, reads, fleet_ssm

    def test_failure_parameter_never_copies_short_log_values(self):
        _, _, fleet_ssm = self.failure_fixture()
        encoded = json.dumps(fleet_ssm.values["/fcvm-fleet/ami-factory-failure"])
        for opaque in ("Q3Z4mW9P2h8N", "M7vSp8R4K2dQ", "D3vmQ8w2R5pN", "S8nV9kQ2R4zM"):
            with self.subTest(opaque=opaque):
                self.assertNotIn(opaque, encoded)

    def test_a_failed_request_publishes_its_sanitized_cause_to_the_fleet(self):
        instance_id, reads, fleet_ssm = self.failure_fixture()
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "failed")
        value = fleet_ssm.values["/fcvm-fleet/ami-factory-failure"]
        self.assertEqual((value["version"], value["request_id"], value["source_sha"], value["status"], value["build_status"]),
                         (1, INPUT_SHA, SOURCE_SHA, "failed", "FAILED"))
        self.assertEqual(value["reason"], "build_failed_or_expired")
        self.assertEqual(value["executor_error"], "builder_command_failed")
        self.assertEqual(value["phase"], {"kind": "host", "phase": "build"})
        self.assertEqual(value["unavailable"], [])
        s3 = [kwargs for kind, kwargs in reads if kind == "s3"]
        self.assertEqual(len(s3), 1)
        self.assertEqual((s3[0]["Bucket"], s3[0]["VersionId"]), ("fixture-factory", "fixture-version"))
        self.assertEqual(s3[0]["Key"], "builders/arn:aws:ec2:us-east-1:123456789012:instance/" + instance_id + "/diagnostics/host-build.log")
        self.assertEqual(value["diagnostic_codes"], ["disk_full", "adb_install_failed"])
        encoded = json.dumps(value)
        for private in ("123456789012", instance_id, "10.21.41.120", "2600:1f14", "fixture.invalid", "AKIDFIXTURE",
                        "fixture-bearer-value", "fixture-version", "/aws/codebuild", "stream-1"):
            self.assertNotIn(private, encoded)
        self.assertLessEqual(len(json.dumps(value, separators=(",", ":")).encode()), 4000)
        # Published once: a later reconcile of the failed request leaves the parameter alone.
        writes = len(fleet_ssm.writes)
        self.factory.reconcile()
        self.assertEqual(len(fleet_ssm.writes), writes)

    def test_an_unreadable_log_or_diagnostics_still_publishes_and_says_so(self):
        _, _, fleet_ssm = self.failure_fixture(logs_fail=True)
        value = fleet_ssm.values["/fcvm-fleet/ami-factory-failure"]
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "failed")
        self.assertEqual((value["executor_error"], value["phase"], value["diagnostic_codes"]), (None, None, []))
        self.assertEqual(value["unavailable"], ["executor_log"])
        self.setUp()
        _, _, fleet_ssm = self.failure_fixture(diagnostics_fail=True)
        value = fleet_ssm.values["/fcvm-fleet/ami-factory-failure"]
        self.assertEqual(value["phase"], {"kind": "host", "phase": "build"})
        self.assertEqual((value["diagnostic_codes"], value["unavailable"]), ([], ["phase_diagnostics"]))
        self.setUp()
        _, reads, fleet_ssm = self.failure_fixture(version_id="null")
        value = fleet_ssm.values["/fcvm-fleet/ami-factory-failure"]
        self.assertEqual(value["phase"], {"kind": "host", "phase": "build"})
        self.assertEqual(value["unavailable"], ["phase_diagnostics"])
        self.assertFalse(any(kind == "s3" for kind, _ in reads), "never read an unversioned archive")

    def test_failure_parameter_keeps_only_recognized_metadata(self):
        _, _, fleet_ssm = self.failure_fixture()
        self.factory.publish_failure(
            {"request_id": INPUT_SHA, "status": "failed", "source_sha": "another private value",
             "message": "some private error detail"},
            build={"buildStatus": "unexpected private status"})
        value = fleet_ssm.values["/fcvm-fleet/ami-factory-failure"]
        self.assertEqual((value["reason"], value["source_sha"], value["build_status"]), ("unknown_failure", None, None))
        self.assertNotIn("private", json.dumps(value))

    def test_viewer_path_failure_keeps_only_its_code(self):
        class Logs:
            def get_log_events(self, **kwargs):
                return {"events": [{"message": "RuntimeError: candidate viewer path failed: example private detail"}]}
        self.factory.client = lambda *args: Logs()
        self.assertEqual(self.factory.executor_failure({"logs": {"groupName": "group", "streamName": "stream"}}),
                         ("viewer_path_failed", None))

    def test_publisher_rollback_reports_once_during_repeated_cleanup(self):
        for lease_expired in (False, True):
            with self.subTest(lease_expired=lease_expired):
                self.setUp()
                path = "/fcvm-fleet/ami-factory-failure"
                self.factory.env["FAILURE_PARAMETER"] = path
                ended = self.succeed()
                if lease_expired:
                    self.time = ended + mod.PUBLICATION_SECONDS + 1
                record = self.store.values["/factory/requests/" + INPUT_SHA]
                record.update(status="failed", message="publication rolled back; automatic replay refused")
                # EC2 termination has not settled on the first two reconciliation ticks.
                self.factory.gone = lambda *args: False
                self.factory.reconcile()
                self.factory.reconcile()
                self.factory.gone = lambda *args: True
                self.factory.reconcile()
                self.assertEqual(self.fleet_ssm.writes.count(path), 1)
                status = self.fleet_ssm.values[path]
                self.assertEqual((status["reason"], status["build_status"]), ("publication_rolled_back", "SUCCEEDED"))
                self.assertEqual(self.factory.request_record(INPUT_SHA)["message"],
                                 "publication rolled back; automatic replay refused")
                self.assertIsNone(self.store.values["/factory/control"]["active"])
                self.assertEqual(self.publisher.calls, [], "a rolled-back request cannot be replayed")

    def test_failures_are_published_newest_last_after_the_queue_settles(self):
        # A failed build is retired and the pending request's start is refused in one reconcile:
        # the parameter ends on the refused request, and it is written once the queue holds
        # neither intent.
        self.factory.env.update(FAILURE_PARAMETER="/fcvm-fleet/ami-factory-failure")
        self.factory.journal(BUILD)
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        newer = "5" * 64
        self.store.values["/factory/requests/" + newer] = {"request_id": newer, "status": "queued", "source_sha": SOURCE_SHA,
            "input_sha256": newer, "priority": "normal", "build_id": None, "not_before": 0}
        self.store.values["/factory/control"]["pending"] = newer
        def refuse(**kwargs):
            raise ClientError({"Error": {"Code": "AccountLimitExceededException"}}, "StartBuild")
        self.cb.start_build = refuse
        order = []
        test = self
        class FleetSSM(Store):
            def put_parameter(self, Name, Value, **kwargs):
                control = test.store.values["/factory/control"]
                test.assertIsNone(control.get("active"), "published while the queue still held an intent")
                order.append(json.loads(Value)["request_id"])
                return super().put_parameter(Name, Value, **kwargs)
        fleet = FleetSSM()
        clients = {("ssm", False): self.store, ("ssm", True): fleet, ("ec2", True): self.ec2,
                   ("codebuild", False): self.cb, ("dynamodb", False): self.ddb, ("lambda", False): self.publisher}
        self.factory.client = lambda name, fleet=False, region=None: clients[(name, fleet)]
        self.factory.reconcile()
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "failed")
        self.assertEqual(self.factory.request_record(newer)["status"], "failed")
        self.assertEqual(order, [INPUT_SHA, newer])
        self.assertEqual(fleet.values["/fcvm-fleet/ami-factory-failure"]["request_id"], newer)
        self.assertEqual(fleet.values["/fcvm-fleet/ami-factory-failure"]["reason"], "start_rejected")

    def test_failure_parameter_is_the_fleet_accounts_and_only_the_dispatcher_writes_it(self):
        source = (SOURCE.parents[1] / "fcvm-fleet-ami-pipeline.tf").read_text()
        parameter = re.search(r'^resource "aws_ssm_parameter" "fcvm_ami_failure" \{\n.*?^\}', source, re.S | re.M).group()
        self.assertIn("provider    = aws.fcvm_fleet_east1", parameter)
        self.assertIn('name        = "/fcvm-fleet/ami-factory-failure"', parameter)
        self.assertNotIn("ami-pipeline", re.search(r'name\s*=\s*"([^"]+)"', parameter).group(1),
                         "the fleet's role cannot read the factory's /fcvm-fleet/ami-pipeline/ state")
        self.assertIn("ignore_changes = [value]", parameter)
        broker = re.search(r'^resource "aws_iam_role_policy" "fcvm_ami_broker_fleet" \{\n.*?^\}', source, re.S | re.M).group()
        self.assertIn('{ Sid = "SanitizedFailureReason", Effect = "Allow", Action = "ssm:PutParameter", '
                      'Resource = aws_ssm_parameter.fcvm_ami_failure.arn }', broker)
        dispatch = re.search(r'^resource "aws_iam_role_policy" "fcvm_ami_dispatch" \{\n.*?^\}', source, re.S | re.M).group()
        self.assertIn('Action = "logs:GetLogEvents", Resource = "${aws_cloudwatch_log_group.fcvm_ami_codebuild.arn}:log-stream:*"', dispatch)
        self.assertIn('Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.fcvm_ami_pipeline.arn}/builders/*/diagnostics/*"', dispatch)
        function = re.search(r'^resource "aws_lambda_function" "fcvm_ami_dispatch" \{\n.*?^\}', source, re.S | re.M).group()
        self.assertRegex(function, r"FAILURE_PARAMETER\s*=\s*aws_ssm_parameter\.fcvm_ami_failure\.name")
        self.assertRegex(function, r"PIPELINE_REGION\s*=\s*local\.fcvm_fleet_home_region")
        self.assertRegex(function, r'RELEASE_REGION\s*=\s*local\.fcvm_ami_region')
        self.assertRegex(source, r'fcvm_ami_region\s*=\s*"us-east-1"')

    def retired_pending(self):
        source = "3e36a3a36e13fdb03168b77ff6257a707e6654d1"
        request = "/factory/requests/" + INPUT_SHA
        control = {"active": None, "pending": INPUT_SHA, "last_normal_day": "2026-11-13", "last_source": source}
        self.store.values["/factory/control"] = control
        self.store.values[request].update(status="queued", priority="normal", build_id=None, not_before=NOW - 1,
                                          source_sha=source)
        self.queue_prepared = True
        return source, request, control

    def test_retired_source_stays_queued_while_paused_and_never_starts_on_unpause(self):
        source, request, control = self.retired_pending()
        before = copy.deepcopy(self.store.values)
        self.factory.env["ENABLED"] = "false"
        self.factory.reconcile()
        self.assertEqual(self.request("critical", "6" * 64)["status"], "disabled")
        self.assertEqual((self.store.values, self.store.writes, self.cb.starts), (before, [], []))

        self.factory.env["ENABLED"] = "true"
        status = self.factory.request({"action": "status", "request_id": INPUT_SHA})
        self.assertEqual((status["status"], self.store.values["/factory/control"]),
                         ("superseded", {**control, "pending": None}))
        self.assertIn("fleet host fails to load boot user data", status["message"])
        self.assertEqual((self.store.writes, self.cb.starts, self.cb.stops), ([request, "/factory/control"], [], []))

        self.store.writes.clear()
        self.factory.reconcile()
        with patch.object(mod, "public_github") as github, self.assertRaisesRegex(mod.Refused, "source retired"):
            self.factory.request({"action": "request", "source_sha": source, "input_sha256": INPUT_SHA, "priority": "critical"})
        github.assert_not_called()
        self.assertEqual((self.store.writes, self.cb.starts, self.cb.stops), ([], [], []))

        fixed_source = "d613d813befeac90e1a38924bf909056024f5a12"
        fixed = self.request("critical", "6" * 64, source_sha=fixed_source)
        self.assertEqual(fixed["status"], "building")
        self.assertEqual([build["sourceVersion"] for build in self.cb.starts], [fixed_source])

    def test_retired_source_retries_terminal_write_and_keeps_uncertain_work(self):
        _, request, control = self.retired_pending()
        original_put = self.store.put_parameter

        def interrupted_put(Name, Value, **kw):
            if Name == "/factory/control":
                raise TimeoutError("control write interrupted")
            return original_put(Name, Value, **kw)

        with patch.object(self.store, "put_parameter", side_effect=interrupted_put), self.assertRaises(TimeoutError):
            self.factory.reconcile()
        self.assertEqual(self.store.values[request]["status"], "superseded")
        self.assertEqual(self.store.values["/factory/control"], control)
        self.factory.reconcile()
        self.assertEqual(self.store.values["/factory/control"], {**control, "pending": None})
        self.assertEqual(self.cb.starts, [])

        for fields in ({"start_attempted": NOW - 20}, {"build_id": BUILD}, {"status": "awaiting-publish"}):
            with self.subTest(fields=fields):
                self.setUp()
                _, request, _ = self.retired_pending()
                self.store.values[request].update(fields)
                saved = copy.deepcopy(self.store.values)
                with self.assertRaisesRegex(mod.Refused, "uncertain start|unexpected status"):
                    self.factory.reconcile()
                self.assertEqual((self.store.values, self.store.writes, self.cb.starts, self.cb.stops),
                                 (saved, [], [], []))

    def test_retired_source_preserves_an_active_intent_with_unknown_start_result(self):
        _, request, control = self.retired_pending()
        self.store.values[request]["start_attempted"] = NOW - 20
        control["active"] = {"intent": INPUT_SHA}
        self.cb.builds.clear()  # CodeBuild may have accepted StartBuild without a visible result yet.
        before = copy.deepcopy(self.store.values)
        self.factory.env["ENABLED"] = "false"
        self.factory.reconcile()
        self.factory.env["ENABLED"] = "true"
        self.factory.reconcile()
        self.assertEqual((self.store.values, self.store.writes, self.cb.starts, self.cb.stops),
                         (before, [], [], []))

    def test_a_paused_factory_starts_no_build_and_keeps_the_queue(self):
        # The pause reaches the dispatcher as ENABLED=false, which starts nothing, while the launch
        # templates still follow the release pointer. It was on from generation twelve (#823) until
        # ejc3/fcvm had the boot fix and the launch-path gate; it is off again.
        source = (SOURCE.parents[1] / "fcvm-fleet-ami-pipeline.tf").read_text()
        variable = re.search(r'^variable "fcvm_ami_factory_paused" \{\n.*?^\}', source, re.S | re.M).group()
        self.assertRegex(variable, r"default\s*=\s*false")
        self.assertIn("ENABLED           = tostring(var.fcvm_ami_pipeline_enabled && !var.fcvm_ami_factory_paused)", source)
        self.assertNotIn("fcvm_ami_factory_paused", (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text())
        # what ENABLED=false does with a queued normal request whose window has opened
        self.store.values.pop("/factory/control", None)
        self.store.values.pop("/factory/requests/" + INPUT_SHA, None)
        self.queue_prepared = True
        queued = self.request("normal")
        self.assertEqual(queued["status"], "queued")
        self.factory.env["ENABLED"] = "false"
        self.time += 7 * 3600
        self.factory.reconcile()
        self.assertEqual(self.cb.starts, [])
        self.assertEqual(self.factory.request_record(INPUT_SHA)["status"], "queued")
        self.assertEqual(self.request("critical", "6" * 64)["status"], "disabled")
        self.assertEqual(self.cb.starts, [])

if __name__ == "__main__":
    unittest.main(buffer=True)
