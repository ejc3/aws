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
        build["startTime"] = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc)
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
            else:
                raise AssertionError("unexpected fixture filter")
        return {"Reservations": [{"Instances": copy.deepcopy(rows)}]}

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

    def request(self, priority="normal", input_sha=INPUT_SHA):
        if not self.queue_prepared and not self.ec2.launches:
            self.store.values.pop("/factory/control", None)
            self.store.values.pop("/factory/requests/" + INPUT_SHA, None)
        self.queue_prepared = True
        def response(path):
            if path == "git/ref/heads/main":
                return {"object": {"sha": SOURCE_SHA}}
            return {"workflow_runs": [self.ci_run()]}
        with patch.object(mod, "public_github", side_effect=response):
            return self.factory.request({"action": "request", "source_sha": SOURCE_SHA, "input_sha256": input_sha, "priority": priority})

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
        ended = self.succeed(mod.DEADLINE_SECONDS - 2)
        self.time = NOW + mod.DEADLINE_SECONDS + 10
        self.factory.reconcile()
        record = self.factory.request_record(INPUT_SHA)
        self.assertEqual(record["status"], "awaiting-publish")
        self.assertEqual(record["publication_deadline_unix"], ended + 86400)
        self.assertEqual(self.store.values["/factory/control"]["active"], BUILD)
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.cb.stops, [])

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
        self.time += mod.DEADLINE_SECONDS + 1
        for action in ("launch", "start", "reboot", "credentials"):
            with self.assertRaises(mod.Refused):
                self.call(action, "host-build")
        self.call("cleanup")
        self.assertEqual(self.ec2.terminations, [[instance_id]])

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

    def test_lost_start_response_never_starts_another_build(self):
        self.cb.builds.clear()
        self.cb.lose_start = True
        with self.assertRaises(TimeoutError):
            self.request("critical")
        self.factory.reconcile()
        self.assertEqual(len(self.cb.starts), 1)
        record = self.factory.request_record(INPUT_SHA)
        self.assertEqual(record["status"], "building")
        self.assertEqual(record["build_id"], "factory:22222222-2222-2222-2222-222222222222")

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
        self.time += mod.DEADLINE_SECONDS + 1
        self.factory.tick()
        self.assertEqual(self.ec2.terminations, [[instance_id]])

    def test_failed_factory_image_is_collected_only_after_termination(self):
        image = self.seal()
        self.fleet_ssm.values["/release"] = {"host_amis": {}, "tile_amis": {}, "previous": None}
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [])
        self.ec2.instances[image["instance_id"]]["State"]["Name"] = "terminated"
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [image["image_id"]])

    def test_image_retention_preserves_release_references(self):
        image = self.seal()
        self.ec2.instances[image["instance_id"]]["State"]["Name"] = "terminated"
        self.cb.builds[BUILD]["buildStatus"] = "FAILED"
        self.fleet_ssm.values["/release"] = {"host_amis": {"us-east-1": {"x86_64": image["image_id"]}}, "tile_amis": {}, "previous": None}
        self.factory.collect_images()
        self.assertEqual(self.ec2.deleted_images, [])

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


if __name__ == "__main__":
    unittest.main(buffer=True)
