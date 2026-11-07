#!/usr/bin/env python3
"""Credential-free publisher transactions against strict in-memory AWS fakes."""
import base64
import copy
import datetime
import hashlib
import http.client
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


class CopyEC2:
    """A copy region: copy_image makes a pending image carrying the source's tags."""
    def __init__(self, source):
        self.source, self.images, self.copies = source, {}, []
        self.fail_copy = False

    def describe_images(self, Owners, Filters):
        assert Owners == ["222222222222"] and [item["Name"] for item in Filters] == ["name"]
        names = Filters[0]["Values"]
        return {"Images": [copy.deepcopy(item) for item in self.images.values() if item["Name"] in names]}

    def copy_image(self, SourceImageId, SourceRegion, Name, Description, Encrypted, CopyImageTags, ClientToken):
        assert SourceRegion == "us-east-1" and Encrypted is True and CopyImageTags is True
        assert ClientToken == MODULE.digest(SourceImageId + "/us-west-2")
        if self.fail_copy:
            raise RuntimeError("copy unavailable")
        assert all(item["Name"] != Name for item in self.images.values()), "a name is never copied twice"
        source = self.source.images[SourceImageId]
        image_id = "ami-" + format(0xc0000 + len(self.images), "017x")
        self.images[image_id] = {"ImageId": image_id, "Name": Name, "OwnerId": source["OwnerId"], "Public": False,
            "State": "pending", "Tags": copy.deepcopy(source["Tags"])}
        self.copies.append((SourceImageId, image_id))
        return {"ImageId": image_id}


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
            "COPY_REGIONS": "us-west-2"}
        start = datetime.datetime(2026, 10, 31, tzinfo=datetime.timezone.utc)
        build = {"id": self.build, "projectName": "factory", "buildStatus": "SUCCEEDED", "buildComplete": True,
            "resolvedSourceVersion": self.source, "startTime": start, "endTime": start + datetime.timedelta(hours=2),
            "environment": {"environmentVariables": [{"name": "SOURCE_SHA", "value": self.source}, {"name": "INPUT_SHA256", "value": self.input}]}}
        self.main_ssm, self.fleet_ssm, self.ddb, self.ec2 = SSM(), SSM(), DDB(), EC2()
        self.west = CopyEC2(self.ec2)
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
                   "deadline_unix": int(start.timestamp()) + 14400, "slots": {}, "candidates": new}
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
                    "sanitized": True, "tls_rotation": True, "phone_clone": kind == "host", **self.manifest["tls"],
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
            image["ImageId"] = image_id
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
            image["ImageId"] = image_id
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
            image["ImageId"] = image_id
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
        self.ec2.images[image_id] = {**image, "ImageId": image_id}
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

    def copies(self):
        return {source: copied for source, copied in self.west.copies}

    def test_published_pair_is_copied_to_each_copy_region_and_recorded(self):
        self.assertEqual(self.publisher().run(self.event), {"status": "promoted", "generation": 1})
        copies = self.copies()
        self.assertEqual(set(copies), set(self.new.values()))
        self.assertEqual(self.fleet_ssm.values["/release-copies"], {"version": 1, "generation": 1, "build_id": self.build,
            "source_sha": self.source, "images": {"us-east-1": self.new,
                "us-west-2": {kind: copies[image] for kind, image in self.new.items()}}})
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
        self.assertEqual(self.main_ssm.values["/factory/requests/" + self.input]["status"], "promoted")
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
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                self.setUp()
                self.active_new_pair()
                source = self.ec2.images[self.new["host"]]
                tags = copy.deepcopy(source["Tags"])
                if foreign:
                    tags = [tag for tag in tags if tag["Key"] != "FleetImageKind"] + [{"Key": "FleetImageKind", "Value": "tile"}]
                self.west.images["ami-" + "9" * 17] = {"ImageId": "ami-" + "9" * 17, "Name": source["Name"],
                    "OwnerId": "222222222222", "Public": False, "State": "available", "Tags": tags}
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


if __name__ == "__main__":
    unittest.main(buffer=True)
