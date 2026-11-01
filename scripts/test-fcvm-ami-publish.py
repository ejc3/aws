#!/usr/bin/env python3
"""Credential-free publisher transactions against strict in-memory AWS fakes."""
import base64
import copy
import datetime
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "fcvm-fleet-ami-pipeline/publish.py"
SPEC = importlib.util.spec_from_file_location("fleet_image_publisher", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ParameterNotFound(Exception):
    response = {"Error": {"Code": "ParameterNotFound"}}


class SSM:
    def __init__(self):
        self.values = {}

    def get_parameter(self, Name, WithDecryption):
        assert WithDecryption is False
        if Name not in self.values:
            raise ParameterNotFound()
        return {"Parameter": {"Value": MODULE.encoded(self.values[Name])}}

    def put_parameter(self, Name, Type, Value, Overwrite):
        assert Type == "String" and Overwrite is True
        self.values[Name] = json.loads(Value)
        return {"Version": 1}


class DDB:
    def __init__(self):
        self.item = None
        self.puts = 0

    def put_item(self, TableName, Item, ConditionExpression):
        self.puts += 1
        assert TableName == "locks" and ConditionExpression == "attribute_not_exists(LockID)"
        if self.item is not None:
            raise RuntimeError("locked")
        self.item = copy.deepcopy(Item)

    def delete_item(self, TableName, Key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        assert TableName == "locks" and Key == {"LockID": self.item["LockID"]}
        assert ConditionExpression == "#info = :ours" and ExpressionAttributeNames == {"#info": "Info"}
        assert ExpressionAttributeValues[":ours"] == self.item["Info"]
        self.item = None


class S3:
    def __init__(self, source=None):
        self.objects = {}
        self.source = source
        self.copies = []
        self.bad_copy_checksum = False

    def get_object(self, Bucket, Key, VersionId=None, **_kwargs):
        data, metadata, version = self.objects[Key]
        if VersionId is not None:
            assert VersionId == version
        return {"Body": io.BytesIO(data), "VersionId": version}

    def head_object(self, Bucket, Key, VersionId=None, **_kwargs):
        data, metadata, version = self.objects[Key]
        if VersionId is not None:
            assert VersionId == version
        value = {"ContentLength": len(data), "Metadata": metadata, "VersionId": version, "ETag": "etag-" + version}
        if _kwargs.get("ChecksumMode") == "ENABLED":
            value["ChecksumSHA256"] = base64.b64encode(hashlib.sha256(data).digest()).decode()
        return value

    def list_objects_v2(self, Bucket, Prefix, **_kwargs):
        return {"Contents": [{"Key": name} for name in self.objects if name.startswith(Prefix)]}

    def copy_object(self, Bucket, Key, CopySource, CopySourceIfMatch, Metadata, **_kwargs):
        assert Key not in self.objects, "a different destination must never be overwritten"
        data, _metadata, version = self.source.objects[CopySource["Key"]]
        assert CopySource["VersionId"] == version and CopySourceIfMatch == "etag-" + version
        self.objects[Key] = (data, Metadata, "copied-" + version)
        self.copies.append(Key)
        return {"VersionId": "copied-" + version, "CopySourceVersionId": version,
                "CopyObjectResult": {"ChecksumSHA256": "wrong" if self.bad_copy_checksum
                                     else base64.b64encode(hashlib.sha256(data).digest()).decode()}}


class EC2:
    def __init__(self):
        self.templates, self.images, self.snapshots, self.instances = {}, {}, {}, {}
        self.created, self.selected, self.deleted, self.terminated = [], [], [], []
        self.fail_select = None
        self.fail_terminate = False
        self.corrupt_candidate = False
        self.lose_create_response = False
        self.unknown_create_response = False
        self.spot_requests = {}
        self.cancelled_spot = []
        self.reopened_spot = []
        self.runtime_order = []
        self.fail_cancel = False

    def describe_instances(self, InstanceIds=None, **_kwargs):
        values = [self.instances[item] for item in InstanceIds] if InstanceIds else list(self.instances.values())
        return {"Reservations": [{"OwnerId": "222222222222", "Instances": copy.deepcopy(values)}]}

    def describe_images(self, ImageIds):
        return {"Images": [copy.deepcopy(self.images[name]) for name in ImageIds]}

    def describe_snapshots(self, SnapshotIds):
        return {"Snapshots": [copy.deepcopy(self.snapshots[name]) for name in SnapshotIds]}

    def describe_launch_templates(self, LaunchTemplateIds):
        return {"LaunchTemplates": [{"LaunchTemplateId": key, "DefaultVersionNumber": self.templates[key]["default"]}
                                    for key in LaunchTemplateIds]}

    def describe_launch_template_versions(self, LaunchTemplateId, Versions=None, **_kwargs):
        values = self.templates[LaunchTemplateId]["versions"]
        return {"LaunchTemplateVersions": [copy.deepcopy(values[int(value)]) for value in Versions] if Versions else copy.deepcopy(list(values.values()))}

    def create_launch_template_version(self, LaunchTemplateId, SourceVersion, LaunchTemplateData, VersionDescription, ClientToken):
        assert set(LaunchTemplateData) == {"ImageId"} and SourceVersion.isdigit()
        assert ClientToken == MODULE.digest(VersionDescription)
        if self.unknown_create_response:
            raise RuntimeError("uncertain without visible version")
        template = self.templates[LaunchTemplateId]
        data = copy.deepcopy(template["versions"][int(SourceVersion)]["LaunchTemplateData"])
        data.update(LaunchTemplateData)
        if self.corrupt_candidate:
            data["SecurityGroupIds"] = ["unexpected-group"]
        number = max(template["versions"]) + 1
        value = {"LaunchTemplateId": LaunchTemplateId, "VersionNumber": number,
                 "VersionDescription": VersionDescription, "LaunchTemplateData": data}
        template["versions"][number] = value
        self.created.append((LaunchTemplateId, int(SourceVersion), number))
        if self.lose_create_response:
            self.lose_create_response = False
            raise RuntimeError("lost after creation")
        return {"LaunchTemplateVersion": copy.deepcopy(value)}

    def modify_launch_template(self, LaunchTemplateId, DefaultVersion, ClientToken):
        if self.fail_select == LaunchTemplateId:
            self.fail_select = None
            raise RuntimeError("selection failed")
        self.templates[LaunchTemplateId]["default"] = int(DefaultVersion)
        self.selected.append((LaunchTemplateId, int(DefaultVersion)))
        return {}

    def delete_launch_template_versions(self, LaunchTemplateId, Versions):
        template = self.templates[LaunchTemplateId]
        for value in Versions:
            assert int(value) != template["default"]
            assert template["versions"][int(value)]["VersionDescription"].startswith("fleet-image-")
            del template["versions"][int(value)]
            self.deleted.append((LaunchTemplateId, int(value)))
        return {}

    def create_tags(self, Resources, Tags):
        assert Tags == [{"Key": "FleetImageValidated", "Value": "true"}]
        assert all(name in self.images or name in self.snapshots for name in Resources)

    def terminate_instances(self, InstanceIds):
        if self.fail_terminate:
            raise RuntimeError("termination unavailable")
        self.terminated.extend(InstanceIds)
        for name in InstanceIds:
            request = self.spot_requests.get(self.instances[name].get("SpotInstanceRequestId"))
            if request and request["Type"] == "persistent" and request["State"] not in {"cancelled", "closed"}:
                self.reopened_spot.append(request["SpotInstanceRequestId"])
                request["State"] = "open"
            self.instances[name]["State"]["Name"] = "terminated"
        self.runtime_order.append("terminate")
        return {}

    def describe_spot_instance_requests(self, SpotInstanceRequestIds):
        return {"SpotInstanceRequests": [copy.deepcopy(self.spot_requests[name]) for name in SpotInstanceRequestIds]}

    def cancel_spot_instance_requests(self, SpotInstanceRequestIds):
        if self.fail_cancel:
            raise RuntimeError("Spot cancellation unavailable")
        self.runtime_order.append("cancel")
        for name in SpotInstanceRequestIds:
            request = self.spot_requests[name]
            assert request["Type"] == "persistent" and request["Tags"] == [{"Key": "Purpose", "Value": "fcvm-fleet-tile"}]
            self.cancelled_spot.append(name)
            request["State"] = "cancelled"
            if self.instances[request["InstanceId"]]["State"]["Name"] == "stopped":
                self.instances[request["InstanceId"]]["State"]["Name"] = "terminated"
        return {"CancelledSpotInstanceRequests": [{"SpotInstanceRequestId": name, "State": "cancelled"} for name in SpotInstanceRequestIds]}


class CodeBuild:
    def __init__(self, value):
        self.value = value

    def batch_get_builds(self, ids):
        assert ids == [self.value["id"]]
        return {"builds": [copy.deepcopy(self.value)]}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.build = "factory:00000000-0000-0000-0000-000000000001"
        self.source, self.input = "a" * 40, "b" * 64
        self.ids = ["lt-" + format(i, "017x") for i in range(1, 5)]
        self.templates = [{"region": "us-east-1", "id": name, "kind": "host" if i == 0 else "tile", "arch": "x86_64"}
                          for i, name in enumerate(self.ids)]
        self.env = {"TEMPLATES_JSON": json.dumps(self.templates), "CONTROL_REGION": "us-west-1",
            "COMPUTE_REGION": "us-east-1", "ASSET_REGION": "us-east-2", "PIPELINE_REGION": "us-east-2", "CONTROL_ACCOUNT": "111111111111",
            "FLEET_ACCOUNT": "222222222222", "PROJECT_NAME": "factory", "STATE_PREFIX": "/factory",
            "PIPELINE_BUCKET": "private-stage", "ASSET_BUCKET": "runtime-assets", "GUARD_SHA256": "c" * 64,
            "RELEASE_PARAMETER": "/release", "LOCK_ID": "exact-backend/key", "LOCK_TABLE": "locks"}
        start = datetime.datetime(2026, 10, 31, tzinfo=datetime.timezone.utc)
        build = {"id": self.build, "projectName": "factory", "buildStatus": "SUCCEEDED", "buildComplete": True,
            "resolvedSourceVersion": self.source, "startTime": start, "endTime": start + datetime.timedelta(hours=2),
            "environment": {"environmentVariables": [{"name": "SOURCE_SHA", "value": self.source}, {"name": "INPUT_SHA256", "value": self.input}]}}
        self.main_ssm, self.fleet_ssm, self.ddb, self.ec2 = SSM(), SSM(), DDB(), EC2()
        self.s3, self.assets = S3(), S3()
        self.assets.source = self.s3
        self.codebuild = CodeBuild(build)
        self.event = {"source": "aws.codebuild", "detail-type": "CodeBuild Build State Change", "account": self.env["CONTROL_ACCOUNT"],
            "region": "us-west-1", "detail": {"build-status": "SUCCEEDED", "project-name": "factory", "build-id": self.build}}
        old = {"host": "ami-" + "1" * 17, "tile": "ami-" + "2" * 17}
        new = {"host": "ami-" + "3" * 17, "tile": "ami-" + "4" * 17}
        self.old, self.new = old, new
        release = {"version": 1, "generation": 0, "source_sha": "", "build_id": "", "guard_sha256": self.env["GUARD_SHA256"],
            "host_amis": {"us-east-1": {"x86_64": old["host"]}}, "tile_amis": {"us-east-1": {"x86_64": old["tile"]}}, "versions": {}, "previous": None}
        self.fleet_ssm.values["/release"] = release
        journal = {"build_id": self.build, "source_sha": self.source, "input_sha256": self.input, "request_id": self.input,
                   "deadline_unix": int(start.timestamp()) + 14400, "slots": {}, "candidates": new}
        for i, slot in enumerate(sorted(MODULE.SLOTS), 10):
            instance = "i-" + format(i, "017x")
            journal["slots"][slot] = {"instance_id": instance}
            self.ec2.instances[instance] = {"InstanceId": instance, "State": {"Name": "terminated"}}
        self.main_ssm.values["/factory/control"] = {"active": self.build}
        self.main_ssm.values["/factory/builds/" + MODULE.digest(self.build)] = journal
        self.main_ssm.values["/factory/requests/" + self.input] = {"request_id": self.input, "input_sha256": self.input,
            "source_sha": self.source, "build_id": self.build, "priority": "normal", "status": "building"}
        self.manifest = {"version": 1, "build_id": self.build, "source_commit": self.source, "input_sha256": self.input,
            "source_tree_sha256": "f" * 64, "tls": {"build_cert_sha256": "7" * 64, "test_cert_sha256": "8" * 64},
            "guard_sha256": self.env["GUARD_SHA256"], "deadline_unix": journal["deadline_unix"], "images": {}}
        for kind in ("host", "tile"):
            self.manifest["images"][kind] = {"image_id": new[kind], "renderer_sha256": "d" * 64,
                "renderer_image_id": "sha256:" + "e" * 64, "validation": {"guard": True, "frames": True,
                    "sanitized": True, "tls_rotation": True, "phone_clone": kind == "host", **self.manifest["tls"],
                    "boot_ids": [f"00000000-0000-0000-0000-{i:012d}" for i in range(3)]}}
            tags = {"Purpose": "fcvm-fleet", "FleetImageKind": kind, "GuardHelperSHA256": self.env["GUARD_SHA256"],
                "SourceCommit": self.source, "InputSHA256": self.input, "PipelineBuildId": self.build, "PipelineSanitized": "true"}
            snapshot = "snap-" + ("5" if kind == "host" else "6") * 17
            self.ec2.images[new[kind]] = {"ImageId": new[kind], "OwnerId": self.env["FLEET_ACCOUNT"], "Public": False,
                "State": "available", "Architecture": "x86_64", "RootDeviceType": "ebs", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
                "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot}}]}
            self.ec2.snapshots[snapshot] = {"SnapshotId": snapshot, "OwnerId": self.env["FLEET_ACCOUNT"], "State": "completed",
                "Encrypted": True, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
        for item in self.templates:
            self.ec2.templates[item["id"]] = {"default": 1, "versions": {1: {"LaunchTemplateId": item["id"], "VersionNumber": 1,
                "VersionDescription": "terraform", "LaunchTemplateData": {"ImageId": old[item["kind"]], "UserData": "unchanged-private-userdata",
                    "InstanceType": "g4dn.metal" if item["kind"] == "host" else "g4dn.xlarge", "SecurityGroupIds": ["known-group"]}}}}
        self.stage_prefix = "runs/" + MODULE.digest(self.build) + "/store/"
        self.target_prefix = "factory/" + self.input + "/store/"
        data = {name: name.encode() for name in MODULE.ARCHIVES}
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in data.items()}
        data["parts"] = ("\n".join(f"part-{i:02d}" for i in range(16)) + "\n").encode()
        data["sha256.json"] = json.dumps(hashes).encode()
        objects = {}
        for i, (name, value) in enumerate(sorted(data.items())):
            sha, version = hashlib.sha256(value).hexdigest(), "version-" + str(i)
            self.s3.objects[self.stage_prefix + name] = (value, {"sha256": sha}, version)
            objects[name] = {"sha256": sha, "version_id": version, "size": len(value)}
        self.manifest["store"] = {"bucket": "private-stage", "region": "us-east-2", "prefix": self.stage_prefix,
            "destination": {"bucket": "runtime-assets", "region": "us-east-2", "prefix": self.target_prefix}, "objects": objects}
        self.update_manifest()

    def update_manifest(self):
        self.s3.objects["runs/" + MODULE.digest(self.build) + "/manifest.json"] = (json.dumps(self.manifest).encode(), {}, "manifest-version")

    def publisher(self):
        clients = {("ssm", False, "us-west-1"): self.main_ssm, ("ssm", True, "us-east-1"): self.fleet_ssm,
            ("dynamodb", False, "us-west-1"): self.ddb, ("ec2", True, "us-east-1"): self.ec2,
            ("codebuild", False, "us-west-1"): self.codebuild, ("s3", False, "us-east-2"): self.s3,
            ("s3", True, "us-east-2"): self.assets}
        publisher = MODULE.Publisher(self.env, clients, sleep=lambda _: None)
        # Any missing fake is a test error, never an implicit real boto3 client.
        regions = {("s3", False): "us-east-2", ("s3", True): "us-east-2",
                   ("ec2", True): "us-east-1", ("ssm", True): "us-east-1",
                   ("ssm", False): "us-west-1", ("dynamodb", False): "us-west-1", ("codebuild", False): "us-west-1"}
        publisher.client = lambda service, fleet=False, region=None: clients[(service, fleet, region or regions[(service, fleet)])]
        return publisher

    def test_native_client_factory_separates_staging_from_control_and_compute_regions(self):
        sdk = types.SimpleNamespace(client=mock.Mock(side_effect=lambda *_args, **_kwargs: object()))
        modules = {"boto3": sdk, "botocore": types.ModuleType("botocore"),
                   "botocore.config": types.SimpleNamespace(Config=lambda **_kwargs: None)}
        publisher = MODULE.Publisher(dict(self.env, FLEET_ROLE="arn:aws:iam::222222222222:role/publish"))
        sts = mock.Mock()
        sts.assume_role.return_value = {"Credentials": {"AccessKeyId": "dummy", "SecretAccessKey": "dummy", "SessionToken": "dummy"}}
        publisher.clients[("sts", False, "us-west-1")] = sts
        with mock.patch.dict(sys.modules, modules):
            stage = publisher.client("s3")
            self.assertIs(publisher.client("s3"), stage)
            publisher.client("ssm")
            publisher.client("codebuild")
            publisher.client("ec2", True)
            self.assertIsNot(publisher.client("s3", True, "us-east-2"), stage)
        self.assertEqual([(call.args[0], call.kwargs["region_name"]) for call in sdk.client.call_args_list],
                         [("s3", "us-east-2"), ("ssm", "us-west-1"), ("codebuild", "us-west-1"),
                          ("ec2", "us-east-1"), ("s3", "us-east-2")])

    def test_staging_proof_cannot_substitute_the_control_region(self):
        self.manifest["store"]["region"] = "us-west-1"
        self.update_manifest()
        with self.assertRaises(MODULE.Refused):
            self.publisher().run(self.event)
        self.assertEqual(self.ec2.created, [])

    def test_success_changes_only_images_and_copies_exact_immutable_store(self):
        result = self.publisher().run(self.event)
        self.assertEqual(result, {"status": "promoted", "generation": 1})
        self.assertEqual(len(self.assets.copies), 19)
        self.assertIsNone(self.ddb.item)
        release = self.fleet_ssm.values["/release"]
        self.assertEqual(release["previous"]["generation"], 0)
        self.assertEqual(len(release["versions"]), 4)
        for item in self.templates:
            template = self.ec2.templates[item["id"]]
            self.assertEqual(template["versions"][template["default"]]["LaunchTemplateData"], {
                **template["versions"][1]["LaunchTemplateData"], "ImageId": self.new[item["kind"]]})

    def test_success_after_150_minutes_and_at_exactly_240_can_publish(self):
        for seconds in (9001, 14400):
            with self.subTest(seconds=seconds):
                self.setUp()
                self.codebuild.value["endTime"] = self.codebuild.value["startTime"] + datetime.timedelta(seconds=seconds)
                try:
                    result = self.publisher().run(self.event)
                except MODULE.Refused as error:
                    self.fail("the approved 240-minute build was refused: " + str(error))
                self.assertEqual(result, {"status": "promoted", "generation": 1})
                self.assertEqual(len(self.ec2.created), 4)

    def test_overlong_or_mismatched_execution_lease_cannot_change_templates(self):
        for case in ("elapsed-over-240", "old-journal", "extended-journal"):
            with self.subTest(case=case):
                self.setUp()
                if case == "elapsed-over-240":
                    self.codebuild.value["endTime"] = self.codebuild.value["startTime"] + datetime.timedelta(seconds=14401)
                else:
                    journal = self.main_ssm.values["/factory/builds/" + MODULE.digest(self.build)]
                    journal["deadline_unix"] = int(self.codebuild.value["startTime"].timestamp()) + (9000 if case == "old-journal" else 14401)
                    self.manifest["deadline_unix"] = journal["deadline_unix"]
                    self.update_manifest()
                with self.assertRaises(MODULE.Refused):
                    self.publisher().run(self.event)
                self.assertEqual((self.ec2.created, self.assets.copies), ([], []))

    def test_event_and_actual_build_must_both_be_successful(self):
        self.event["detail"]["build-status"] = "FAILED"
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.event["detail"]["build-status"] = "SUCCEEDED"
        self.codebuild.value["buildStatus"] = "FAILED"
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertFalse(self.ec2.created)

    def test_foreign_event_account_is_refused(self):
        self.event["account"] = "333333333333"
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertIsNone(self.ddb.item)

    def test_live_terraform_lock_is_never_stolen(self):
        self.ddb.item = {"LockID": {"S": "exact-backend/key"}, "Info": {"S": "terraform-owner"}}
        with self.assertRaises(RuntimeError): self.publisher().run(self.event)
        self.assertEqual(self.ddb.item["Info"]["S"], "terraform-owner")
        self.assertFalse(self.ec2.created)

    def test_boot_proof_or_pair_mismatch_refuses_before_versions(self):
        baseline = copy.deepcopy(self.manifest)
        for mutation in (lambda: self.manifest["images"]["host"]["validation"].update(boot_ids=["same"] * 3),
                         lambda: self.manifest["images"]["tile"].update(renderer_image_id="sha256:" + "f" * 64)):
            self.manifest = copy.deepcopy(baseline)
            mutation(); self.update_manifest()
            with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
            self.assertFalse(self.ec2.created)

    def test_foreign_public_unavailable_or_unsanitized_image_refuses(self):
        image = self.ec2.images[self.new["host"]]
        for key, value in (("OwnerId", "333333333333"), ("Public", True), ("State", "pending")):
            old = image[key]; image[key] = value
            with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
            image[key] = old
        image["Tags"] = [item for item in image["Tags"] if item["Key"] != "PipelineSanitized"]
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertFalse(self.ec2.created)

    def test_tls_canaries_must_be_distinct_and_bound_to_each_validator(self):
        baseline = copy.deepcopy(self.manifest)
        for change in (lambda: self.manifest["tls"].update(test_cert_sha256=self.manifest["tls"]["build_cert_sha256"]),
                       lambda: self.manifest["images"]["host"]["validation"].update(test_cert_sha256="9" * 64)):
            self.manifest = copy.deepcopy(baseline)
            change(); self.update_manifest()
            with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
            self.assertFalse(self.ec2.created)

    def test_superseded_build_or_running_builder_cannot_publish(self):
        self.main_ssm.values["/factory/control"]["active"] = "a-different-build"
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.main_ssm.values["/factory/control"]["active"] = self.build
        next(iter(self.ec2.instances.values()))["State"]["Name"] = "running"
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertFalse(self.ec2.created)

    def test_snapshot_owner_and_encryption_are_required(self):
        snapshot = next(iter(self.ec2.snapshots.values()))
        for key, value in (("OwnerId", "333333333333"), ("Encrypted", False)):
            previous = snapshot[key]; snapshot[key] = value
            with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
            snapshot[key] = previous
        self.assertFalse(self.ec2.created)

    def test_store_extra_object_or_manifest_digest_change_refuses(self):
        self.assets.objects[self.target_prefix + "unrelated"] = (b"foreign", {}, "foreign")
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assets.objects.clear()
        data, metadata, version = self.s3.objects[self.stage_prefix + "parts"]
        self.s3.objects[self.stage_prefix + "parts"] = (b"wrong", metadata, version)
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertFalse(self.ec2.created)

    def test_mutable_source_version_or_foreign_destination_is_refused(self):
        obj = self.manifest["store"]["objects"]["parts"]
        obj["version_id"] = "null"; self.update_manifest()
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        obj["version_id"] = self.s3.objects[self.stage_prefix + "parts"][2]
        self.update_manifest()
        self.assets.objects[self.target_prefix + "dirs.tar.zst"] = (b"foreign", {"sha256": "f" * 64}, "foreign")
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertEqual(self.assets.objects[self.target_prefix + "dirs.tar.zst"][0], b"foreign")
        self.assertFalse(self.ec2.created)

    def test_configuration_change_is_rejected_and_only_owned_candidate_deleted(self):
        self.ec2.corrupt_candidate = True
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertEqual(self.ec2.deleted, [(self.ids[0], 2)])
        self.assertEqual(self.ec2.templates[self.ids[0]]["default"], 1)
        self.assertIsNone(self.ddb.item)

    def test_second_default_failure_restores_old_images_using_current_configuration(self):
        self.ec2.fail_select = self.ids[1]
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertEqual(self.fleet_ssm.values["/release"]["generation"], 0)
        first = self.ec2.templates[self.ids[0]]
        self.assertEqual(first["default"], 3)
        self.assertEqual(first["versions"][3]["LaunchTemplateData"], first["versions"][1]["LaunchTemplateData"])
        self.assertIn((self.ids[0], 2, 3), self.ec2.created)
        self.assertEqual(len(self.ec2.deleted), 4)
        self.assertIsNone(self.ddb.item)

    def test_rolled_back_event_is_terminal_without_lock_or_creation_replay(self):
        self.ec2.fail_select = self.ids[1]
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        created, selected, puts = list(self.ec2.created), list(self.ec2.selected), self.ddb.puts
        result = self.publisher().run(self.event)
        self.assertEqual(result, {"status": "failed", "reason": "rolled_back"})
        self.assertEqual(self.main_ssm.values["/factory/requests/" + self.input]["status"], "failed")
        self.assertEqual(self.fleet_ssm.values["/release"]["generation"], 0)
        self.assertEqual(self.ec2.created, created)
        self.assertEqual(self.ec2.selected, selected)
        self.assertEqual(self.ddb.puts, puts)
        self.assertIsNone(self.ddb.item)

    def test_lost_create_response_is_discovered_without_replay(self):
        self.ec2.lose_create_response = True
        self.publisher().run(self.event)
        self.assertEqual(len(self.ec2.created), 4)

    def test_unresolved_create_retains_lock_and_does_not_replay(self):
        self.ec2.unknown_create_response = True
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertIsNotNone(self.ddb.item)
        self.assertFalse(self.ec2.created)

    def old_fleet_instance(self, number, *, template=None, image=None, purpose="fcvm-fleet-host"):
        name = "i-" + format(number, "017x")
        self.ec2.instances[name] = {"InstanceId": name, "ImageId": image or self.old["host"], "InstanceType": "g4dn.metal",
            "Tags": [{"Key": "Purpose", "Value": purpose}, {"Key": "aws:ec2launchtemplate:id", "Value": template or self.ids[0]},
                     {"Key": "aws:ec2launchtemplate:version", "Value": "9"}], "State": {"Name": "running"}}
        return name

    def test_normal_release_never_terminates_running_instances(self):
        self.old_fleet_instance(100)
        self.publisher().run(self.event)
        self.assertFalse(self.ec2.terminated)

    def test_critical_retires_only_old_images_from_exact_fleet_templates(self):
        self.main_ssm.values["/factory/requests/" + self.input]["priority"] = "critical"
        allowed = self.old_fleet_instance(100)
        two_generations_behind = self.old_fleet_instance(104, image="ami-" + "9" * 17)
        tile = self.old_fleet_instance(105, template=self.ids[1], image=self.old["tile"], purpose="fcvm-fleet-tile")
        self.ec2.instances[tile]["InstanceType"] = "g4dn.xlarge"
        self.old_fleet_instance(101, template="lt-" + "9" * 17)
        self.old_fleet_instance(102, image=self.new["host"])
        self.old_fleet_instance(103, purpose="nextjs-dev")
        self.publisher().run(self.event)
        self.assertEqual(self.ec2.terminated, [allowed, two_generations_behind, tile])

    def test_critical_requires_real_immutable_template_tags_not_a_synthetic_field(self):
        self.main_ssm.values["/factory/requests/" + self.input]["priority"] = "critical"
        synthetic = self.old_fleet_instance(100)
        self.ec2.instances[synthetic]["Tags"] = [{"Key": "Purpose", "Value": "fcvm-fleet-host"}]
        self.ec2.instances[synthetic]["LaunchTemplate"] = {"LaunchTemplateId": self.ids[0]}
        malformed = self.old_fleet_instance(101)
        self.ec2.instances[malformed]["Tags"][-1]["Value"] = "$Latest"
        self.publisher().run(self.event)
        self.assertFalse(self.ec2.terminated)

    def persistent_tile(self, *, stopped=False):
        instance = self.old_fleet_instance(120, template=self.ids[3], image=self.old["tile"], purpose="fcvm-fleet-tile")
        self.ec2.instances[instance].update(InstanceType="g4dn.xlarge", SpotInstanceRequestId="sir-12345678")
        if stopped:
            self.ec2.instances[instance]["State"]["Name"] = "stopped"
        self.ec2.spot_requests["sir-12345678"] = {"SpotInstanceRequestId": "sir-12345678", "InstanceId": instance,
            "Type": "persistent", "State": "disabled" if stopped else "active", "Tags": [{"Key": "Purpose", "Value": "fcvm-fleet-tile"}],
            "LaunchSpecification": {"ImageId": self.old["tile"], "InstanceType": "g4dn.xlarge"}}
        self.main_ssm.values["/factory/requests/" + self.input]["priority"] = "critical"
        return instance

    def test_critical_cancels_persistent_request_before_termination_without_reopening(self):
        instance = self.persistent_tile()
        self.publisher().run(self.event)
        self.assertEqual(self.ec2.cancelled_spot, ["sir-12345678"])
        self.assertEqual(self.ec2.runtime_order, ["cancel", "terminate"])
        self.assertFalse(self.ec2.reopened_spot)
        self.assertEqual(self.ec2.instances[instance]["State"]["Name"], "terminated")

    def test_cancelled_stopped_spot_auto_termination_is_success_without_an_extra_terminate(self):
        instance = self.persistent_tile(stopped=True)
        self.publisher().run(self.event)
        self.assertEqual(self.ec2.cancelled_spot, ["sir-12345678"])
        self.assertFalse(self.ec2.terminated)
        self.assertFalse(self.ec2.reopened_spot)
        self.assertEqual(self.ec2.instances[instance]["State"]["Name"], "terminated")

    def test_spot_cancel_failure_preserves_new_release_and_retry_never_recreates_versions(self):
        self.persistent_tile()
        self.ec2.fail_cancel = True
        with self.assertRaises(RuntimeError): self.publisher().run(self.event)
        self.assertEqual(self.fleet_ssm.values["/release"]["generation"], 1)
        self.assertEqual(self.main_ssm.values["/factory/requests/" + self.input]["status"], "awaiting-publish")
        self.assertFalse(self.ec2.terminated)
        self.ec2.fail_cancel = False
        self.publisher().run(self.event)
        self.assertEqual(len(self.ec2.created), 4)
        self.assertEqual(self.ec2.cancelled_spot, ["sir-12345678"])

    def test_persistent_request_must_be_bound_to_the_exact_owned_instance(self):
        self.persistent_tile()
        self.ec2.spot_requests["sir-12345678"]["InstanceId"] = "i-" + "9" * 17
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertFalse(self.ec2.cancelled_spot)
        self.assertFalse(self.ec2.terminated)

    def test_source_metadata_cannot_lie_about_actual_archive_checksum(self):
        key = self.stage_prefix + "dirs.tar.zst"
        data, metadata, version = self.s3.objects[key]
        self.s3.objects[key] = (b"x" * len(data), metadata, version)
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertFalse(self.ec2.created)

    def test_copy_must_return_the_verified_content_checksum(self):
        self.assets.bad_copy_checksum = True
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertFalse(self.ec2.created)

    def test_preemption_during_asset_copy_is_rechecked_under_backend_lock(self):
        publisher = self.publisher()
        original = publisher.store
        def preempt(manifest, input_sha):
            original(manifest, input_sha)
            self.main_ssm.values["/factory/control"]["active"] = "critical-replacement"
        publisher.store = preempt
        with self.assertRaises(MODULE.Refused): publisher.run(self.event)
        self.assertFalse(self.ec2.created)
        self.assertIsNone(self.ddb.item)

    def test_critical_failure_keeps_new_pair_and_retries_cleanup_without_new_versions(self):
        self.main_ssm.values["/factory/requests/" + self.input]["priority"] = "critical"
        allowed = self.old_fleet_instance(100)
        self.ec2.fail_terminate = True
        with self.assertRaises(RuntimeError): self.publisher().run(self.event)
        self.assertEqual(self.fleet_ssm.values["/release"]["generation"], 1)
        self.assertEqual(self.main_ssm.values["/factory/requests/" + self.input]["status"], "awaiting-publish")
        self.ec2.fail_terminate = False
        self.main_ssm.values["/factory/control"]["active"] = "newer-build"
        self.publisher().run(self.event)
        self.assertEqual(self.ec2.terminated, [allowed])
        self.assertEqual(len(self.ec2.created), 4)
        self.assertIsNone(self.ddb.item)


if __name__ == "__main__":
    unittest.main(buffer=True)
