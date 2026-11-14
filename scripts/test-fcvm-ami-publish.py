#!/usr/bin/env python3
"""Credential-free publisher transactions against strict in-memory AWS fakes."""
import base64
import copy
import inspect
import re
import datetime
import hashlib
import http.client
import importlib.util
import io
import json
import os
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
    """One region of the fleet account; `peers` maps every region to its fake, for CopyImage."""
    def __init__(self, region="us-east-1", peers=None):
        self.region, self.peers = region, peers if peers is not None else {}
        self.peers[region] = self
        self.copies, self.fail_copy = [], False
        self.deregistered, self.deleted_snapshots, self.fail_snapshot_delete = [], [], False
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
        self.tagged = []
        self.fail_cancel = False

    def describe_instances(self, InstanceIds=None, Filters=None, **_kwargs):
        values = [self.instances[item] for item in InstanceIds] if InstanceIds else list(self.instances.values())
        for item in Filters or []:
            if item["Name"] == "instance-id":
                values = [instance for instance in values if instance["InstanceId"] in item["Values"]]
            elif item["Name"].startswith("tag:"):
                key = item["Name"][4:]
                values = [instance for instance in values if {t["Key"]: t["Value"] for t in instance.get("Tags", [])}.get(key) in item["Values"]]
            elif item["Name"] == "instance-state-name":
                values = [instance for instance in values if instance["State"]["Name"] in item["Values"]]
            else:
                raise AssertionError("unexpected instance filter")
        return {"Reservations": [{"OwnerId": "222222222222", "Instances": copy.deepcopy(values)}]}

    def describe_images(self, ImageIds=None, Owners=None, Filters=None):
        if ImageIds is not None:
            assert Owners is None and Filters is None
            return {"Images": [copy.deepcopy(self.images[name]) for name in ImageIds]}
        assert Owners == ["222222222222"] and len(Filters) == 1
        name, values = Filters[0]["Name"], Filters[0]["Values"]
        if name == "name":
            rows = [item for item in self.images.values() if item.get("Name") in values]
        else:
            assert name.startswith("tag:")
            rows = [item for item in self.images.values()
                    if {t["Key"]: t["Value"] for t in item.get("Tags", [])}.get(name[4:]) in values]
        return {"Images": copy.deepcopy(rows)}

    def deregister_image(self, ImageId):
        self.deregistered.append(ImageId)
        del self.images[ImageId]

    def delete_snapshot(self, SnapshotId):
        if self.fail_snapshot_delete:
            self.fail_snapshot_delete = False
            raise RuntimeError("snapshot deletion unavailable")
        if any(item.get("Ebs", {}).get("SnapshotId") == SnapshotId for image in self.images.values()
               for item in image.get("BlockDeviceMappings", [])):
            error = RuntimeError("in use")
            error.response = {"Error": {"Code": "InvalidSnapshot.InUse"}}
            raise error
        self.deleted_snapshots.append(SnapshotId)
        self.snapshots.pop(SnapshotId, None)

    def copy_image(self, SourceImageId, SourceRegion, Name, Description, Encrypted, CopyImageTags, TagSpecifications, ClientToken):
        """A pending image carrying the source's tags; `available()` completes it."""
        assert SourceRegion != self.region and Encrypted is True and CopyImageTags is True
        assert TagSpecifications == [{"ResourceType": "snapshot", "Tags": [{"Key": "FleetPublisherCopy", "Value": "true"}]}]
        suffix = Name[-1] if Name.endswith(("-copy-2", "-copy-3")) else ""
        assert ClientToken == MODULE.digest(SourceImageId + "/" + self.region + ("/" + suffix if suffix else ""))
        if self.fail_copy:
            raise RuntimeError("copy unavailable")
        assert all(item.get("Name") != Name for item in self.images.values()), "a name is never copied twice"
        source = self.peers[SourceRegion].images[SourceImageId]
        image_id = "ami-" + format(0xc0000 + 16 * len(self.peers) + len(self.images) + 4096 * (self.region == "us-east-1"), "017x")
        snapshot = "snap-" + image_id[4:]
        self.snapshots[snapshot] = {"SnapshotId": snapshot, "OwnerId": source["OwnerId"], "Tags": copy.deepcopy(TagSpecifications[0]["Tags"])}
        self.images[image_id] = {"ImageId": image_id, "Name": Name, "Description": Description, "OwnerId": source["OwnerId"],
            "Public": False, "State": getattr(self, "copy_state", "pending" if self.region == "us-east-1" else "available"),
            "Architecture": "x86_64", "RootDeviceType": "ebs",
            "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot}}],
            "SourceImageId": SourceImageId, "SourceImageRegion": SourceRegion, "Tags": copy.deepcopy(source["Tags"])}
        self.copies.append((SourceImageId, image_id))
        return {"ImageId": image_id}

    def available(self):
        for item in self.images.values():
            if item["State"] == "pending":
                item["State"] = "available"

    def describe_snapshots(self, SnapshotIds=None, OwnerIds=None, Filters=None, MaxResults=None):
        if SnapshotIds is not None:
            return {"Snapshots": [copy.deepcopy(self.snapshots[name]) for name in SnapshotIds]}
        assert OwnerIds == ["222222222222"] and MaxResults == MODULE.RETAIN_LIMIT
        values = [item for item in self.snapshots.values() if item.get("OwnerId") in OwnerIds]
        for scope in Filters:
            name = scope["Name"]
            if name == "snapshot-id":
                values = [item for item in values if item["SnapshotId"] in scope["Values"]]
            else:
                assert name in {"tag:FleetRetired", "tag:FleetPublisherCopy"}
                values = [item for item in values if {t["Key"]: t["Value"] for t in item.get("Tags", [])}.get(name[4:]) in scope["Values"]]
        return {"Snapshots": copy.deepcopy(values[:MaxResults])}

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
        if Tags == [{"Key": "FleetRetired", "Value": "true"}]:
            assert all(name.startswith("snap-") for name in Resources)
            for name in Resources:
                item = self.snapshots[name]
                assert {tag["Key"]: tag["Value"] for tag in item["Tags"]}.get("FleetPublisherCopy") == "true"
                item["Tags"] = [t for t in item.get("Tags", []) if t["Key"] != "FleetRetired"] + copy.deepcopy(Tags)
            return
        assert Tags == [{"Key": "FleetImageValidated", "Value": "true"}]
        assert all(name in self.images or name in self.snapshots for name in Resources)
        self.tagged.extend(Resources)
        for name in Resources:
            item = self.images.get(name) or self.snapshots[name]
            item["Tags"] = [tag for tag in item.get("Tags", []) if tag["Key"] != "FleetImageValidated"] + copy.deepcopy(Tags)

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

    def describe_spot_instance_requests(self, SpotInstanceRequestIds=None, Filters=None):
        if SpotInstanceRequestIds is None:
            assert [item["Name"] for item in Filters] == ["state"]
            return {"SpotInstanceRequests": [copy.deepcopy(item) for item in self.spot_requests.values()
                                             if item["State"] in Filters[0]["Values"]]}
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
        network = mock.patch.object(http.client, "HTTPSConnection", side_effect=AssertionError("offline fixture forbids network"))
        network.start()
        self.addCleanup(network.stop)
        self.build = "factory:00000000-0000-0000-0000-000000000001"
        self.source, self.input = "a" * 40, "b" * 64
        self.ids = ["lt-" + format(i, "017x") for i in range(1, 5)]
        self.templates = [{"region": "us-east-1", "id": name, "kind": "host" if i == 0 else "tile", "arch": "x86_64"}
                          for i, name in enumerate(self.ids)]
        self.env = {"TEMPLATES_JSON": json.dumps(self.templates), "CONTROL_REGION": "us-west-1",
            "COMPUTE_REGION": "us-east-1", "ASSET_REGION": "us-east-2", "PIPELINE_REGION": "us-east-2", "CONTROL_ACCOUNT": "111111111111",
            "FLEET_ACCOUNT": "222222222222", "PROJECT_NAME": "factory", "STATE_PREFIX": "/factory",
            "PIPELINE_BUCKET": "private-stage", "ASSET_BUCKET": "runtime-assets", "GUARD_SHA256": "c" * 64,
            "BUILDER_PROFILE": "arn:aws:iam::222222222222:instance-profile/factory-builder",
            "RELEASE_PARAMETER": "/release", "LOCK_ID": "exact-backend/key", "LOCK_TABLE": "locks",
            "COPY_REGIONS": "us-west-2", "BUILD_REGION": "us-east-1"}
        start = datetime.datetime(2026, 10, 31, tzinfo=datetime.timezone.utc)
        build = {"id": self.build, "projectName": "factory", "buildStatus": "SUCCEEDED", "buildComplete": True,
            "resolvedSourceVersion": self.source, "startTime": start, "endTime": start + datetime.timedelta(hours=2),
            "timeoutInMinutes": 360,
            "environment": {"environmentVariables": [{"name": "SOURCE_SHA", "value": self.source}, {"name": "INPUT_SHA256", "value": self.input}]}}
        self.main_ssm, self.fleet_ssm, self.ddb, self.ec2 = SSM(), SSM(), DDB(), EC2()
        self.west = EC2("us-west-2", self.ec2.peers)
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
        self.env["SEED_RELEASE_JSON"] = json.dumps({"source_sha": self.source, "images": new, "expected": copy.deepcopy(release)})
        journal = {"build_id": self.build, "source_sha": self.source, "input_sha256": self.input, "request_id": self.input,
                   "deadline_unix": int(start.timestamp()) + 21600, "slots": {}, "candidates": new}
        for i, slot in enumerate(sorted(MODULE.SLOTS), 10):
            instance = "i-" + format(i, "017x")
            journal["slots"][slot] = {"instance_id": instance, "attempted": True, "terminated_seen": True}
            self.ec2.instances[instance] = {"InstanceId": instance, "State": {"Name": "terminated"},
                "InstanceType": "g4dn.metal" if slot.startswith("host-") else "g4dn.xlarge",
                "IamInstanceProfile": {"Arn": self.env["BUILDER_PROFILE"]},
                "Tags": [{"Key": "Purpose", "Value": "fcvm-fleet-ami-builder"},
                    {"Key": "FactoryBuild", "Value": MODULE.digest(self.build)}, {"Key": "FactorySlot", "Value": slot}]}
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
                    "sanitized": True, "tls_rotation": True, "phone_clone": kind == "host",
                    "viewer_path": kind == "host", **self.manifest["tls"],
                    "boot_ids": [f"00000000-0000-0000-0000-{i:012d}" for i in range(3)]}}
            tags = {"Purpose": "fcvm-fleet", "FleetImageKind": kind, "GuardHelperSHA256": self.env["GUARD_SHA256"],
                "SourceCommit": self.source, "InputSHA256": self.input, "PipelineBuildId": self.build, "PipelineSanitized": "true"}
            snapshot = "snap-" + ("5" if kind == "host" else "6") * 17
            self.ec2.images[new[kind]] = {"ImageId": new[kind], "Name": f"fcvm-fleet-{kind}-x86_64-{self.source[:8]}-202610310000",
                "OwnerId": self.env["FLEET_ACCOUNT"], "Public": False,
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
            ("dynamodb", False, "us-west-1"): self.ddb, ("ec2", True, "us-east-1"): self.ec2, ("ec2", True, "us-west-2"): self.west,
            ("codebuild", False, "us-west-1"): self.codebuild, ("s3", False, "us-east-2"): self.s3,
            ("s3", True, "us-east-2"): self.assets}
        publisher = MODULE.Publisher(self.env, clients, sleep=lambda _: None)
        # Any missing fake is a test error, never an implicit real boto3 client.
        regions = {("s3", False): "us-east-2", ("s3", True): "us-east-2",
                   ("ec2", True): "us-east-1", ("ssm", True): "us-east-1",
                   ("ssm", False): "us-west-1", ("dynamodb", False): "us-west-1", ("codebuild", False): "us-west-1"}
        publisher.client = lambda service, fleet=False, region=None: clients[(service, fleet, region or regions[(service, fleet)])]
        return publisher

    def legacy_seed(self):
        for image in self.ec2.images.values():
            image["Tags"] = [tag for tag in image["Tags"]
                             if tag["Key"] in {"Purpose", "FleetImageKind", "GuardHelperSHA256"}]
            image["Tags"].append({"Key": "FleetImageValidated", "Value": "true"})
        return {"action": "activate-reviewed-seed"}

    def seed_success(self, publisher, event):
        try:
            return publisher.run(event)
        except MODULE.Refused as error:
            self.fail("reviewed seed transition was refused: " + str(error))

    def test_reviewed_seed_uses_only_the_existing_image_transaction(self):
        event = self.legacy_seed()
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        requests = copy.deepcopy(self.main_ssm.values)
        with mock.patch.object(self.codebuild, "batch_get_builds", side_effect=AssertionError("no paid build path")):
            self.assertEqual(self.seed_success(self.publisher(), event), {"status": "promoted", "generation": 1})
        release = self.fleet_ssm.values["/release"]
        self.assertEqual(release["previous"], {k: v for k, v in before.items() if k != "previous"})
        self.assertTrue(release["build_id"].startswith("reviewed-seed:"))
        self.assertEqual(release["source_sha"], self.source)
        self.assertEqual(len(self.ec2.created), 4)
        for item in self.templates:
            template = self.ec2.templates[item["id"]]
            self.assertEqual(template["versions"][template["default"]]["LaunchTemplateData"],
                             {**template["versions"][1]["LaunchTemplateData"], "ImageId": self.new[item["kind"]]})
        self.assertEqual((self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], []))
        self.assertTrue(all(self.main_ssm.values[key] == value for key, value in requests.items()))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_seed_refuses_caller_overrides_nonseed_and_unvalidated_images(self):
        variants = ("caller-image", "caller-source", "published", "changed-seed", "wrong-config",
                    "OwnerId", "Public", "State", "FleetImageKind", "FleetImageValidated", "GuardHelperSHA256")
        for variant in variants:
            with self.subTest(variant=variant):
                self.setUp()
                event = self.legacy_seed()
                if variant == "caller-image": event["images"] = self.old
                elif variant == "caller-source": event["source_sha"] = "f" * 40
                elif variant == "published": self.fleet_ssm.values["/release"]["generation"] = 1
                elif variant == "changed-seed": self.fleet_ssm.values["/release"]["host_amis"]["us-east-1"]["x86_64"] = self.new["host"]
                elif variant == "wrong-config": self.env["SEED_RELEASE_JSON"] = '{}'
                else:
                    image = self.ec2.images[self.new["host"]]
                    if variant == "Public": image[variant] = True
                    elif variant in {"OwnerId", "State"}: image[variant] = "foreign"
                    else: next(tag for tag in image["Tags"] if tag["Key"] == variant)["Value"] = "foreign"
                with self.assertRaises(MODULE.Refused): self.publisher().run(event)
                self.assertEqual((self.ec2.created, self.ec2.tagged, self.assets.copies), ([], [], []))
                self.assertIsNone(self.ddb.item)

    def test_reviewed_seed_partial_selection_rolls_back_and_never_replays(self):
        event = self.legacy_seed()
        self.ec2.fail_select = self.ids[2]
        publisher = self.publisher()
        with self.assertRaises(MODULE.Refused): publisher.run(event)
        self.assertGreater(len(self.ec2.created), 4, "actual seed transaction must reach rollback")
        self.assertEqual(self.fleet_ssm.values["/release"]["generation"], 0)
        for item in self.templates:
            template = self.ec2.templates[item["id"]]
            self.assertEqual(template["versions"][template["default"]]["LaunchTemplateData"],
                             template["versions"][1]["LaunchTemplateData"])
        created = list(self.ec2.created)
        self.assertEqual(self.publisher().run(event), {"status": "failed", "reason": "rolled_back"})
        self.assertEqual(self.ec2.created, created)
        self.assertEqual(len(self.ec2.deleted), 4)
        self.assertIsNone(self.ddb.item)

    def test_reviewed_seed_lost_commit_ack_replays_and_interrupted_transaction_recovers(self):
        event = self.legacy_seed()
        original = self.fleet_ssm.put_parameter
        def lost_ack(**arguments):
            original(**arguments)
            raise RuntimeError("response lost after durable commit")
        with mock.patch.object(self.fleet_ssm, "put_parameter", side_effect=lost_ack):
            self.assertEqual(self.seed_success(self.publisher(), event), {"status": "promoted", "generation": 1})
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 1})
        self.assertEqual(len(self.ec2.created), 4)
        self.setUp()
        event = self.legacy_seed()
        original_select = self.ec2.modify_launch_template
        class Interrupted(BaseException):
            pass
        def interrupt(**arguments):
            original_select(**arguments)
            raise Interrupted()
        with mock.patch.object(self.ec2, "modify_launch_template", side_effect=interrupt):
            try:
                self.publisher().run(event)
            except MODULE.Refused as error:
                self.fail("seed transaction never reached the interruption: " + str(error))
            except Interrupted:
                pass
        with self.assertRaises(MODULE.Refused): self.publisher().run(event)
        self.assertEqual(self.publisher().run(event), {"status": "failed", "reason": "rolled_back"})
        self.assertEqual(len(self.ec2.deleted), 4)
        self.assertEqual(self.fleet_ssm.values["/release"]["generation"], 0)
        self.assertIsNone(self.ddb.item)

    def test_factory_source_fence_refuses_older_or_unknown_sources_and_keeps_full_proof(self):
        self.assertTrue(hasattr(self.publisher(), "compare_sources"), "publication needs a native source ancestry fence")
        with mock.patch.object(http.client, "HTTPSConnection") as transport:
            response = transport.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"status":"ahead"}'
            self.assertEqual(self.publisher().compare_sources("9" * 40, self.source), {"status": "ahead"})
            transport.assert_called_once_with("api.github.com", timeout=10)
            request = transport.return_value.request.call_args
            self.assertEqual(request.args, ("GET", "/repos/ejc3/fcvm/compare/" + "9" * 40 + "..." + self.source + "?per_page=1&page=2"))
            response.read.assert_called_once_with(2 * 1024 * 1024 + 1)
            transport.return_value.close.assert_called_once_with()
        for variant in ("older", "network", "descendant", "missing-proof", "changed-under-lock"):
            with self.subTest(variant=variant):
                self.setUp()
                config = json.loads(self.env["SEED_RELEASE_JSON"])
                config["source_sha"] = "9" * 40
                self.env["SEED_RELEASE_JSON"] = json.dumps(config)
                publisher = self.publisher()
                comparison = {"status": "ahead", "behind_by": 0, "base_commit": {"sha": "9" * 40},
                              "merge_base_commit": {"sha": "9" * 40}}
                if variant == "network": publisher.compare_sources = mock.Mock(side_effect=OSError("network unavailable"))
                elif variant == "older": publisher.compare_sources = mock.Mock(return_value={**comparison, "status": "behind"})
                elif variant == "changed-under-lock": publisher.compare_sources = mock.Mock(side_effect=[comparison, {**comparison, "status": "behind"}])
                else: publisher.compare_sources = mock.Mock(return_value=comparison)
                if variant == "missing-proof":
                    self.ec2.images[self.new["host"]]["Tags"] = [tag for tag in self.ec2.images[self.new["host"]]["Tags"]
                                                                if tag["Key"] != "PipelineSanitized"]
                if variant == "descendant":
                    self.assertEqual(publisher.run(self.event), {"status": "promoted", "generation": 1})
                    self.assertEqual(len(self.assets.copies), 19)
                    self.assertGreaterEqual(publisher.compare_sources.call_count, 3)
                else:
                    with self.assertRaises((MODULE.Refused, OSError)): publisher.run(self.event)
                    self.assertEqual(self.ec2.created, [])
                if variant in {"older", "network"}: self.assertEqual(self.assets.copies, [])

    def reviewed_phone_fixture(self):
        self.assertEqual(self.publisher().run(self.legacy_seed()), {"status": "promoted", "generation": 1})
        self.ec2.created.clear()
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        images = {kind: "ami-" + digit * 17 for kind, digit in (("host", "7"), ("tile", "8"))}
        for kind, image_id in images.items():
            image = copy.deepcopy(self.ec2.images[self.new[kind]])
            # An AMI name is unique in its region.
            image.update(ImageId=image_id, Name=f"fcvm-fleet-{kind}-x86_64-{image_id[4:12]}-202611010000")
            self.ec2.images[image_id] = image
        source = "d" * 40
        self.env["PHONE_AMI_JSON"] = json.dumps({"source_sha": source, "images": images, "expected": before})
        ancestry = {"status": "ahead", "behind_by": 0, "base_commit": {"sha": self.source},
                    "merge_base_commit": {"sha": self.source}}
        publisher = self.publisher()
        publisher.compare_sources = mock.Mock(return_value=ancestry)
        return publisher, {"action": "activate-reviewed-phone-release"}, before, images, source, ancestry

    def test_reviewed_phone_pair_uses_exact_predecessor_and_replays_without_writes(self):
        publisher, event, before, images, source, _ = self.reviewed_phone_fixture()
        with mock.patch.object(self.codebuild, "batch_get_builds", side_effect=AssertionError("not a factory build")):
            try:
                result = publisher.run(event)
            except MODULE.Refused as error:
                self.fail("reviewed phone transition was refused: " + str(error))
        self.assertEqual(result, {"status": "promoted", "generation": 2})
        self.assertEqual(publisher.compare_sources.call_args_list, [mock.call(self.source, source)] * 2)
        after = self.fleet_ssm.values["/release"]
        self.assertEqual(after["previous"], {k: v for k, v in before.items() if k != "previous"})
        self.assertEqual(after["source_sha"], source)
        self.assertTrue(after["build_id"].startswith("reviewed-phone-release:"))
        for template in self.templates:
            name, kind = template["id"], template["kind"]
            table = self.ec2.templates[name]
            version = after["versions"]["us-east-1/" + name]
            self.assertEqual(table["default"], version)
            self.assertEqual(table["versions"][version]["LaunchTemplateData"],
                             {**table["versions"][1]["LaunchTemplateData"], "ImageId": images[kind]})
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 2})
        self.assertEqual(len(self.ec2.created), 4)
        self.assertEqual((self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], []))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_phone_rejects_overrides_changed_predecessor_and_unreviewed_targets(self):
        for variant in ("override", "predecessor", "untagged", "diverged"):
            with self.subTest(variant=variant):
                self.setUp()
                publisher, event, _, images, _, ancestry = self.reviewed_phone_fixture()
                if variant == "override":
                    event["images"] = images
                elif variant == "predecessor":
                    self.fleet_ssm.values["/release"]["versions"]["us-east-1/" + self.ids[0]] += 1
                elif variant == "untagged":
                    tags = self.ec2.images[images["tile"]]["Tags"]
                    tags[:] = [tag for tag in tags if tag["Key"] != "FleetImageValidated"]
                else:
                    publisher.compare_sources.return_value = {**ancestry, "status": "diverged"}
                unchanged = copy.deepcopy(self.fleet_ssm.values["/release"])
                with self.assertRaises(MODULE.Refused):
                    publisher.run(event)
                self.assertEqual(self.fleet_ssm.values["/release"], unchanged)
                self.assertEqual(self.ec2.created, [])
                self.assertIsNone(self.ddb.item)

    def test_reviewed_phone_rolls_back_when_ancestry_fails_at_commit(self):
        publisher, event, before, _, _, ancestry = self.reviewed_phone_fixture()
        publisher.compare_sources.side_effect = [ancestry, {**ancestry, "status": "diverged"}]
        with self.assertRaises(MODULE.Refused):
            publisher.run(event)
        self.assertEqual(len(self.ec2.deleted), 4)
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        for template in self.templates:
            table = self.ec2.templates[template["id"]]
            self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"]["ImageId"],
                             self.new[template["kind"]])
        self.assertIsNone(self.ddb.item)

    def reviewed_renderer_fixture(self):
        phone, event, _, _, phone_source, _ = self.reviewed_phone_fixture()
        self.assertEqual(phone.run(event), {"status": "promoted", "generation": 2})
        self.ec2.created.clear()
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        images = {kind: "ami-" + digit * 17 for kind, digit in (("host", "9"), ("tile", "a"))}
        for kind, image_id in images.items():
            image = copy.deepcopy(self.ec2.images[self.new[kind]])
            # An AMI name is unique in its region.
            image.update(ImageId=image_id, Name=f"fcvm-fleet-{kind}-x86_64-{image_id[4:12]}-202611010000")
            self.ec2.images[image_id] = image
        source = "e" * 40
        self.env["RENDERER_AMI_JSON"] = json.dumps({"source_sha": source, "images": images,
                                                  "versions": before["versions"]})
        publisher = self.publisher()
        publisher.compare_sources = mock.Mock(side_effect=lambda base, head: {
            "status": "ahead", "behind_by": 0, "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
        })
        return publisher, {"action": "activate-reviewed-renderer-release"}, before, images, source, phone_source

    def test_reviewed_renderer_only_updates_default_images_and_preserves_phone_rollback(self):
        publisher, event, before, images, source, phone_source = self.reviewed_renderer_fixture()
        self.assertEqual(publisher.reviewed_renderer_release()["expected"], before)
        with mock.patch.object(self.codebuild, "batch_get_builds", side_effect=AssertionError("not a factory build")):
            self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 3})
        self.assertEqual(publisher.compare_sources.call_args_list,
                         [mock.call(self.source, source), mock.call(phone_source, source)] * 2)
        after = self.fleet_ssm.values["/release"]
        self.assertEqual(after["source_sha"], source)
        self.assertEqual(after["previous"], {key: value for key, value in before.items() if key != "previous"})
        self.assertTrue(after["build_id"].startswith("reviewed-renderer-release:"))
        for template in self.templates:
            name, kind = template["id"], template["kind"]
            table = self.ec2.templates[name]
            key = "us-east-1/" + name
            self.assertEqual(table["default"], after["versions"][key])
            self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                             {**table["versions"][before["versions"][key]]["LaunchTemplateData"],
                              "ImageId": images[kind]})
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 3})
        self.assertEqual(len(self.ec2.created), 4)
        self.assertEqual((self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], []))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_renderer_refuses_overrides_and_changed_predecessor(self):
        publisher, event, before, images, _, _ = self.reviewed_renderer_fixture()
        with self.assertRaisesRegex(MODULE.Refused, "no caller configuration"):
            publisher.run({**event, "images": images})
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.fleet_ssm.values["/release"]["previous"]["generation"] = 0
        with self.assertRaisesRegex(MODULE.Refused, "unchanged predecessor"):
            publisher.run(event)
        self.assertEqual(self.ec2.created, [])
        self.assertIsNone(self.ddb.item)

    def reviewed_potato_fixture(self):
        renderer, event, _, _, renderer_source, _ = self.reviewed_renderer_fixture()
        self.assertEqual(renderer.run(event), {"status": "promoted", "generation": 3})
        self.ec2.created.clear()
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        images = {kind: "ami-" + digit * 17 for kind, digit in (("host", "b"), ("tile", "c"))}
        for kind, image_id in images.items():
            image = copy.deepcopy(self.ec2.images[self.new[kind]])
            # An AMI name is unique in its region.
            image.update(ImageId=image_id, Name=f"fcvm-fleet-{kind}-x86_64-{image_id[4:12]}-202611010000")
            self.ec2.images[image_id] = image
        source, tile_source = "f" * 40, "9" * 40
        self.env["POTATO_AMI_JSON"] = json.dumps({
            "source_sha": source, "tile_source_sha": tile_source, "images": images, "versions": before["versions"],
        })
        publisher = self.publisher()
        publisher.compare_sources = mock.Mock(side_effect=lambda base, head: {
            "status": "ahead", "behind_by": 0, "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
        })
        return publisher, {"action": "activate-reviewed-potato-release"}, before, images, source, tile_source, renderer_source

    def test_reviewed_potato_pair_pins_both_sources_and_keeps_renderer_rollback(self):
        publisher, event, before, images, source, tile_source, renderer_source = self.reviewed_potato_fixture()
        self.assertEqual(publisher.reviewed_potato_release()[0]["expected"], before)
        with mock.patch.object(self.codebuild, "batch_get_builds", side_effect=AssertionError("not a factory build")):
            self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 4})
        self.assertEqual(publisher.compare_sources.call_args_list,
                         ([mock.call(base, tile_source) for base in sorted({self.source, renderer_source})]
                          + [mock.call(tile_source, source)]) * 2)
        after = self.fleet_ssm.values["/release"]
        self.assertEqual(after["source_sha"], source)
        self.assertEqual(after["previous"], {key: value for key, value in before.items() if key != "previous"})
        self.assertTrue(after["build_id"].startswith("reviewed-potato-release:"))
        for template in self.templates:
            name, kind = template["id"], template["kind"]
            table = self.ec2.templates[name]
            key = "us-east-1/" + name
            self.assertEqual(table["default"], after["versions"][key])
            self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                             {**table["versions"][before["versions"][key]]["LaunchTemplateData"],
                              "ImageId": images[kind]})
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 4})
        self.assertEqual(len(self.ec2.created), 4)
        self.assertEqual((self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], []))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_potato_refuses_overrides_newer_release_and_divergent_tile(self):
        for variant in ("override", "newer", "divergent-tile", "divergent-active"):
            with self.subTest(variant=variant):
                self.setUp()
                publisher, event, _, _, _, tile_source, renderer_source = self.reviewed_potato_fixture()
                if variant == "override":
                    event["tile_source_sha"] = "b" * 40
                elif variant == "newer":
                    self.fleet_ssm.values["/release"]["generation"] = 4
                else:
                    compare = publisher.compare_sources.side_effect
                    divergent_base = tile_source if variant == "divergent-tile" else renderer_source
                    publisher.compare_sources.side_effect = lambda base, head: (
                        {"status": "diverged"} if base == divergent_base else compare(base, head)
                    )
                before = copy.deepcopy(self.fleet_ssm.values["/release"])
                with self.assertRaises(MODULE.Refused):
                    publisher.run(event)
                self.assertEqual(self.fleet_ssm.values["/release"], before)
                self.assertEqual(self.ec2.created, [])
                self.assertIsNone(self.ddb.item)

    def reviewed_host_fix_fixture(self):
        potato, event, _, _, _, _, _ = self.reviewed_potato_fixture()
        self.assertEqual(potato.run(event), {"status": "promoted", "generation": 4})
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        host, image_id, source = self.ids[0], "ami-" + "d" * 17, "7" * 40
        image = copy.deepcopy(self.ec2.images[before["host_amis"]["us-east-1"]["x86_64"]])
        self.ec2.images[image_id] = {**image, "ImageId": image_id, "Name": f"fcvm-fleet-host-x86_64-{image_id[4:12]}-202611010000"}
        self.env["HOST_FIX_AMI_JSON"] = json.dumps({
            "source_sha": source, "host_image": image_id, "versions": before["versions"],
        })
        # Terraform prepares a new host default without changing the published image or tile defaults.
        table = self.ec2.templates[host]
        prepared = table["default"] + 1
        table["versions"][prepared] = copy.deepcopy(table["versions"][table["default"]])
        user_data = base64.b64encode(b"exact Terraform-managed bootstrap for both reviewed hosts").decode()
        table["versions"][prepared].update(VersionNumber=prepared, VersionDescription="terraform")
        table["versions"][prepared]["LaunchTemplateData"]["UserData"] = user_data
        table["default"] = prepared
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)
        self.ec2.created.clear()
        self.ec2.selected.clear()
        publisher = self.publisher()
        publisher.compare_sources = mock.Mock(return_value={
            "status": "ahead", "behind_by": 0, "base_commit": {"sha": before["source_sha"]},
            "merge_base_commit": {"sha": before["source_sha"]},
        })
        publisher.github_source_json = mock.Mock(return_value={
            "sha": source, "commit": {"verification": {"verified": True, "reason": "valid"}},
        })
        return publisher, {"action": "activate-reviewed-potato-host-fix"}, before, source, image_id

    def test_reviewed_host_fix_changes_one_image_and_keeps_exact_tiles_and_rollback(self):
        publisher, event, before, source, image_id = self.reviewed_host_fix_fixture()
        self.assertEqual(publisher.reviewed_potato_host_fix()["expected"], before)
        old_templates = copy.deepcopy(self.ec2.templates)
        host = self.ids[0]
        host_key = "us-east-1/" + host
        prepared = old_templates[host]["default"]
        with mock.patch.object(self.codebuild, "batch_get_builds", side_effect=AssertionError("not a factory build")):
            self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 5})
        self.assertEqual(publisher.github_source_json.call_args_list,
                         [mock.call("commits/" + source, "reviewed host source signature")] * 2)
        self.assertEqual(publisher.compare_sources.call_args_list, [mock.call(before["source_sha"], source)] * 2)
        after = self.fleet_ssm.values["/release"]
        self.assertEqual(after["source_sha"], source)
        self.assertEqual(after["tile_amis"], before["tile_amis"])
        self.assertEqual(after["host_amis"], {"us-east-1": {"x86_64": image_id}})
        self.assertEqual(after["previous"], {k: v for k, v in before.items() if k != "previous"})
        default = self.ec2.templates[host]["default"]
        self.assertEqual(after["versions"], {**before["versions"], host_key: default})
        self.assertEqual(self.ec2.templates[host]["versions"][default]["LaunchTemplateData"],
                         {**old_templates[host]["versions"][prepared]["LaunchTemplateData"], "ImageId": image_id})
        for tile in self.ids[1:]:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 5})
        self.assertEqual(self.ec2.created, [(host, prepared, default)])
        self.assertEqual(self.ec2.selected, [(host, default)])
        self.assertEqual((self.ec2.deleted, self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], [], []))
        self.ec2.templates[host]["default"] = prepared
        with self.assertRaisesRegex(MODULE.Refused, "host template default differs"):
            self.publisher().run(event)
        self.assertEqual(self.ec2.created, [(host, prepared, default)])
        self.assertIsNone(self.ddb.item)

    def test_reviewed_host_fix_refuses_unprepared_or_unproven_inputs_before_mutation(self):
        for variant in ("override", "generation", "unsigned", "wrong-commit", "divergent", "public-host",
                        "wrong-tile-tag", "tile-default", "old-host-default", "wrong-user-data"):
            with self.subTest(variant=variant):
                self.setUp()
                publisher, event, before, _, image_id = self.reviewed_host_fix_fixture()
                host, tile = (self.ec2.templates[name] for name in self.ids[:2])
                signature = publisher.github_source_json.return_value
                if variant == "override": event["host_image"] = image_id
                elif variant == "generation": self.fleet_ssm.values["/release"]["generation"] += 1
                elif variant == "unsigned": signature["commit"]["verification"]["verified"] = False
                elif variant == "wrong-commit": signature["sha"] = "8" * 40
                elif variant == "divergent": publisher.compare_sources.return_value["merge_base_commit"]["sha"] = "8" * 40
                elif variant == "public-host": self.ec2.images[image_id]["Public"] = True
                elif variant == "wrong-tile-tag":
                    tile_id = before["tile_amis"]["us-east-1"]["x86_64"]
                    next(tag for tag in self.ec2.images[tile_id]["Tags"] if tag["Key"] == "FleetImageValidated")["Value"] = "false"
                elif variant == "tile-default": tile["default"] -= 1
                elif variant == "old-host-default": host["default"] -= 1
                elif variant == "wrong-user-data": host["versions"][host["default"]]["LaunchTemplateData"]["UserData"] = "eA=="
                original = copy.deepcopy(self.fleet_ssm.values["/release"])
                journal = copy.deepcopy(self.main_ssm.values)
                with self.assertRaises(MODULE.Refused): publisher.run(event)
                self.assertEqual(self.fleet_ssm.values["/release"], original)
                self.assertEqual(self.main_ssm.values, journal)
                self.assertEqual((self.ec2.created, self.ec2.selected), ([], []))
                self.assertIsNone(self.ddb.item)

    def test_reviewed_host_fix_failed_pointer_write_rolls_back_host_only(self):
        publisher, event, before, _, _ = self.reviewed_host_fix_fixture()
        old_templates = copy.deepcopy(self.ec2.templates)
        with mock.patch.object(self.fleet_ssm, "put_parameter", side_effect=RuntimeError("no write")):
            with self.assertRaisesRegex(MODULE.Refused, "pointer update was not confirmed"):
                publisher.run(event)
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        host = self.ids[0]
        table = self.ec2.templates[host]
        self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                         old_templates[host]["versions"][old_templates[host]["default"]]["LaunchTemplateData"])
        for tile in self.ids[1:]:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        self.assertEqual(self.publisher().run(event), {"status": "failed", "reason": "rolled_back"})
        self.assertEqual([item[0] for item in self.ec2.created], [host, host])
        self.assertEqual([item[0] for item in self.ec2.deleted], [host])
        self.assertIsNone(self.ddb.item)

    def test_reviewed_host_fix_refuses_default_drift_during_final_source_fence(self):
        publisher, event, before, _, _ = self.reviewed_host_fix_fixture()
        host = self.ids[0]
        table = self.ec2.templates[host]
        prepared = table["default"]
        proof = publisher.compare_sources.return_value
        def compare(*_args):
            if publisher.compare_sources.call_count == 2:
                table["default"] = prepared
            return proof
        publisher.compare_sources.side_effect = compare
        with self.assertRaisesRegex(MODULE.Refused, "host template default differs"):
            publisher.run(event)
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual(table["default"], prepared)
        self.assertEqual([item[0] for item in self.ec2.deleted], [host])
        self.assertIsNone(self.ddb.item)

    def reviewed_stride_fixture(self):
        host_fix, event, _, _, _ = self.reviewed_host_fix_fixture()
        self.assertEqual(host_fix.run(event), {"status": "promoted", "generation": 5})
        before = self.fleet_ssm.values["/release"]
        host = self.ec2.templates[self.ids[0]]
        # Model the observed production generation-five host receipt and the
        # subsequent Terraform-only bootstrap change before #805 publication.
        prior = copy.deepcopy(host["versions"][host["default"]])
        host["versions"][21] = {**prior, "VersionNumber": 21}
        before["versions"]["us-east-1/" + self.ids[0]] = 21
        user_data = base64.b64encode(b"reviewed bootstrap for candidate, current and rollback hosts").decode()
        host["versions"][22] = {**prior, "VersionNumber": 22, "VersionDescription": "terraform",
                                "LaunchTemplateData": {**prior["LaunchTemplateData"], "UserData": user_data}}
        host["default"] = 22
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)
        catalog = (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text()
        images = {"host": "ami-09ac99f69700ec306", "tile": "ami-094ca9c62a284a389"}
        for kind, image_id in images.items():
            self.assertRegex(catalog, rf'"{kind}/x86_64/92a26793"\s*=\s*{{ image = "{image_id}", kind = "{kind}" }}')
            old_id = before[kind + "_amis"]["us-east-1"]["x86_64"]
            self.ec2.images[image_id] = {**copy.deepcopy(self.ec2.images[old_id]), "ImageId": image_id,
                "Name": "fcvm-fleet-" + kind + "-x86_64-92a26793-202611070000"}
        self.ec2.created.clear()
        self.ec2.selected.clear()
        publisher = self.publisher()
        source = "92a26793a818b142e71ba7d96b014d033f014562"
        publisher.github_source_json = mock.Mock(return_value={
            "sha": source, "commit": {"verification": {"verified": True, "reason": "valid"}},
        })
        publisher.compare_sources = mock.Mock(return_value={
            "status": "ahead", "behind_by": 0, "base_commit": {"sha": before["source_sha"]},
            "merge_base_commit": {"sha": before["source_sha"]},
        })
        return publisher, {"action": "activate-reviewed-stride-release"}, copy.deepcopy(before), images, source

    def test_reviewed_stride_pair_changes_only_images_and_preserves_generation_five(self):
        publisher, event, before, images, source = self.reviewed_stride_fixture()
        old_templates = copy.deepcopy(self.ec2.templates)
        with mock.patch.object(self.codebuild, "batch_get_builds", side_effect=AssertionError("not a factory build")):
            self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 6})
        after = self.fleet_ssm.values["/release"]
        self.assertEqual(after["source_sha"], source)
        self.assertEqual(after["previous"], {k: v for k, v in before.items() if k != "previous"})
        self.assertEqual(publisher.compare_sources.call_args_list, [mock.call(before["source_sha"], source)] * 2)
        self.assertEqual(len(self.ec2.created), 4)
        for template, kind in zip(self.ids, ("host", "tile", "tile", "tile"), strict=True):
            table, old = self.ec2.templates[template], old_templates[template]
            self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                             {**old["versions"][old["default"]]["LaunchTemplateData"], "ImageId": images[kind]})
            self.assertEqual(after["versions"]["us-east-1/" + template], table["default"])
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 6})
        self.assertEqual(len(self.ec2.created), 4)
        self.assertEqual((self.ec2.tagged, self.ec2.terminated, self.assets.copies), ([], [], []))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_stride_pair_refuses_unprepared_or_unproven_inputs_before_mutation(self):
        for variant in ("override", "predecessor", "unsigned", "unvalidated-tile", "bootstrap", "tile-default"):
            with self.subTest(variant=variant):
                self.setUp()
                publisher, event, _, images, _ = self.reviewed_stride_fixture()
                if variant == "override":
                    event["host_image"] = images["host"]
                elif variant == "predecessor":
                    self.fleet_ssm.values["/release"]["previous"]["generation"] = 3
                elif variant == "unsigned":
                    publisher.github_source_json.return_value["commit"]["verification"]["verified"] = False
                elif variant == "unvalidated-tile":
                    tags = self.ec2.images[images["tile"]]["Tags"]
                    next(tag for tag in tags if tag["Key"] == "FleetImageValidated")["Value"] = "false"
                elif variant == "bootstrap":
                    self.ec2.templates[self.ids[0]]["default"] = 21
                elif variant == "tile-default":
                    self.ec2.templates[self.ids[1]]["default"] -= 1
                before = copy.deepcopy(self.fleet_ssm.values["/release"])
                journal = copy.deepcopy(self.main_ssm.values)
                with self.assertRaises(MODULE.Refused):
                    publisher.run(event)
                self.assertEqual(self.fleet_ssm.values["/release"], before)
                self.assertEqual(self.main_ssm.values, journal)
                self.assertEqual((self.ec2.created, self.ec2.selected), ([], []))
                self.assertIsNone(self.ddb.item)

    def test_reviewed_stride_pair_restores_all_defaults_if_pointer_write_fails(self):
        publisher, event, before, _, _ = self.reviewed_stride_fixture()
        old_templates = copy.deepcopy(self.ec2.templates)
        with mock.patch.object(self.fleet_ssm, "put_parameter", side_effect=RuntimeError("no write")):
            with self.assertRaisesRegex(MODULE.Refused, "pointer update was not confirmed"):
                publisher.run(event)
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        for template in self.ids:
            table, old = self.ec2.templates[template], old_templates[template]
            self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                             old["versions"][old["default"]]["LaunchTemplateData"])
        self.assertEqual(self.publisher().run(event), {"status": "failed", "reason": "rolled_back"})
        self.assertCountEqual([item[0] for item in self.ec2.deleted], self.ids)
        self.assertIsNone(self.ddb.item)

    def reviewed_retake_host_fixture(self):
        stride, event, _, images, _ = self.reviewed_stride_fixture()
        self.assertEqual(stride.run(event), {"status": "promoted", "generation": 6})
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        before_copies = copy.deepcopy(self.fleet_ssm.values["/release-copies"])
        host, image_id = self.ids[0], "ami-05923927f810a2ce9"
        source = "343b2287636d07b1726ab7a2d431c9a1ec854a67"
        catalog = (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text()
        self.assertRegex(catalog, rf'"host/x86_64/343b2287"\s*=\s*{{ image = "{image_id}", kind = "host" }}')
        self.ec2.images[image_id] = {
            **copy.deepcopy(self.ec2.images[images["host"]]), "ImageId": image_id,
            "Name": "fcvm-fleet-host-x86_64-343b2287-202611071247",
        }
        table = self.ec2.templates[host]
        self.assertEqual(table["default"], 23)
        prepared = copy.deepcopy(table["versions"][23])
        user_data = base64.b64encode(b"reviewed bootstrap includes both gen6 and gen7 hosts").decode()
        prepared.update(VersionNumber=24, VersionDescription="terraform")
        prepared["LaunchTemplateData"]["UserData"] = user_data
        table["versions"][24] = prepared
        table["default"] = 24
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)
        self.ec2.created.clear()
        self.ec2.selected.clear()
        self.west.copies.clear()
        publisher = self.publisher()
        publisher.github_source_json = mock.Mock(return_value={
            "sha": source, "commit": {"verification": {"verified": True, "reason": "valid"}},
        })
        publisher.compare_sources = mock.Mock(return_value={
            "status": "ahead", "behind_by": 0, "base_commit": {"sha": before["source_sha"]},
            "merge_base_commit": {"sha": before["source_sha"]},
        })
        return publisher, {"action": "activate-reviewed-retake-host-release"}, before, before_copies, source, image_id

    def test_reviewed_retake_host_commits_once_and_reuses_the_generation_six_west_tile(self):
        publisher, event, before, before_copies, source, image_id = self.reviewed_retake_host_fixture()
        old_templates = copy.deepcopy(self.ec2.templates)
        host, *tiles = self.ids
        self.west.copy_state = "pending"
        with self.assertRaisesRegex(MODULE.Refused, "regional AMI copies are still pending: us-west-2/host"):
            publisher.run(event)
        after = copy.deepcopy(self.fleet_ssm.values["/release"])
        self.assertEqual(after["generation"], 7)
        self.assertEqual(after["source_sha"], source)
        self.assertEqual(after["host_amis"], {"us-east-1": {"x86_64": image_id}})
        self.assertEqual(after["tile_amis"], before["tile_amis"])
        self.assertEqual(after["previous"], {k: v for k, v in before.items() if k != "previous"})
        self.assertEqual(after["versions"], {**before["versions"], "us-east-1/" + host: 25})
        self.assertEqual(self.ec2.templates[host]["versions"][25]["LaunchTemplateData"],
                         {**old_templates[host]["versions"][24]["LaunchTemplateData"], "ImageId": image_id})
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        self.assertEqual(publisher.compare_sources.call_args_list, [mock.call(before["source_sha"], source)] * 2)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], before_copies)
        self.assertEqual([image for image, _ in self.west.copies], [image_id])
        self.assertIsNone(self.ddb.item)

        with self.assertRaisesRegex(MODULE.Refused, "regional AMI copies are still pending: us-west-2/host"):
            self.publisher().run(event)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], before_copies)
        self.west.available()
        self.assertEqual(self.publisher().run({"action": "replicate-active-release"}), {"status": "replicated", "generation": 7})
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 7})
        copies = self.fleet_ssm.values["/release-copies"]
        self.assertEqual(copies["generation"], 7)
        self.assertEqual(copies["build_id"], after["build_id"])
        self.assertEqual(copies["images"]["us-west-2"], {
            "host": self.west.copies[0][1], "tile": before_copies["images"]["us-west-2"]["tile"],
        })
        self.assertEqual(copies["previous_images"], before_copies["images"])
        self.assertEqual(self.fleet_ssm.values["/release"], after)
        self.assertEqual(self.ec2.created, [(host, 24, 25)])
        self.assertEqual(self.ec2.selected, [(host, 25)])
        self.assertEqual(len(self.west.copies), 1)
        self.assertEqual((self.ec2.deleted, self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], [], []))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_retake_host_requires_exact_predecessor_and_candidate(self):
        reasons = {
            "caller-image": "accepts no caller configuration",
            "predecessor": "requires its unchanged predecessor",
            "rollback": "requires its unchanged predecessor",
            "unsigned": "reviewed host source signature is invalid",
            "candidate-tag": "reviewed image is outside its private validated scope",
            "host-default": "Terraform has not prepared the exact reviewed host bootstrap",
            "tile-default": "retained tile template default differs",
        }
        for variant, reason in reasons.items():
            with self.subTest(variant=variant):
                self.setUp()
                publisher, event, _, _, _, image_id = self.reviewed_retake_host_fixture()
                if variant == "caller-image":
                    event["host_image"] = image_id
                elif variant == "predecessor":
                    self.fleet_ssm.values["/release"]["versions"]["us-east-1/" + self.ids[1]] += 1
                elif variant == "rollback":
                    self.fleet_ssm.values["/release"]["previous"]["build_id"] = "changed"
                elif variant == "unsigned":
                    publisher.github_source_json.return_value["commit"]["verification"]["verified"] = False
                elif variant == "candidate-tag":
                    tags = self.ec2.images[image_id]["Tags"]
                    next(tag for tag in tags if tag["Key"] == "FleetImageValidated")["Value"] = "false"
                elif variant == "host-default":
                    self.ec2.templates[self.ids[0]]["default"] = 23
                elif variant == "tile-default":
                    self.ec2.templates[self.ids[1]]["default"] -= 1
                before = copy.deepcopy(self.fleet_ssm.values)
                journal = copy.deepcopy(self.main_ssm.values)
                with self.assertRaisesRegex(MODULE.Refused, reason):
                    publisher.run(event)
                self.assertEqual(self.fleet_ssm.values, before)
                self.assertEqual(self.main_ssm.values, journal)
                self.assertEqual((self.ec2.created, self.ec2.selected, self.west.copies), ([], [], []))
                self.assertIsNone(self.ddb.item)

    def test_reviewed_retake_host_failed_pointer_restores_only_the_generation_six_host(self):
        publisher, event, before, before_copies, _, _ = self.reviewed_retake_host_fixture()
        old = copy.deepcopy(self.ec2.templates)
        host, *tiles = self.ids
        with mock.patch.object(self.fleet_ssm, "put_parameter", side_effect=RuntimeError("no write")):
            with self.assertRaisesRegex(MODULE.Refused, "pointer update was not confirmed"):
                publisher.run(event)
        table = self.ec2.templates[host]
        self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                         old[host]["versions"][24]["LaunchTemplateData"])
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old[tile])
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], before_copies)
        self.assertEqual(self.west.copies, [])
        self.assertEqual([name for name, _ in self.ec2.deleted], [host])
        self.assertEqual(self.publisher().run(event), {"status": "failed", "reason": "rolled_back"})
        self.assertIsNone(self.ddb.item)

    HELD_STAMP_SOURCE = "fb2f5c361175bb8a0b5baac141e359906543e101"
    HELD_STAMP_IMAGE = "ami-0d176763f9f33416e"
    HELD_STAMP_NAME = "fcvm-fleet-host-x86_64-fb2f5c36-202611072303"

    def held_stamp_publisher(self, before):
        publisher = self.publisher()
        publisher.github_source_json = mock.Mock(return_value={
            "sha": self.HELD_STAMP_SOURCE, "commit": {"verification": {"verified": True, "reason": "valid"}},
        })
        publisher.compare_sources = mock.Mock(return_value={
            "status": "ahead", "behind_by": 0, "base_commit": {"sha": before["source_sha"]},
            "merge_base_commit": {"sha": before["source_sha"]},
        })
        return publisher

    def prepare_held_stamp_bootstrap(self, copied):
        # Terraform's second apply: the potato list names the copy, which changes the host bootstrap.
        table = self.ec2.templates[self.ids[0]]
        self.assertEqual(table["default"], 25)
        prepared = copy.deepcopy(table["versions"][25])
        user_data = base64.b64encode(b"reviewed bootstrap names the gen6, gen7 and gen8 hosts: " + copied.encode()).decode()
        prepared.update(VersionNumber=26, VersionDescription="terraform")
        prepared["LaunchTemplateData"]["UserData"] = user_data
        table["versions"][26] = prepared
        table["default"] = 26
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)

    def reviewed_held_stamp_host_fixture(self, prepared=True):
        """Generation seven live, and the fb2f5c36 host built by hand in us-west-2 (#807's build
        region) and tagged validated there by Terraform. Prepared: the publisher has copied it to
        us-east-1 and Terraform's bootstrap names the copy."""
        retake, event, _, _, _, _ = self.reviewed_retake_host_fixture()
        self.assertEqual(retake.run(event), {"status": "promoted", "generation": 7})
        self.env["BUILD_REGION"] = "us-west-2"   # production's, since #807
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        before_copies = copy.deepcopy(self.fleet_ssm.values["/release-copies"])
        original = self.HELD_STAMP_IMAGE
        catalog = (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text()
        self.assertRegex(catalog, rf'"host/x86_64/fb2f5c36"\s*=\s*{{ image = "{original}", kind = "host", '
                                  rf'name = "{self.HELD_STAMP_NAME}" }}')
        image = copy.deepcopy(self.ec2.images[before["host_amis"]["us-east-1"]["x86_64"]])
        # what build-ami.sh's CreateImage reports: its builder's base image as the ancestry
        image.update(ImageId=original, Name=self.HELD_STAMP_NAME, State="available",
                     Description="fcvm fleet: " + self.HELD_STAMP_NAME,
                     SourceImageId="ami-0ddddddddddddddd0", SourceImageRegion="us-west-2")
        self.west.images[original] = image
        self.ec2.created.clear()
        self.ec2.selected.clear()
        self.ec2.copies.clear()
        self.west.copies.clear()
        event = {"action": "activate-reviewed-held-stamp-host-release"}
        if prepared:
            with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
                self.held_stamp_publisher(before).run(event)
            self.ec2.available()
            self.prepare_held_stamp_bootstrap(self.ec2.copies[0][1])
        return self.held_stamp_publisher(before), event, before, before_copies, original

    def test_reviewed_held_stamp_host_releases_the_publishers_copy_of_the_west_build(self):
        publisher, event, before, before_copies, original = self.reviewed_held_stamp_host_fixture(prepared=False)
        host, *tiles = self.ids
        old_templates = copy.deepcopy(self.ec2.templates)
        with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
            publisher.run(event)
        [(source_image, copied)] = self.ec2.copies
        self.assertEqual(source_image, original)
        made = self.ec2.images[copied]
        self.assertEqual((made["Name"], made["SourceImageId"], made["SourceImageRegion"], made["Description"]),
                         (self.HELD_STAMP_NAME, original, "us-west-2", "copy of " + original + " from us-west-2"))
        with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
            self.held_stamp_publisher(before).run(event)
        self.assertEqual(len(self.ec2.copies), 1)
        self.ec2.available()
        with self.assertRaisesRegex(MODULE.Refused, "host bootstrap does not name the release-region copy yet"):
            self.held_stamp_publisher(before).run(event)
        self.assertEqual(self.ec2.templates, old_templates)
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], before_copies)

        self.prepare_held_stamp_bootstrap(copied)
        old_templates = copy.deepcopy(self.ec2.templates)
        publisher = self.held_stamp_publisher(before)
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 8})
        after = copy.deepcopy(self.fleet_ssm.values["/release"])
        self.assertEqual(after["generation"], 8)
        self.assertEqual(after["source_sha"], self.HELD_STAMP_SOURCE)
        self.assertEqual(after["build_id"], "reviewed-held-stamp-host-release:"
                         + MODULE.digest(MODULE.encoded(publisher.reviewed_held_stamp_host_release())))
        self.assertEqual(after["host_amis"], {"us-east-1": {"x86_64": copied}})
        self.assertEqual(after["tile_amis"], before["tile_amis"])
        self.assertEqual(after["previous"], {k: v for k, v in before.items() if k != "previous"})
        self.assertEqual(after["versions"], {**before["versions"], "us-east-1/" + host: 27})
        self.assertEqual(self.ec2.templates[host]["versions"][27]["LaunchTemplateData"],
                         {**old_templates[host]["versions"][26]["LaunchTemplateData"], "ImageId": copied})
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        record = self.fleet_ssm.values["/release-copies"]
        self.assertEqual((record["generation"], record["build_id"], record["source_sha"]),
                         (8, after["build_id"], self.HELD_STAMP_SOURCE))
        self.assertEqual(record["images"], {
            "us-east-1": {"host": copied, "tile": before["tile_amis"]["us-east-1"]["x86_64"]},
            # the build region's entry is the hand-built original, not a copy of the copy
            "us-west-2": {"host": original, "tile": before_copies["images"]["us-west-2"]["tile"]},
        })
        self.assertEqual(record["previous_images"], before_copies["images"])
        self.assertEqual(self.west.copies, [])
        self.assertEqual(self.ec2.created, [(host, 26, 27)])
        self.assertEqual(self.ec2.selected, [(host, 27)])

        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 8})
        self.assertEqual(self.fleet_ssm.values["/release"], after)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], record)
        self.assertEqual((len(self.ec2.copies), self.west.copies, self.ec2.created), (1, [], [(host, 26, 27)]))
        self.assertEqual((self.ec2.deleted, self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], [], []))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_held_stamp_host_requires_exact_predecessor_and_candidate(self):
        reasons = {
            "caller-image": "accepts no caller configuration",
            "predecessor": "requires its unchanged predecessor",
            "rollback": "requires its unchanged predecessor",
            "unsigned": "reviewed host source signature is invalid",
            "west-unvalidated": "reviewed image is outside its private validated scope",
            "west-a-copy": "reviewed image is outside its private validated scope",
            "candidate-tag": "a regional copy is outside its private image lineage",
            "bootstrap-without-copy": "host bootstrap does not name the release-region copy yet",
            "host-default": "Terraform has not prepared the exact reviewed host bootstrap",
            "tile-default": "retained tile template default differs",
        }
        for variant, reason in reasons.items():
            with self.subTest(variant=variant):
                self.setUp()
                publisher, event, _, _, original = self.reviewed_held_stamp_host_fixture()
                copied = self.ec2.copies[0][1]
                if variant == "caller-image":
                    event["host_image"] = original
                elif variant == "predecessor":
                    self.fleet_ssm.values["/release"]["versions"]["us-east-1/" + self.ids[1]] += 1
                elif variant == "rollback":
                    self.fleet_ssm.values["/release"]["previous"]["build_id"] = "changed"
                elif variant == "unsigned":
                    publisher.github_source_json.return_value["commit"]["verification"]["verified"] = False
                elif variant == "west-unvalidated":
                    tags = self.west.images[original]["Tags"]
                    next(tag for tag in tags if tag["Key"] == "FleetImageValidated")["Value"] = "false"
                elif variant == "west-a-copy":
                    self.west.images[original].update(Description="copy of ami-0bbbbbbbbbbbbbbb0 from us-east-1",
                                                      SourceImageId="ami-0bbbbbbbbbbbbbbb0", SourceImageRegion="us-east-1")
                elif variant == "candidate-tag":
                    tags = self.ec2.images[copied]["Tags"]
                    next(tag for tag in tags if tag["Key"] == "FleetImageValidated")["Value"] = "false"
                elif variant == "bootstrap-without-copy":
                    data = self.ec2.templates[self.ids[0]]["versions"][26]["LaunchTemplateData"]
                    data["UserData"] = base64.b64encode(b"reviewed bootstrap names only the gen6 and gen7 hosts").decode()
                    self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(data["UserData"])
                elif variant == "host-default":
                    self.ec2.templates[self.ids[0]]["default"] = 25
                    self.ec2.templates[self.ids[0]]["versions"][25]["LaunchTemplateData"]["UserData"] = \
                        self.ec2.templates[self.ids[0]]["versions"][26]["LaunchTemplateData"]["UserData"]
                elif variant == "tile-default":
                    self.ec2.templates[self.ids[1]]["default"] -= 1
                before = copy.deepcopy(self.fleet_ssm.values)
                journal = copy.deepcopy(self.main_ssm.values)
                with self.assertRaisesRegex(MODULE.Refused, reason):
                    publisher.run(event)
                self.assertEqual(self.fleet_ssm.values, before)
                self.assertEqual(self.main_ssm.values, journal)
                self.assertEqual((self.ec2.created, self.ec2.selected, self.west.copies, len(self.ec2.copies)), ([], [], [], 1))
                self.assertIsNone(self.ddb.item)

    def test_reviewed_held_stamp_host_failed_pointer_restores_only_the_generation_seven_host(self):
        publisher, event, before, before_copies, _ = self.reviewed_held_stamp_host_fixture()
        old = copy.deepcopy(self.ec2.templates)
        host, *tiles = self.ids
        with mock.patch.object(self.fleet_ssm, "put_parameter", side_effect=RuntimeError("no write")):
            with self.assertRaisesRegex(MODULE.Refused, "pointer update was not confirmed"):
                publisher.run(event)
        table = self.ec2.templates[host]
        self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                         old[host]["versions"][26]["LaunchTemplateData"])
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old[tile])
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], before_copies)
        self.assertEqual(self.west.copies, [])
        self.assertEqual([name for name, _ in self.ec2.deleted], [host])
        self.assertEqual(self.publisher().run(event), {"status": "failed", "reason": "rolled_back"})
        self.assertIsNone(self.ddb.item)

    WINDOWS_SOURCE = "853eb034a4611e4e4d3b7e79caaf03cf5b2ed643"
    WINDOWS_IMAGE = "ami-0925206c2b9eac0fe"
    WINDOWS_NAME = "fcvm-fleet-host-x86_64-853eb034-202611091050"

    def windows_publisher(self, before):
        publisher = self.publisher()
        publisher.github_source_json = mock.Mock(return_value={
            "sha": self.WINDOWS_SOURCE, "commit": {"verification": {"verified": True, "reason": "valid"}},
        })
        publisher.compare_sources = mock.Mock(return_value={
            "status": "ahead", "behind_by": 0, "base_commit": {"sha": before["source_sha"]},
            "merge_base_commit": {"sha": before["source_sha"]},
        })
        return publisher

    def prepare_windows_bootstrap(self, copied):
        # Terraform's second apply: the potato list names the copy, which changes the host bootstrap.
        table = self.ec2.templates[self.ids[0]]
        self.assertEqual(table["default"], 27)
        prepared = copy.deepcopy(table["versions"][27])
        user_data = base64.b64encode(b"reviewed bootstrap with --windows 4 names the gen6, gen7, gen8 and gen9 hosts: " + copied.encode()).decode()
        prepared.update(VersionNumber=28, VersionDescription="terraform")
        prepared["LaunchTemplateData"]["UserData"] = user_data
        table["versions"][28] = prepared
        table["default"] = 28
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)

    def reviewed_windows_host_fixture(self, prepared=True):
        """Generation eight live, and the 853eb034 host built by hand in us-west-2 and tagged
        validated there by Terraform. Prepared: the publisher has copied it to us-east-1 and
        Terraform's bootstrap names the copy."""
        held, event, _, _, _ = self.reviewed_held_stamp_host_fixture()
        self.assertEqual(held.run(event), {"status": "promoted", "generation": 8})
        # The generation-eight receipt names the publisher's copy, whose id this fake EC2 chose.
        held_copy = self.fleet_ssm.values["/release"]["host_amis"]["us-east-1"]["x86_64"]
        patcher = mock.patch.object(MODULE.Publisher, "HELD_STAMP_HOST_COPY", held_copy)
        patcher.start()
        self.addCleanup(patcher.stop)
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        before_copies = copy.deepcopy(self.fleet_ssm.values["/release-copies"])
        original = self.WINDOWS_IMAGE
        catalog = (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text()
        self.assertRegex(catalog, rf'"host/x86_64/853eb034"\s*=\s*{{ image = "{original}", kind = "host", '
                                  rf'name = "{self.WINDOWS_NAME}" }}')
        image = copy.deepcopy(self.ec2.images[before["host_amis"]["us-east-1"]["x86_64"]])
        # what build-ami.sh's CreateImage reports: its builder's base image as the ancestry
        image.update(ImageId=original, Name=self.WINDOWS_NAME, State="available",
                     Description="fcvm fleet: " + self.WINDOWS_NAME,
                     SourceImageId="ami-0ddddddddddddddd0", SourceImageRegion="us-west-2")
        self.west.images[original] = image
        self.ec2.created.clear()
        self.ec2.selected.clear()
        self.ec2.copies.clear()
        self.west.copies.clear()
        event = {"action": "activate-reviewed-windows-host-release"}
        if prepared:
            with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
                self.windows_publisher(before).run(event)
            self.ec2.available()
            self.prepare_windows_bootstrap(self.ec2.copies[0][1])
        return self.windows_publisher(before), event, before, before_copies, original

    def test_reviewed_windows_host_releases_the_publishers_copy_of_the_west_build(self):
        publisher, event, before, before_copies, original = self.reviewed_windows_host_fixture(prepared=False)
        host, *tiles = self.ids
        old_templates = copy.deepcopy(self.ec2.templates)
        with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
            publisher.run(event)
        [(source_image, copied)] = self.ec2.copies
        self.assertEqual(source_image, original)
        made = self.ec2.images[copied]
        self.assertEqual((made["Name"], made["SourceImageId"], made["SourceImageRegion"], made["Description"]),
                         (self.WINDOWS_NAME, original, "us-west-2", "copy of " + original + " from us-west-2"))
        with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
            self.windows_publisher(before).run(event)
        self.assertEqual(len(self.ec2.copies), 1)
        self.ec2.available()
        with self.assertRaisesRegex(MODULE.Refused, "host bootstrap does not name the release-region copy yet"):
            self.windows_publisher(before).run(event)
        self.assertEqual(self.ec2.templates, old_templates)
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], before_copies)

        self.prepare_windows_bootstrap(copied)
        old_templates = copy.deepcopy(self.ec2.templates)
        publisher = self.windows_publisher(before)
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 9})
        after = copy.deepcopy(self.fleet_ssm.values["/release"])
        self.assertEqual(after["generation"], 9)
        self.assertEqual(after["source_sha"], self.WINDOWS_SOURCE)
        self.assertEqual(after["build_id"], "reviewed-windows-host-release:"
                         + MODULE.digest(MODULE.encoded(publisher.reviewed_windows_host_release())))
        self.assertEqual(after["host_amis"], {"us-east-1": {"x86_64": copied}})
        self.assertEqual(after["tile_amis"], before["tile_amis"])
        self.assertEqual(after["previous"], {k: v for k, v in before.items() if k != "previous"})
        self.assertEqual(after["versions"], {**before["versions"], "us-east-1/" + host: 29})
        self.assertEqual(self.ec2.templates[host]["versions"][29]["LaunchTemplateData"],
                         {**old_templates[host]["versions"][28]["LaunchTemplateData"], "ImageId": copied})
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        record = self.fleet_ssm.values["/release-copies"]
        self.assertEqual((record["generation"], record["build_id"], record["source_sha"]),
                         (9, after["build_id"], self.WINDOWS_SOURCE))
        self.assertEqual(record["images"], {
            "us-east-1": {"host": copied, "tile": before["tile_amis"]["us-east-1"]["x86_64"]},
            # the build region's entry is the hand-built original, not a copy of the copy
            "us-west-2": {"host": original, "tile": before_copies["images"]["us-west-2"]["tile"]},
        })
        self.assertEqual(record["previous_images"], before_copies["images"])
        self.assertEqual(self.west.copies, [])
        self.assertEqual(self.ec2.created, [(host, 28, 29)])
        self.assertEqual(self.ec2.selected, [(host, 29)])

        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 9})
        self.assertEqual(self.fleet_ssm.values["/release"], after)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], record)
        self.assertEqual((len(self.ec2.copies), self.west.copies, self.ec2.created), (1, [], [(host, 28, 29)]))
        self.assertEqual((self.ec2.deleted, self.ec2.tagged, self.assets.copies, self.ec2.terminated), ([], [], [], []))
        self.assertIsNone(self.ddb.item)

    def test_reviewed_windows_host_requires_exact_predecessor_and_candidate(self):
        reasons = {
            "caller-image": "accepts no caller configuration",
            "predecessor": "requires its unchanged predecessor",
            "rollback": "requires its unchanged predecessor",
            "unsigned": "reviewed host source signature is invalid",
            "west-unvalidated": "reviewed image is outside its private validated scope",
            "west-a-copy": "reviewed image is outside its private validated scope",
            "candidate-tag": "a regional copy is outside its private image lineage",
            "bootstrap-without-copy": "host bootstrap does not name the release-region copy yet",
            "host-default": "Terraform has not prepared the exact reviewed host bootstrap",
            "tile-default": "retained tile template default differs",
        }
        for variant, reason in reasons.items():
            with self.subTest(variant=variant):
                self.setUp()
                publisher, event, _, _, original = self.reviewed_windows_host_fixture()
                copied = self.ec2.copies[0][1]
                if variant == "caller-image":
                    event["host_image"] = original
                elif variant == "predecessor":
                    self.fleet_ssm.values["/release"]["versions"]["us-east-1/" + self.ids[1]] += 1
                elif variant == "rollback":
                    self.fleet_ssm.values["/release"]["previous"]["build_id"] = "changed"
                elif variant == "unsigned":
                    publisher.github_source_json.return_value["commit"]["verification"]["verified"] = False
                elif variant == "west-unvalidated":
                    tags = self.west.images[original]["Tags"]
                    next(tag for tag in tags if tag["Key"] == "FleetImageValidated")["Value"] = "false"
                elif variant == "west-a-copy":
                    self.west.images[original].update(Description="copy of ami-0bbbbbbbbbbbbbbb0 from us-east-1",
                                                      SourceImageId="ami-0bbbbbbbbbbbbbbb0", SourceImageRegion="us-east-1")
                elif variant == "candidate-tag":
                    tags = self.ec2.images[copied]["Tags"]
                    next(tag for tag in tags if tag["Key"] == "FleetImageValidated")["Value"] = "false"
                elif variant == "bootstrap-without-copy":
                    data = self.ec2.templates[self.ids[0]]["versions"][28]["LaunchTemplateData"]
                    data["UserData"] = base64.b64encode(b"reviewed bootstrap names only the gen6, gen7 and gen8 hosts").decode()
                    self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(data["UserData"])
                elif variant == "host-default":
                    self.ec2.templates[self.ids[0]]["default"] = 27
                    self.ec2.templates[self.ids[0]]["versions"][27]["LaunchTemplateData"]["UserData"] = \
                        self.ec2.templates[self.ids[0]]["versions"][28]["LaunchTemplateData"]["UserData"]
                elif variant == "tile-default":
                    self.ec2.templates[self.ids[1]]["default"] -= 1
                before = copy.deepcopy(self.fleet_ssm.values)
                journal = copy.deepcopy(self.main_ssm.values)
                with self.assertRaisesRegex(MODULE.Refused, reason):
                    publisher.run(event)
                self.assertEqual(self.fleet_ssm.values, before)
                self.assertEqual(self.main_ssm.values, journal)
                self.assertEqual((self.ec2.created, self.ec2.selected, self.west.copies, len(self.ec2.copies)), ([], [], [], 1))
                self.assertIsNone(self.ddb.item)

    def test_reviewed_windows_host_failed_pointer_restores_only_the_generation_eight_host(self):
        publisher, event, before, before_copies, _ = self.reviewed_windows_host_fixture()
        old = copy.deepcopy(self.ec2.templates)
        host, *tiles = self.ids
        with mock.patch.object(self.fleet_ssm, "put_parameter", side_effect=RuntimeError("no write")):
            with self.assertRaisesRegex(MODULE.Refused, "pointer update was not confirmed"):
                publisher.run(event)
        table = self.ec2.templates[host]
        self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"],
                         old[host]["versions"][28]["LaunchTemplateData"])
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old[tile])
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], before_copies)
        self.assertEqual(self.west.copies, [])
        self.assertEqual([name for name, _ in self.ec2.deleted], [host])
        self.assertEqual(self.publisher().run(event), {"status": "failed", "reason": "rolled_back"})
        self.assertIsNone(self.ddb.item)

    RESTORE_SOURCE = "0e910245f92d3ff093ec1fad1fdbe69fef948f3c"
    RESTORE_IMAGE = "ami-0c1f87eea5c86c172"
    RESTORE_NAME = "fcvm-fleet-host-x86_64-0e910245-202611121727"

    def reviewed_restore_host_fixture(self):
        """Generation nine live (the 853eb034 host, released by its own action), and the 0e910245
        host built by hand in us-west-2 and tagged validated there by Terraform."""
        publisher, event, _, _, _ = self.reviewed_windows_host_fixture()
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 9})
        nine = copy.deepcopy(self.fleet_ssm.values["/release"])
        # Generation ten's predecessor constants, as they are filled from the generation-nine receipt.
        for name, value in (("WINDOWS_HOST_COPY", nine["host_amis"]["us-east-1"]["x86_64"]),
                            ("WINDOWS_HOST_VERSION", nine["versions"]["us-east-1/" + self.ids[0]])):
            patcher = mock.patch.object(MODULE.Publisher, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        before_copies = copy.deepcopy(self.fleet_ssm.values["/release-copies"])
        original = self.RESTORE_IMAGE
        catalog = (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text()
        self.assertRegex(catalog, rf'"host/x86_64/{self.RESTORE_SOURCE[:8]}"\s*=\s*{{ image = "{original}", kind = "host", '
                                  rf'name = "{self.RESTORE_NAME}" }}')
        image = copy.deepcopy(self.west.images[self.WINDOWS_IMAGE])
        image.update(ImageId=original, Name=self.RESTORE_NAME, State="available",
                     Description="fcvm fleet: " + self.RESTORE_NAME, SourceImageId=self.WINDOWS_IMAGE,
                     SourceImageRegion="us-west-2")
        self.west.images[original] = image
        self.ec2.created.clear()
        self.ec2.selected.clear()
        self.ec2.copies.clear()
        publisher = self.windows_publisher(nine)
        publisher.github_source_json.return_value["sha"] = self.RESTORE_SOURCE
        return publisher, {"action": "activate-reviewed-restore-host-release"}, nine, before_copies, original

    def test_reviewed_restore_host_releases_generation_ten_after_generation_nine(self):
        publisher, event, nine, nine_copies, original = self.reviewed_restore_host_fixture()
        host, *tiles = self.ids
        with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
            publisher.run(event)
        [(source_image, copied)] = self.ec2.copies
        self.assertEqual(source_image, original)
        self.ec2.available()
        with self.assertRaisesRegex(MODULE.Refused, "host bootstrap does not name the release-region copy yet"):
            publisher.run(event)
        # Terraform's second apply: the bootstrap names the generation-ten copy too.
        table = self.ec2.templates[host]
        version = table["default"]
        prepared = copy.deepcopy(table["versions"][version])
        user_data = base64.b64encode(b"reviewed bootstrap with --windows 4 and --minecraft 4 names the gen9 and gen10 hosts: "
                                     + copied.encode()).decode()
        prepared.update(VersionNumber=version + 1, VersionDescription="terraform")
        prepared["LaunchTemplateData"]["UserData"] = user_data
        table["versions"][version + 1] = prepared
        table["default"] = version + 1
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)
        old_templates = copy.deepcopy(self.ec2.templates)
        # A publisher reads its environment when it starts: the next invocation sees the new digest.
        publisher = self.windows_publisher(nine)
        publisher.github_source_json.return_value["sha"] = self.RESTORE_SOURCE
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 10})
        after = self.fleet_ssm.values["/release"]
        self.assertEqual((after["generation"], after["source_sha"]), (10, self.RESTORE_SOURCE))
        self.assertEqual(after["build_id"], "reviewed-restore-host-release:"
                         + MODULE.digest(MODULE.encoded(publisher.reviewed_restore_host_release())))
        self.assertEqual(after["host_amis"], {"us-east-1": {"x86_64": copied}})
        self.assertEqual(after["tile_amis"], nine["tile_amis"])
        self.assertEqual(after["previous"], {k: v for k, v in nine.items() if k != "previous"})
        self.assertEqual(after["versions"], {**nine["versions"], "us-east-1/" + host: version + 2})
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        self.assertEqual(self.fleet_ssm.values["/release-copies"]["images"]["us-west-2"]["host"], original)
        # A replay is the same receipt.
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 10})
        self.assertIsNone(self.ddb.item)

    def test_reviewed_restore_host_names_the_generation_nine_receipt(self):
        # Filled from the live generation-nine receipt: an image id and a template version.
        self.assertRegex(MODULE.Publisher.WINDOWS_HOST_COPY, r"^ami-[0-9a-f]{17}$")
        self.assertIs(type(MODULE.Publisher.WINDOWS_HOST_VERSION), int)
        self.assertGreater(MODULE.Publisher.WINDOWS_HOST_VERSION, 0)

    def test_reviewed_restore_host_refuses_a_predecessor_other_than_the_one_it_names(self):
        # The constants name a generation-nine receipt that is not the live one (here the host
        # version differs): the action refuses at the predecessor check and changes nothing.
        publisher, event, nine, _, _ = self.reviewed_restore_host_fixture()
        host = self.ids[0]
        patcher = mock.patch.object(MODULE.Publisher, "WINDOWS_HOST_VERSION", nine["versions"]["us-east-1/" + host] + 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
            publisher.run(event)
        [(_, copied)] = self.ec2.copies
        self.ec2.available()
        table = self.ec2.templates[host]
        version = table["default"]
        prepared = copy.deepcopy(table["versions"][version])
        user_data = base64.b64encode(b"reviewed bootstrap names the gen10 host: " + copied.encode()).decode()
        prepared.update(VersionNumber=version + 1, VersionDescription="terraform")
        prepared["LaunchTemplateData"]["UserData"] = user_data
        table["versions"][version + 1] = prepared
        table["default"] = version + 1
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)
        publisher = self.windows_publisher(nine)
        publisher.github_source_json.return_value["sha"] = self.RESTORE_SOURCE
        before = copy.deepcopy(self.fleet_ssm.values)
        with self.assertRaisesRegex(MODULE.Refused, "reviewed transition requires its unchanged predecessor"):
            publisher.run(event)
        self.assertEqual(self.fleet_ssm.values, before)
        self.assertEqual(self.ec2.selected, [])

    IPV4_SOURCE = "cad0ddad653808dcce3acc78264c7c84263fac6a"
    IPV4_IMAGE = "ami-05c06ef8ae037b10a"
    IPV4_NAME = "fcvm-fleet-host-x86_64-cad0ddad-202611131412"

    def release_through_second_apply(self, publisher_for, event, previous, source):
        """Run a build-region host release to its receipt: the copy, Terraform's second apply naming
        it in the bootstrap, and the promotion. Returns the copy and the template version before."""
        host = self.ids[0]
        with self.assertRaisesRegex(MODULE.Refused, "release-region copies are still pending: host"):
            publisher_for(previous).run(event)
        copied = self.ec2.copies[-1][1]
        self.ec2.available()
        table = self.ec2.templates[host]
        version = table["default"]
        prepared = copy.deepcopy(table["versions"][version])
        user_data = base64.b64encode(b"reviewed bootstrap names the host: " + copied.encode()).decode()
        prepared.update(VersionNumber=version + 1, VersionDescription="terraform")
        prepared["LaunchTemplateData"]["UserData"] = user_data
        table["versions"][version + 1] = prepared
        table["default"] = version + 1
        self.env["HOST_FIX_BOOT_SHA"] = MODULE.digest(user_data)
        publisher = publisher_for(previous)
        publisher.github_source_json.return_value["sha"] = source
        return publisher, copied, version

    def reviewed_ipv4_host_fixture(self):
        """Generation ten live (the 0e910245 host, released by its own action after generation nine),
        and the cad0ddad host built by hand in us-west-2 and tagged validated there by Terraform."""
        publisher, event, nine, _, _ = self.reviewed_restore_host_fixture()
        publisher, _, _ = self.release_through_second_apply(self.windows_publisher, event, nine, self.RESTORE_SOURCE)
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 10})
        ten = copy.deepcopy(self.fleet_ssm.values["/release"])
        # Generation eleven's predecessor constants, as they are filled from the generation-ten receipt.
        for name, value in (("RESTORE_HOST_COPY", ten["host_amis"]["us-east-1"]["x86_64"]),
                            ("RESTORE_HOST_VERSION", ten["versions"]["us-east-1/" + self.ids[0]])):
            patcher = mock.patch.object(MODULE.Publisher, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        catalog = (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text()
        self.assertRegex(catalog, rf'"host/x86_64/{self.IPV4_SOURCE[:8]}"\s*=\s*{{ image = "{self.IPV4_IMAGE}", kind = "host", '
                                  rf'name = "{self.IPV4_NAME}" }}')
        image = copy.deepcopy(self.west.images[self.RESTORE_IMAGE])
        image.update(ImageId=self.IPV4_IMAGE, Name=self.IPV4_NAME, State="available",
                     Description="fcvm fleet: " + self.IPV4_NAME, SourceImageId=self.RESTORE_IMAGE,
                     SourceImageRegion="us-west-2")
        self.west.images[self.IPV4_IMAGE] = image
        self.ec2.created.clear()
        self.ec2.selected.clear()
        return {"action": "activate-reviewed-ipv4-host-release"}, ten

    def test_reviewed_ipv4_host_releases_generation_eleven_after_generation_ten(self):
        event, ten = self.reviewed_ipv4_host_fixture()
        host, *tiles = self.ids
        old_templates = copy.deepcopy(self.ec2.templates)
        publisher, copied, version = self.release_through_second_apply(self.windows_publisher, event, ten, self.IPV4_SOURCE)
        self.assertEqual(self.ec2.copies[-1][0], self.IPV4_IMAGE)
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 11})
        after = self.fleet_ssm.values["/release"]
        self.assertEqual((after["generation"], after["source_sha"]), (11, self.IPV4_SOURCE))
        self.assertEqual(after["build_id"], "reviewed-ipv4-host-release:"
                         + MODULE.digest(MODULE.encoded(publisher.reviewed_ipv4_host_release())))
        self.assertEqual(after["host_amis"], {"us-east-1": {"x86_64": copied}})
        self.assertEqual(after["tile_amis"], ten["tile_amis"])
        self.assertEqual(after["previous"], {k: v for k, v in ten.items() if k != "previous"})
        self.assertEqual(after["versions"], {**ten["versions"], "us-east-1/" + host: version + 2})
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        self.assertEqual(self.fleet_ssm.values["/release-copies"]["images"]["us-west-2"]["host"], self.IPV4_IMAGE)
        # A replay is the same receipt.
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 11})
        self.assertIsNone(self.ddb.item)

    def test_reviewed_ipv4_host_names_the_generation_ten_receipt(self):
        # Filled from the live generation-ten receipt: an image id and a template version.
        self.assertRegex(MODULE.Publisher.RESTORE_HOST_COPY, r"^ami-[0-9a-f]{17}$")
        self.assertIs(type(MODULE.Publisher.RESTORE_HOST_VERSION), int)
        self.assertGreater(MODULE.Publisher.RESTORE_HOST_VERSION, 0)

    def test_reviewed_ipv4_host_refuses_a_predecessor_other_than_the_one_it_names(self):
        event, ten = self.reviewed_ipv4_host_fixture()
        patcher = mock.patch.object(MODULE.Publisher, "RESTORE_HOST_VERSION", ten["versions"]["us-east-1/" + self.ids[0]] + 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        publisher, _, _ = self.release_through_second_apply(self.windows_publisher, event, ten, self.IPV4_SOURCE)
        before = copy.deepcopy(self.fleet_ssm.values)
        with self.assertRaisesRegex(MODULE.Refused, "reviewed transition requires its unchanged predecessor"):
            publisher.run(event)
        self.assertEqual(self.fleet_ssm.values, before)
        self.assertEqual(self.ec2.selected, [])

    FACTORY_BUILD = "fcvm-fleet-image-factory:00000000-0000-4000-8000-000000000012"
    FACTORY_SOURCE = "5cb0257d07e95129ad91ab45d0f804bd669b5b58"
    FACTORY_HOST = "ami-0f00000000000012a"
    FACTORY_TILE = "ami-0f00000000000012b"

    def generation_eleven_rollback_fixture(self, *, factory_build=None):
        """Generation eleven live (the cad0ddad host), then the image factory's generation twelve: a new
        version of every template naming its host and tile, selected, and its receipt. The publisher's
        constants are filled from both receipts, as they are from the live ones."""
        event, ten = self.reviewed_ipv4_host_fixture()
        publisher, _, _ = self.release_through_second_apply(self.windows_publisher, event, ten, self.IPV4_SOURCE)
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 11})
        eleven = copy.deepcopy(self.fleet_ssm.values["/release"])
        host = self.ids[0]
        versions = {}
        for template in self.ids:
            table = self.ec2.templates[template]
            number = table["default"] + 1
            factory = copy.deepcopy(table["versions"][table["default"]])
            factory.update(VersionNumber=number, VersionDescription="fleet-image-factory")
            factory["LaunchTemplateData"]["ImageId"] = self.FACTORY_HOST if template == host else self.FACTORY_TILE
            table["versions"][number] = factory
            table["default"] = number
            versions["us-east-1/" + template] = number
        twelve = copy.deepcopy(eleven)
        twelve.update(generation=12, source_sha=self.FACTORY_SOURCE, build_id=self.FACTORY_BUILD, versions=versions,
                      previous={k: v for k, v in eleven.items() if k != "previous"},
                      host_amis={"us-east-1": {"x86_64": self.FACTORY_HOST}},
                      tile_amis={"us-east-1": {"x86_64": self.FACTORY_TILE}})
        self.fleet_ssm.values["/release"] = copy.deepcopy(twelve)
        for name, value in (("IPV4_HOST_COPY", eleven["host_amis"]["us-east-1"]["x86_64"]),
                            ("IPV4_HOST_VERSION", eleven["versions"]["us-east-1/" + host]),
                            ("FACTORY_TWELVE_BUILD", factory_build or self.FACTORY_BUILD),
                            ("FACTORY_TWELVE_SOURCE", self.FACTORY_SOURCE),
                            ("FACTORY_TWELVE_HOST", self.FACTORY_HOST),
                            ("FACTORY_TWELVE_TILE", self.FACTORY_TILE)):
            patcher = mock.patch.object(MODULE.Publisher, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ec2.created.clear()
        self.ec2.selected.clear()
        return {"action": "activate-reviewed-generation-eleven-rollback"}, eleven, twelve

    def test_reviewed_rollback_puts_generation_eleven_back_after_the_factory_generation_twelve(self):
        event, eleven, twelve = self.generation_eleven_rollback_fixture()
        before = copy.deepcopy(self.ec2.templates)
        publisher = self.publisher()
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 13})
        after = self.fleet_ssm.values["/release"]
        self.assertEqual((after["generation"], after["source_sha"]), (13, self.IPV4_SOURCE))
        self.assertEqual(after["build_id"], "reviewed-generation-eleven-rollback:"
                         + MODULE.digest(MODULE.encoded(publisher.reviewed_generation_eleven_rollback())))
        self.assertEqual(after["host_amis"], eleven["host_amis"])
        self.assertEqual(after["tile_amis"], eleven["tile_amis"])
        self.assertEqual(after["previous"], {k: v for k, v in twelve.items() if k != "previous"})
        for template in self.ids:
            table = self.ec2.templates[template]
            old = before[template]["versions"][before[template]["default"]]
            new = table["versions"][table["default"]]
            # A new version of the factory's: generation eleven's image and nothing else changed.
            self.assertEqual(table["default"], before[template]["default"] + 1)
            self.assertEqual(after["versions"]["us-east-1/" + template], table["default"])
            kind = "host" if template == self.ids[0] else "tile"
            self.assertEqual(new["LaunchTemplateData"]["ImageId"], eleven[kind + "_amis"]["us-east-1"]["x86_64"])
            self.assertEqual(MODULE.without_image(new["LaunchTemplateData"]), MODULE.without_image(old["LaunchTemplateData"]))
        # A replay is the same receipt.
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 13})
        self.assertIsNone(self.ddb.item)

    def test_reviewed_rollback_refuses_a_predecessor_other_than_the_factory_receipt_it_names(self):
        event, _, _ = self.generation_eleven_rollback_fixture(
            factory_build="fcvm-fleet-image-factory:00000000-0000-4000-8000-000000000099")
        before = copy.deepcopy(self.fleet_ssm.values)
        with self.assertRaisesRegex(MODULE.Refused, "reviewed transition requires its unchanged predecessor"):
            self.publisher().run(event)
        self.assertEqual(self.fleet_ssm.values, before)
        self.assertEqual(self.ec2.selected, [])

    def test_reviewed_rollback_names_the_live_receipts(self):
        self.assertRegex(MODULE.Publisher.IPV4_HOST_COPY, r"^ami-[0-9a-f]{17}$")
        self.assertRegex(MODULE.Publisher.FACTORY_TWELVE_HOST, r"^ami-[0-9a-f]{17}$")
        self.assertRegex(MODULE.Publisher.FACTORY_TWELVE_TILE, r"^ami-[0-9a-f]{17}$")
        self.assertRegex(MODULE.Publisher.FACTORY_TWELVE_SOURCE, r"^[0-9a-f]{40}$")
        self.assertRegex(MODULE.Publisher.FACTORY_TWELVE_BUILD, r"^fcvm-fleet-image-factory:[0-9a-f-]{36}$")
        self.assertIs(type(MODULE.Publisher.IPV4_HOST_VERSION), int)

    ENA_SOURCE = "c12ad070012a74308ba37236127d6dbd5316ceda"
    ENA_IMAGE = "ami-0c2656522631215d1"
    ENA_NAME = "fcvm-fleet-host-x86_64-c12ad070-202611141114"

    def reviewed_ena_host_fixture(self):
        """Generation thirteen live (the rollback to generation eleven's images after the factory's
        generation twelve), and the c12ad070 host built by hand in us-west-2 and tagged validated there."""
        event, _, _ = self.generation_eleven_rollback_fixture()
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 13})
        thirteen = copy.deepcopy(self.fleet_ssm.values["/release"])
        catalog = (SOURCE.parents[1] / "fcvm-fleet-ami-release.tf").read_text()
        self.assertRegex(catalog, rf'"host/x86_64/{self.ENA_SOURCE[:8]}"\s*=\s*{{ image = "{self.ENA_IMAGE}", kind = "host", '
                                  rf'name = "{self.ENA_NAME}" }}')
        image = copy.deepcopy(self.west.images[self.IPV4_IMAGE])
        image.update(ImageId=self.ENA_IMAGE, Name=self.ENA_NAME, State="available",
                     Description="fcvm fleet: " + self.ENA_NAME, SourceImageId=self.IPV4_IMAGE,
                     SourceImageRegion="us-west-2")
        self.west.images[self.ENA_IMAGE] = image
        self.ec2.created.clear()
        self.ec2.selected.clear()
        return {"action": "activate-reviewed-ena-host-release"}, thirteen

    def test_reviewed_ena_host_releases_generation_fourteen_after_the_rollback(self):
        event, thirteen = self.reviewed_ena_host_fixture()
        host, *tiles = self.ids
        old_templates = copy.deepcopy(self.ec2.templates)
        publisher, copied, version = self.release_through_second_apply(self.windows_publisher, event, thirteen, self.ENA_SOURCE)
        self.assertEqual(self.ec2.copies[-1][0], self.ENA_IMAGE)
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 14})
        after = self.fleet_ssm.values["/release"]
        self.assertEqual((after["generation"], after["source_sha"]), (14, self.ENA_SOURCE))
        self.assertEqual(after["build_id"], "reviewed-ena-host-release:"
                         + MODULE.digest(MODULE.encoded(publisher.reviewed_ena_host_release())))
        self.assertEqual(after["host_amis"], {"us-east-1": {"x86_64": copied}})
        self.assertEqual(after["tile_amis"], thirteen["tile_amis"])
        self.assertEqual(after["previous"], {k: v for k, v in thirteen.items() if k != "previous"})
        self.assertEqual(after["versions"], {**thirteen["versions"], "us-east-1/" + host: version + 2})
        for tile in tiles:
            self.assertEqual(self.ec2.templates[tile], old_templates[tile])
        # A replay is the same receipt.
        self.assertEqual(self.publisher().run(event), {"status": "promoted", "generation": 14})
        self.assertIsNone(self.ddb.item)

    def test_reviewed_ena_host_refuses_a_live_receipt_other_than_the_rollback(self):
        # The live receipt is still the factory's generation twelve: the rollback has not run.
        event, _, _ = self.generation_eleven_rollback_fixture()
        image = copy.deepcopy(self.west.images[self.IPV4_IMAGE])
        image.update(ImageId=self.ENA_IMAGE, Name=self.ENA_NAME, State="available",
                     Description="fcvm fleet: " + self.ENA_NAME, SourceImageId=self.IPV4_IMAGE,
                     SourceImageRegion="us-west-2")
        self.west.images[self.ENA_IMAGE] = image
        twelve = copy.deepcopy(self.fleet_ssm.values["/release"])
        ena = {"action": "activate-reviewed-ena-host-release"}
        publisher, _, _ = self.release_through_second_apply(self.windows_publisher, ena, twelve, self.ENA_SOURCE)
        before = copy.deepcopy(self.fleet_ssm.values)
        with self.assertRaisesRegex(MODULE.Refused, "reviewed transition requires its unchanged predecessor"):
            publisher.run(ena)
        self.assertEqual(self.fleet_ssm.values, before)
        self.assertEqual(self.ec2.selected, [])

    def generation_eleven_live(self):
        """Generation eleven live (the cad0ddad host, released after generation ten): a receipt whose
        previous names generation ten's pair."""
        event, ten = self.reviewed_ipv4_host_fixture()
        publisher, _, _ = self.release_through_second_apply(self.windows_publisher, event, ten, self.IPV4_SOURCE)
        self.assertEqual(publisher.run(event), {"status": "promoted", "generation": 11})
        self.ec2.created.clear()
        self.ec2.selected.clear()
        return ten, copy.deepcopy(self.fleet_ssm.values["/release"])

    def test_the_gate_rolls_a_failed_generation_back_to_its_previous(self):
        ten, eleven = self.generation_eleven_live()
        host, *tiles = self.ids
        before = copy.deepcopy(self.ec2.templates)
        self.assertEqual(self.publisher().gate({"action": "status"}),
                         {"generation": 11, "source_sha": self.IPV4_SOURCE, "verified": False, "rollback": False})
        self.assertEqual(self.publisher().gate({"action": "rollback", "generation": 11}),
                         {"status": "rolled_back", "generation": 12})
        after = self.fleet_ssm.values["/release"]
        self.assertEqual((after["generation"], after["source_sha"]), (12, ten["source_sha"]))
        self.assertTrue(after["build_id"].startswith("rollback-to-previous:"))
        self.assertEqual((after["host_amis"], after["tile_amis"]), (ten["host_amis"], ten["tile_amis"]))
        self.assertEqual(after["previous"], {k: v for k, v in eleven.items() if k != "previous"})
        table = self.ec2.templates[host]
        self.assertEqual(table["versions"][table["default"]]["LaunchTemplateData"]["ImageId"],
                         ten["host_amis"]["us-east-1"]["x86_64"])
        # Only the image changed: the bootstrap and everything else is the live default's.
        self.assertEqual(MODULE.without_image(table["versions"][table["default"]]["LaunchTemplateData"]),
                         MODULE.without_image(before[host]["versions"][before[host]["default"]]["LaunchTemplateData"]))
        # The rollback is recorded as verified, so the next smoke cannot roll it back.
        self.assertEqual(self.publisher().gate({"action": "status"})["verified"], True)
        # A replay of the same request is the same receipt.
        created = list(self.ec2.created)
        self.assertEqual(self.publisher().gate({"action": "rollback", "generation": 11}),
                         {"status": "rolled_back", "generation": 12})
        self.assertEqual(self.ec2.created, created)
        with self.assertRaisesRegex(MODULE.Refused, "the live release is a rollback"):
            self.publisher().run({"action": "rollback-to-previous"})
        self.assertIsNone(self.ddb.item)

    def test_the_gate_refuses_a_rollback_with_no_previous_release(self):
        _, eleven = self.generation_eleven_live()
        orphan = dict(eleven, previous=None)
        self.fleet_ssm.values["/release"] = copy.deepcopy(orphan)
        with self.assertRaisesRegex(MODULE.Refused, "no previous release to roll back to"):
            self.publisher().gate({"action": "rollback", "generation": 11})
        self.assertEqual(self.fleet_ssm.values["/release"], orphan)
        self.assertEqual(self.ec2.selected, [])

    def test_the_gate_refuses_a_rollback_whose_previous_image_is_gone(self):
        ten, _ = self.generation_eleven_live()
        del self.ec2.images[ten["host_amis"]["us-east-1"]["x86_64"]]
        before = copy.deepcopy(self.fleet_ssm.values)
        # EC2 answers DescribeImages for a deregistered id with an error (the fake raises KeyError);
        # either way the gate stops before it records a transaction or touches a template.
        with self.assertRaises(Exception):
            self.publisher().gate({"action": "rollback", "generation": 11})
        self.assertEqual(self.fleet_ssm.values, before)
        self.assertEqual(self.ec2.selected, [])

    def test_the_gate_does_not_roll_back_a_verified_generation(self):
        _, eleven = self.generation_eleven_live()
        self.assertEqual(self.publisher().gate({"action": "verify", "generation": 11}), {"status": "verified", "generation": 11})
        with self.assertRaisesRegex(MODULE.Refused, "the gate verified this generation"):
            self.publisher().gate({"action": "rollback", "generation": 11})
        with self.assertRaisesRegex(MODULE.Refused, "the live release is not that generation"):
            self.publisher().gate({"action": "rollback", "generation": 10})
        self.assertEqual(self.fleet_ssm.values["/release"], eleven)

    def test_the_gate_handler_takes_only_its_three_requests(self):
        for event in ({"action": "rollback"}, {"action": "rollback", "generation": "11"},
                      {"action": "activate-reviewed-seed"}, {"action": "status", "generation": 1}, []):
            with mock.patch.object(MODULE.Publisher, "__init__", lambda this, *a, **k: None):
                self.assertEqual(MODULE.gate_handler(event, None)["status"], "refused", event)

    def test_the_release_gate_is_its_own_function_that_staging_ci_and_dev_servers_may_invoke(self):
        root = SOURCE.parents[1]
        pipeline = (root / "fcvm-fleet-ami-pipeline.tf").read_text()
        gate = re.search(r'resource "aws_lambda_function" "fcvm_ami_release_gate" \{\n(.*?)\n\}', pipeline, re.S).group(1)
        self.assertIn('handler                        = "publish.gate_handler"', gate)
        self.assertIn("role                           = aws_iam_role.fcvm_ami_publish.arn", gate)
        self.assertIn("variables = aws_lambda_function.fcvm_ami_publish.environment[0].variables", gate)
        self.assertIn("reserved_concurrent_executions = 1", gate)
        dev = re.search(r'resource "aws_iam_role_policy" "dev_server_fcvm_ami_release_gate" \{\n(.*?)\n\}', pipeline, re.S).group(1)
        self.assertIn("role = aws_iam_role.dev_server.id", dev)
        self.assertEqual(re.findall(r'Resource = ([^ }]+)', dev), ["aws_lambda_function.fcvm_ami_release_gate.arn"])
        ci = (root / "fcvm-fleet-ci.tf").read_text()
        stage = re.search(r'resource "aws_iam_role_policy" "fcvm_stage_ci" \{\n(.*?)\n\}', ci, re.S).group(1)
        self.assertEqual(re.findall(r'Action = "lambda:InvokeFunction", Resource = ([^ }]+)', stage),
                         ["aws_lambda_function.fcvm_coordinator_release.arn", "aws_lambda_function.fcvm_ami_release_gate.arn"])
        self.assertIn("RELEASE_GATE        = aws_lambda_function.fcvm_ami_release_gate.function_name", ci)
        # The gate's handler reaches only Publisher.gate, never Publisher.run.
        handler = inspect.getsource(MODULE.gate_handler)
        self.assertIn("Publisher().gate(event)", handler)
        self.assertNotIn(".run(", handler)

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

    def test_a_host_without_the_viewer_path_proof_is_not_published(self):
        # A host image whose tiles did not answer a viewer over both families (ejc3/fcvm
        # viewer-acceptance.sh) never reaches the launch templates, and neither does one built
        # before the factory ran that proof.
        for value in (False, None):
            with self.subTest(viewer_path=value):
                self.setUp()
                validation = self.manifest["images"]["host"]["validation"]
                if value is None:
                    del validation["viewer_path"]
                else:
                    validation["viewer_path"] = value
                self.update_manifest()
                with self.assertRaises(MODULE.Refused):
                    self.publisher().run(self.event)
                self.assertEqual(self.ec2.created, [])

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

    def test_expired_early_builder_metadata_does_not_block_completed_publication(self):
        for all_expired in (False, True):
            with self.subTest(all_expired=all_expired):
                self.setUp()
                self.codebuild.value["endTime"] = self.codebuild.value["startTime"] + datetime.timedelta(hours=3)
                journal = self.main_ssm.values["/factory/builds/" + MODULE.digest(self.build)]
                for slot, entry in journal["slots"].items():
                    if all_expired or slot.endswith("-build"):
                        del self.ec2.instances[entry["instance_id"]]
                try:
                    result = self.publisher().run(self.event)
                except Exception as error:
                    self.fail("expired metadata blocked a validated pair: " + type(error).__name__)
                self.assertEqual(result, {"status": "promoted", "generation": 1})
                self.assertEqual((len(self.ec2.created), len(self.assets.copies)), (4, 19))

    def test_missing_metadata_requires_strict_exact_slot_receipts_before_writes(self):
        for variant in ("missing", "false", "string", "attempted-false", "attempted-string", "invalid-id", "duplicate-id", "no-instance", "rejected"):
            with self.subTest(variant=variant):
                self.setUp()
                journal = self.main_ssm.values["/factory/builds/" + MODULE.digest(self.build)]
                entry = journal["slots"]["host-build"]
                del self.ec2.instances[entry["instance_id"]]
                if variant == "missing": entry.pop("terminated_seen")
                elif variant == "false": entry["terminated_seen"] = False
                elif variant == "string": entry["terminated_seen"] = "true"
                elif variant == "attempted-false": entry["attempted"] = False
                elif variant == "attempted-string": entry["attempted"] = "true"
                elif variant == "invalid-id": entry["instance_id"] = "not-an-instance"
                elif variant == "duplicate-id": entry["instance_id"] = journal["slots"]["tile-build"]["instance_id"]
                elif variant == "no-instance": entry["no_instance"] = True
                else: entry["rejected"] = True
                with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
                self.assertEqual((self.ec2.created, self.assets.copies), ([], []))

    def test_expired_builder_cannot_hide_a_returned_live_or_foreign_sibling(self):
        for variant in ("running", "pending", "stopped", "shutting-down", "wrong-type", "wrong-profile", "empty-profile",
                        "Purpose", "FactoryBuild", "FactorySlot", "wrong-id", "wrong-owner"):
            with self.subTest(variant=variant):
                self.setUp()
                journal = self.main_ssm.values["/factory/builds/" + MODULE.digest(self.build)]
                del self.ec2.instances[journal["slots"]["host-build"]["instance_id"]]
                original = self.ec2.describe_instances
                def contradict(**arguments):
                    reply = original(**arguments)
                    reservation = reply["Reservations"][0]
                    row = reservation["Instances"][0]
                    if variant in ("running", "pending", "stopped", "shutting-down"): row["State"]["Name"] = variant
                    elif variant == "wrong-type": row["InstanceType"] = "foreign"
                    elif variant == "wrong-profile": row["IamInstanceProfile"] = {"Arn": "foreign"}
                    elif variant == "empty-profile": row["IamInstanceProfile"] = {}
                    elif variant == "wrong-id": row["InstanceId"] = "i-" + "f" * 17
                    elif variant == "wrong-owner": reservation["OwnerId"] = "foreign"
                    else: next(t for t in row["Tags"] if t["Key"] == variant)["Value"] = "foreign"
                    return reply
                with mock.patch.object(self.ec2, "describe_instances", side_effect=contradict):
                    with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
                self.assertEqual((self.ec2.created, self.assets.copies), ([], []))

    def test_known_terminated_profileless_rows_still_publish(self):
        for instance in self.ec2.instances.values(): instance.pop("IamInstanceProfile")
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})

    def test_receipts_do_not_swallow_ec2_errors(self):
        with mock.patch.object(self.ec2, "describe_instances", side_effect=RuntimeError("AccessDenied")):
            with self.assertRaisesRegex(RuntimeError, "AccessDenied"): self.publisher().run(self.event)
        self.assertEqual((self.ec2.created, self.assets.copies), ([], []))

    def test_publisher_uses_the_managed_builder_profile(self):
        text = (SOURCE.parents[1] / "fcvm-fleet-ami-pipeline.tf").read_text()
        block = text.split('resource "aws_lambda_function" "fcvm_ami_publish" {', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('BUILDER_PROFILE   = aws_iam_instance_profile.fcvm_ami_builder.arn', block)

    def test_success_after_240_minutes_and_at_exactly_360_can_publish(self):
        for seconds in (9001, 14401, 21600):
            with self.subTest(seconds=seconds):
                self.setUp()
                self.codebuild.value["endTime"] = self.codebuild.value["startTime"] + datetime.timedelta(seconds=seconds)
                try:
                    result = self.publisher().run(self.event)
                except MODULE.Refused as error:
                    self.fail("the approved 360-minute build was refused: " + str(error))
                self.assertEqual(result, {"status": "promoted", "generation": 1})
                self.assertEqual(len(self.ec2.created), 4)

    def set_execution_receipt(self, timeout_minutes, elapsed_seconds, journal_seconds):
        build = self.codebuild.value
        if timeout_minutes is None:
            del build["timeoutInMinutes"]
        else:
            build["timeoutInMinutes"] = timeout_minutes
        build["endTime"] = build["startTime"] + datetime.timedelta(seconds=elapsed_seconds)
        journal = self.main_ssm.values["/factory/builds/" + MODULE.digest(self.build)]
        journal["deadline_unix"] = int(build["startTime"].timestamp()) + journal_seconds
        self.manifest["deadline_unix"] = journal["deadline_unix"]
        self.update_manifest()

    def test_legacy_execution_receipt_and_proof_still_publish(self):
        self.set_execution_receipt(240, 14400, 14400)
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})
        self.assertEqual(len(self.ec2.created), 4)

    def test_overlong_or_mismatched_execution_lease_cannot_change_templates(self):
        for timeout_minutes, elapsed_seconds, journal_seconds in (
            (240, 14401, 14400), (240, 1000, 21600), (360, 21601, 21600),
            (360, 1000, 14400), (360, 1000, 21601),
            (None, 1000, 21600), (300, 1000, 18000), ("360", 1000, 21600),
        ):
            with self.subTest(timeout=timeout_minutes, elapsed=elapsed_seconds, journal=journal_seconds):
                self.setUp()
                self.set_execution_receipt(timeout_minutes, elapsed_seconds, journal_seconds)
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

    def copies(self):
        return {source: copied for source, copied in self.west.copies}

    def test_published_pair_is_copied_to_each_copy_region_and_recorded(self):
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})
        copies = self.copies()
        self.assertEqual(set(copies), set(self.new.values()))
        self.assertEqual(self.fleet_ssm.values["/release-copies"], {"version": 1, "generation": 1, "build_id": self.build,
            "source_sha": self.source, "images": {"us-east-1": self.new,
                "us-west-2": {kind: copies[image] for kind, image in self.new.items()}}, "previous_images": {}})
        for source, copied in copies.items():
            self.assertEqual(self.west.images[copied]["Name"], self.ec2.images[source]["Name"])
        self.assertEqual(self.fleet_ssm.values["/release"]["host_amis"], {"us-east-1": {"x86_64": self.new["host"]}},
                         "a copy region must never gain launch templates")
        self.publisher().run(self.event)
        self.assertEqual(len(self.west.copies), 2, "a replay must find its copies, not start new ones")

    def test_reviewed_seed_is_copied_too(self):
        self.assertEqual(self.seed_success(self.publisher(), self.legacy_seed()), {"status": "promoted", "generation": 1})
        self.assertEqual(set(self.copies()), set(self.new.values()))
        self.assertEqual(self.fleet_ssm.values["/release-copies"]["images"]["us-east-1"], self.new)

    def test_copy_failure_keeps_the_release_and_the_next_run_completes_it(self):
        self.west.fail_copy = True
        with self.assertRaises(RuntimeError): self.publisher().run(self.event)
        self.assertEqual(self.fleet_ssm.values["/release"]["generation"], 1)
        # Not promoted until the copies are recorded, so the dispatcher keeps redriving it.
        self.assertNotEqual(self.main_ssm.values["/factory/requests/" + self.input]["status"], "promoted")
        self.assertNotIn("/release-copies", self.fleet_ssm.values)
        self.west.fail_copy = False
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})
        self.assertEqual(set(self.copies()), set(self.new.values()))
        self.assertEqual(len(self.ec2.created), 4, "completing the copy must never touch the templates")
        self.assertIsNone(self.ddb.item)

    def active_new_pair(self):
        release = self.fleet_ssm.values["/release"]
        release.update(generation=6, build_id="factory:example", source_sha=self.source)
        for kind in ("host", "tile"):
            release[kind + "_amis"] = {"us-east-1": {"x86_64": self.new[kind]}}
            self.ec2.images[self.new[kind]]["Tags"].append({"Key": "FleetImageValidated", "Value": "true"})

    def test_replicate_action_copies_the_active_release_and_takes_no_configuration(self):
        self.active_new_pair()
        with self.assertRaises(MODULE.Refused):
            self.publisher().run({"action": "replicate-active-release", "images": self.old})
        self.assertEqual(self.west.copies, [])
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        self.assertEqual(self.publisher().run({"action": "replicate-active-release"}), {"status": "replicated", "generation": 6})
        self.assertEqual(set(self.copies()), set(self.new.values()))
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual((self.ec2.created, self.ec2.tagged), ([], []))

    def test_pending_or_failed_copies_preserve_the_previous_available_record(self):
        self.active_new_pair()
        previous = {"version": 1, "generation": 5, "images": {"us-west-2": self.old}}
        self.fleet_ssm.values["/release-copies"] = copy.deepcopy(previous)
        self.west.copy_state = "pending"
        event = {"action": "replicate-active-release"}
        for _ in range(2):
            with self.assertRaisesRegex(MODULE.Refused, "regional AMI copies are still pending"):
                self.publisher().run(event)
            self.assertEqual(self.fleet_ssm.values["/release-copies"], previous)
            self.assertEqual(len(self.west.copies), 2)
        host = self.west.images[self.copies()[self.new["host"]]]
        host["State"] = "failed"
        with self.assertRaisesRegex(MODULE.Refused, "regional AMI copies are still pending"):
            self.publisher().run(event)
        self.assertEqual(self.fleet_ssm.values["/release-copies"], previous)
        retry = self.copies()[self.new["host"]]
        self.assertEqual(self.west.images[retry]["Name"], host["Name"] + "-copy-2")
        self.assertEqual(self.west.deregistered, [])
        with self.assertRaisesRegex(MODULE.Refused, "regional AMI copies are still pending"):
            self.publisher().run(event)
        self.west.available()
        self.assertEqual(self.publisher().run(event), {"status": "replicated", "generation": 6})
        self.assertEqual(len(self.west.copies), 3)
        self.assertEqual(self.fleet_ssm.values["/release-copies"]["images"]["us-west-2"]["host"], retry)

    def test_foreign_failed_copies_are_not_replaced_and_own_retries_are_bounded(self):
        self.active_new_pair()
        self.west.copy_state = "failed"
        event = {"action": "replicate-active-release"}
        with self.assertRaisesRegex(MODULE.Refused, "still pending"):
            self.publisher().run(event)
        image = self.west.images[self.copies()[self.new["host"]]]
        source = image["SourceImageId"]
        image["SourceImageId"] = "ami-" + "f" * 17
        with self.assertRaisesRegex(MODULE.Refused, "lineage"):
            self.publisher().run(event)
        self.assertEqual(len(self.west.copies), 2)
        image["SourceImageId"] = source
        for _ in range(2):
            with self.assertRaisesRegex(MODULE.Refused, "still pending"):
                self.publisher().run(event)
        with self.assertRaisesRegex(MODULE.Refused, "attempts exhausted"):
            self.publisher().run(event)
        self.assertEqual(len(self.west.copies), 6)
        self.assertEqual(self.west.deregistered, [])
        self.assertNotIn("/release-copies", self.fleet_ssm.values)

    def test_unvalidated_public_or_misnamed_image_is_never_copied(self):
        for variant in ("FleetImageValidated", "Purpose", "Public", "OwnerId", "Name"):
            with self.subTest(variant=variant):
                self.setUp()
                self.active_new_pair()
                image = self.ec2.images[self.new["tile"]]
                if variant == "Public": image["Public"] = True
                elif variant == "OwnerId": image["OwnerId"] = "333333333333"
                elif variant == "Name": image["Name"] = "something-else"
                else: image["Tags"] = [tag for tag in image["Tags"] if tag["Key"] != variant]
                with self.assertRaises(MODULE.Refused): self.publisher().run({"action": "replicate-active-release"})
                self.assertEqual(self.west.copies, [])
                self.assertNotIn("/release-copies", self.fleet_ssm.values)

    def test_an_existing_copy_is_reused_and_a_foreign_image_holding_the_name_refuses(self):
        for foreign in (False, True, "source"):
            with self.subTest(foreign=foreign):
                self.setUp()
                self.active_new_pair()
                source = self.ec2.images[self.new["host"]]
                tags = copy.deepcopy(source["Tags"])
                if foreign is True:
                    tags = [tag for tag in tags if tag["Key"] != "FleetImageKind"] + [{"Key": "FleetImageKind", "Value": "tile"}]
                self.west.images["ami-" + "9" * 17] = {"ImageId": "ami-" + "9" * 17, "Name": source["Name"],
                    "OwnerId": "222222222222", "Public": False, "State": "available", "Architecture": "x86_64",
                    "RootDeviceType": "ebs", "Tags": tags,
                    "SourceImageId": self.new["host"] if foreign != "source" else "ami-" + "7" * 17, "SourceImageRegion": "us-east-1"}
                if foreign:
                    with self.assertRaises(MODULE.Refused): self.publisher().run({"action": "replicate-active-release"})
                    self.assertNotIn("/release-copies", self.fleet_ssm.values)
                    continue
                self.publisher().run({"action": "replicate-active-release"})
                self.assertEqual([item for item, _ in self.west.copies], [self.new["tile"]])
                self.assertEqual(self.fleet_ssm.values["/release-copies"]["images"]["us-west-2"]["host"], "ami-" + "9" * 17)

    def test_copy_regions_are_fixed_and_never_the_compute_region(self):
        for regions in ("us-east-1", "eu-west-1", "us-west-2,us-west-2", "us-west-2,", "[\"us-west-2\"]"):
            with self.subTest(regions=regions):
                self.env["COPY_REGIONS"] = regions
                with self.assertRaises(MODULE.Refused): MODULE.Publisher(self.env, {})

    def build_in_west(self):
        """The factory builds in us-west-2: builders, candidates and their snapshots live there."""
        self.env["BUILD_REGION"] = "us-west-2"
        for name in ("instances", "snapshots"):
            getattr(self.west, name).update(getattr(self.ec2, name))
            getattr(self.ec2, name).clear()
        for kind, image in self.new.items():
            self.west.images[image] = self.ec2.images.pop(image)
            # The name dispatch.py gives a sealed candidate.
            self.west.images[image]["Name"] = "fcvm-factory-" + MODULE.digest(self.build)[:24] + "-" + kind

    def test_a_pair_built_in_us_west_2_is_released_from_its_us_east_1_copies(self):
        self.build_in_west()
        with self.assertRaises(MODULE.Refused) as pending:
            self.publisher().run(self.event)
        self.assertIn("pending", str(pending.exception))
        self.assertEqual((self.ec2.created, self.fleet_ssm.values["/release"]["generation"]), ([], 0))
        self.assertIsNone(self.ddb.item)
        copies = {source: copied for source, copied in self.ec2.copies}
        self.assertEqual(set(copies), set(self.new.values()))
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertEqual(len(self.ec2.copies), 2, "a redrive must find its copies, not start new ones")
        self.ec2.available()
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})
        released = {kind: copies[image] for kind, image in self.new.items()}
        release = self.fleet_ssm.values["/release"]
        self.assertEqual((release["host_amis"], release["tile_amis"]),
                         ({"us-east-1": {"x86_64": released["host"]}}, {"us-east-1": {"x86_64": released["tile"]}}))
        for item in self.templates:
            template = self.ec2.templates[item["id"]]
            self.assertEqual(template["versions"][template["default"]]["LaunchTemplateData"]["ImageId"], released[item["kind"]])
        for fake, image in [(self.ec2, image) for image in released.values()] + [(self.west, image) for image in self.new.values()]:
            tags = {tag["Key"]: tag["Value"] for tag in fake.images[image]["Tags"]}
            self.assertEqual(tags.get("FleetImageValidated"), "true", (fake.region, image))
        self.assertEqual(self.fleet_ssm.values["/release-copies"]["images"], {"us-east-1": released, "us-west-2": self.new})
        self.assertEqual(self.west.copies, [], "the originals are the us-west-2 images")
        self.publisher().run(self.event)
        self.assertEqual((len(self.ec2.copies), len(self.ec2.created)), (2, 4))

    def test_an_image_holding_a_candidate_name_in_the_release_region_refuses(self):
        for field in ("Description", "PipelineBuildId", "FleetImageKind", "SourceImageId", "SourceImageRegion"):
            with self.subTest(field=field):
                self.setUp()
                self.build_in_west()
                original = self.west.images[self.new["host"]]
                tags = copy.deepcopy(original["Tags"])
                image = {"ImageId": "ami-" + "8" * 17, "Name": original["Name"], "OwnerId": "222222222222", "Public": False,
                         "State": "available", "Architecture": "x86_64", "RootDeviceType": "ebs",
                         "Description": "copy of " + self.new["host"] + " from us-west-2", "Tags": tags,
                         "SourceImageId": self.new["host"], "SourceImageRegion": "us-west-2"}
                if field == "Description": image["Description"] = "something else"
                elif field == "SourceImageId": image["SourceImageId"] = "ami-" + "9" * 17
                elif field == "SourceImageRegion": image["SourceImageRegion"] = "us-east-2"
                else: next(tag for tag in tags if tag["Key"] == field)["Value"] = "foreign"
                self.ec2.images[image["ImageId"]] = image
                with self.assertRaises(MODULE.Refused) as refused: self.publisher().run(self.event)
                self.assertIn("lineage", str(refused.exception))
                self.assertEqual((self.ec2.created, self.fleet_ssm.values["/release"]["generation"]), ([], 0))

    def test_retried_east_release_copy_keeps_the_canonical_west_original(self):
        self.build_in_west()
        with self.assertRaisesRegex(MODULE.Refused, "still pending"):
            self.publisher().run(self.event)
        first = dict(self.ec2.copies)[self.new["host"]]
        self.ec2.images[first]["State"] = "failed"
        with self.assertRaisesRegex(MODULE.Refused, "still pending"):
            self.publisher().run(self.event)
        retry = dict(self.ec2.copies)[self.new["host"]]
        self.assertEqual(self.ec2.images[retry]["Name"], self.west.images[self.new["host"]]["Name"] + "-copy-2")
        self.ec2.available()
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})
        record = self.fleet_ssm.values["/release-copies"]["images"]
        self.assertEqual(record["us-east-1"]["host"], retry)
        self.assertEqual(record["us-west-2"], self.new)
        self.assertEqual((self.west.copies, self.ec2.deregistered), ([], []))
        self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual(len(self.ec2.copies), 3)

    def test_builders_are_proven_terminated_in_the_build_region(self):
        # A builder still running in us-west-2 contradicts its recorded termination.
        self.build_in_west()
        next(iter(self.west.instances.values()))["State"]["Name"] = "running"
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.assertEqual((self.ec2.copies, self.ec2.created), ([], []))

    def publisher_copy(self, fake, image_id, source, source_region, kind="host", snapshot=None, description=None, created=None):
        """An image the publisher made with CopyImage, in `fake`'s region."""
        if snapshot:
            fake.snapshots[snapshot] = {"SnapshotId": snapshot, "OwnerId": "222222222222",
                "Tags": [{"Key": "FleetPublisherCopy", "Value": "true"}]}
        fake.images[image_id] = {"ImageId": image_id, "Name": "fcvm-factory-" + image_id[4:16] * 2 + "-" + kind,
            "Description": description or "copy of " + source + " from " + source_region, "OwnerId": "222222222222",
            "Public": False, "State": "available", "Architecture": "x86_64", "RootDeviceType": "ebs",
            "SourceImageId": source, "SourceImageRegion": source_region, "CreationDate": created or "2026-10-01T00:00:00.000Z",
            "Tags": [{"Key": "Purpose", "Value": "fcvm-fleet"}, {"Key": "FleetImageKind", "Value": kind}],
            "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot}}] if snapshot else []}

    def test_retention_removes_only_copies_nothing_needs(self):
        self.build_in_west()
        stale, in_use, previous = "ami-" + "e1" * 8 + "e", "ami-" + "e2" * 8 + "e", "ami-" + "e3" * 8 + "e"
        handmade, stale_west = "ami-" + "e4" * 8 + "e", "ami-" + "e5" * 8 + "e"
        self.publisher_copy(self.ec2, stale, "ami-" + "f1" * 8 + "f", "us-west-2", snapshot="snap-" + "e1" * 8 + "e")
        self.publisher_copy(self.ec2, in_use, "ami-" + "f2" * 8 + "f", "us-west-2")
        self.publisher_copy(self.ec2, previous, "ami-" + "f3" * 8 + "f", "us-west-2")
        self.publisher_copy(self.ec2, handmade, "ami-" + "f4" * 8 + "f", "us-west-2", description="built by hand")
        self.publisher_copy(self.west, stale_west, "ami-" + "f5" * 8 + "f", "us-east-1", kind="tile", snapshot="snap-" + "e5" * 8 + "e")
        self.ec2.instances["i-" + "e" * 17] = {"InstanceId": "i-" + "e" * 17, "ImageId": in_use, "State": {"Name": "stopped"}, "Tags": []}
        # The record of the release before this one names `previous` in us-east-1.
        self.fleet_ssm.values["/release-copies"] = {"version": 1, "generation": 0, "build_id": "older", "source_sha": "",
            "images": {"us-east-1": {"host": previous}}, "previous_images": {}}
        with self.assertRaises(MODULE.Refused): self.publisher().run(self.event)
        self.ec2.available()
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})
        self.assertEqual((self.ec2.deregistered, self.ec2.deleted_snapshots), ([stale], ["snap-" + "e1" * 8 + "e"]))
        self.assertEqual((self.west.deregistered, self.west.deleted_snapshots), ([stale_west], ["snap-" + "e5" * 8 + "e"]))
        for image in (in_use, previous, handmade):
            self.assertIn(image, self.ec2.images)
        record = self.fleet_ssm.values["/release-copies"]
        for image in [*record["images"]["us-east-1"].values()]:
            self.assertIn(image, self.ec2.images)
        for image in self.new.values():
            self.assertIn(image, self.west.images)
        self.assertEqual(record["previous_images"], {"us-east-1": {"host": previous}})

    def test_retention_takes_old_factory_and_unvalidated_handmade_images_only(self):
        import time as clock
        self.active_new_pair()
        def image(fake, image_id, name, days, **tags):
            created = datetime.datetime.fromtimestamp(clock.time() - days * 86400, datetime.timezone.utc)
            snapshot = "snap-" + image_id[4:]
            fake.snapshots[snapshot] = {"SnapshotId": snapshot, "OwnerId": "222222222222", "Tags": []}
            fake.images[image_id] = {"ImageId": image_id, "Name": name, "OwnerId": "222222222222", "Public": False,
                "State": "available", "CreationDate": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "Tags": [{"Key": k, "Value": v} for k, v in {"Purpose": "fcvm-fleet", **tags}.items()],
                "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot}}]}
        factory = {"PipelineSanitized": "true", "PipelineBuildId": "factory:00000000-0000-0000-0000-00000000000f"}
        ids = {name: "ami-" + format(0xb000 + i, "017x") for i, name in enumerate(
            ["old-handmade", "new-handmade", "validated-handmade", "east-factory", "west-factory", "foreign-factory", "other-name",
             "recent-factory"])}
        image(self.ec2, ids["old-handmade"], "fcvm-fleet-host-x86_64-1234abcd-202610230135", 15)
        image(self.ec2, ids["new-handmade"], "fcvm-fleet-host-x86_64-1234abce-202611060135", 13)
        image(self.ec2, ids["validated-handmade"], "fcvm-fleet-tile-x86_64-1234abcf-202610230135", 30, FleetImageValidated="true")
        image(self.ec2, ids["east-factory"], "fcvm-factory-" + "1" * 24 + "-host", 3, **factory)
        # Younger than a publication lease: possibly a publication still in progress.
        image(self.ec2, ids["recent-factory"], "fcvm-factory-" + "4" * 24 + "-host", 1, **factory)
        # In the build region the factory's own retention (dispatch.py) owns candidates.
        self.env["BUILD_REGION"] = "us-west-2"
        image(self.west, ids["west-factory"], "fcvm-factory-" + "2" * 24 + "-host", 30, **factory)
        image(self.ec2, ids["foreign-factory"], "fcvm-factory-" + "3" * 24 + "-host", 30, PipelineSanitized="true", PipelineBuildId="other:1")
        image(self.ec2, ids["other-name"], "something-else", 30)
        self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual(sorted(self.ec2.deregistered), sorted([ids["old-handmade"], ids["east-factory"]]))
        self.assertEqual(self.ec2.deleted_snapshots, [])
        self.assertTrue(all("snap-" + ids[name][4:] in self.ec2.snapshots for name in ("old-handmade", "east-factory")))
        self.assertEqual(self.west.deregistered, [])

    def test_retention_keeps_fresh_copies_spot_images_and_copies_of_kept_images(self):
        self.active_new_pair()
        fresh, spot, linked = "ami-" + "c1" * 8 + "c", "ami-" + "c2" * 8 + "c", "ami-" + "c3" * 8 + "c"
        recent = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.publisher_copy(self.west, fresh, "ami-" + "d1" * 8 + "d", "us-east-1", created=recent)
        self.publisher_copy(self.west, spot, "ami-" + "d2" * 8 + "d", "us-east-1")
        self.west.spot_requests["sir-00000001"] = {"SpotInstanceRequestId": "sir-00000001", "State": "open", "Type": "persistent",
                                                   "LaunchSpecification": {"ImageId": spot}}
        # A copy of the previous release's image, named by no record (one written before previous_images).
        previous = self.fleet_ssm.values["/release"]["previous"] = {"host_amis": {"us-east-1": {"x86_64": self.old["host"]}},
                                                                    "tile_amis": {}}
        self.publisher_copy(self.west, linked, previous["host_amis"]["us-east-1"]["x86_64"], "us-east-1")
        self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual(self.west.deregistered, [])

    def test_a_failed_snapshot_deletion_is_retried_by_the_next_run(self):
        self.active_new_pair()
        stale, snapshot = "ami-" + "c4" * 8 + "c", "snap-" + "c4" * 8 + "c"
        self.publisher_copy(self.west, stale, "ami-" + "d4" * 8 + "d", "us-east-1", snapshot=snapshot)
        self.west.fail_snapshot_delete = True
        with self.assertRaises(RuntimeError): self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual((self.west.deregistered, self.west.deleted_snapshots), ([stale], []))
        self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual(self.west.deleted_snapshots, [snapshot])

    def test_retention_never_claims_an_unproven_snapshot(self):
        self.active_new_pair()
        stale = "ami-" + "c5" * 8 + "c"
        legacy, retired_only, wrong_owner = ["snap-" + format(0xc500 + i, "017x") for i in range(3)]
        self.publisher_copy(self.west, stale, "ami-" + "d5" * 8 + "d", "us-east-1", snapshot=legacy)
        for snapshot, owner, tags in ((legacy, "222222222222", []),
            (retired_only, "222222222222", [{"Key": "FleetRetired", "Value": "true"}]),
            (wrong_owner, "333333333333", [{"Key": "FleetRetired", "Value": "true"},
                                         {"Key": "FleetPublisherCopy", "Value": "true"}])):
            self.west.snapshots[snapshot] = {"SnapshotId": snapshot, "OwnerId": owner, "Tags": tags}
        self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual(self.west.deregistered, [stale])
        self.assertEqual(self.west.deleted_snapshots, [])
        self.assertEqual(self.west.snapshots[legacy]["Tags"], [])

    def test_retention_removes_a_bounded_number_per_run(self):
        self.active_new_pair()
        for index in range(MODULE.RETAIN_LIMIT + 3):
            self.publisher_copy(self.west, "ami-" + format(0xd000 + index, "017x"), "ami-" + format(0xa000 + index, "017x"), "us-east-1")
        self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual(len(self.west.deregistered), MODULE.RETAIN_LIMIT)
        self.publisher().run({"action": "replicate-active-release"})
        self.assertEqual(len(self.west.deregistered), MODULE.RETAIN_LIMIT + 3)
        self.assertEqual(set(self.copies()), set(self.new.values()))

    def test_bulky_configuration_comes_from_the_bundle_not_the_environment(self):
        import tempfile
        bundled = {key: self.env.pop(key) for key in ("TEMPLATES_JSON", "SEED_RELEASE_JSON")}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "publisher-config.json")
            with open(path, "w") as handle:
                json.dump(bundled, handle)
            publisher = MODULE.Publisher(self.env, {}, config=path)
            self.assertEqual(publisher.templates, self.templates)
            self.assertEqual(publisher.reviewed_seed()["images"], self.new)
            with self.assertRaises(MODULE.Refused):
                MODULE.Publisher({**self.env, "TEMPLATES_JSON": bundled["TEMPLATES_JSON"]}, {}, config=path)
            with open(path, "w") as handle:
                json.dump({**bundled, "FLEET_ROLE": "arn:aws:iam::333333333333:role/other"}, handle)
            with self.assertRaises(MODULE.Refused):
                MODULE.Publisher(self.env, {}, config=path)


    def test_a_paused_factory_build_is_not_promoted_but_reviewed_actions_are(self):
        # A build already running when the factory was paused (fcvm_ami_factory_paused) still
        # finishes and asks to publish; it must not promote. A reviewed action still can.
        source = (Path(MODULE.__file__).parents[1] / "fcvm-fleet-ami-pipeline.tf").read_text()
        publisher_tf = re.search(r'^resource "aws_lambda_function" "fcvm_ami_publish" \{\n.*?^\}', source, re.S | re.M).group()
        self.assertRegex(publisher_tf, r"FACTORY_PAUSED\s*=\s*tostring\(var\.fcvm_ami_factory_paused\)")
        self.env["FACTORY_PAUSED"] = "true"
        before = copy.deepcopy(self.fleet_ssm.values["/release"])
        with self.assertRaises(MODULE.Refused) as caught:
            self.publisher().run(self.event)
        self.assertIn("paused", str(caught.exception))
        self.assertEqual(self.fleet_ssm.values["/release"], before)
        self.assertEqual((self.ec2.created, self.ec2.tagged, self.assets.copies), ([], [], []))
        self.assertEqual(self.seed_success(self.publisher(), self.legacy_seed()), {"status": "promoted", "generation": 1})
        self.setUp()
        self.env["FACTORY_PAUSED"] = "false"
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})

if __name__ == "__main__":
    unittest.main(buffer=True)
