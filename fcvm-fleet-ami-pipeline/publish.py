"""Promote one independently validated image pair under Terraform's exact lock.

Only a successful, fixed-project CodeBuild event reaches this consumer. The
worker has no template authority; the Fleet delegate can change only four
configured templates, and every new version differs solely in ImageId.
"""
import base64
import copy
import datetime
import hashlib
import json
import os
import re
import time
import uuid


SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"ami-[0-9a-f]{17}\Z")
SLOTS = {"host-build", "tile-build", "host-verify", "tile-verify"}
ARCHIVES = {f"part-{index:02d}.tar.zst" for index in range(16)} | {"dirs.tar.zst"}
STORE_FILES = ARCHIVES | {"parts", "sha256.json"}
DEADLINE_SECONDS = 240 * 60


class Refused(Exception):
    pass


def need(value, message):
    if not value:
        raise Refused(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def without_image(value):
    return {key: item for key, item in value.items() if key != "ImageId"}


class Publisher:
    def __init__(self, env=None, clients=None, clock=time.time, sleep=time.sleep):
        self.env = dict(os.environ if env is None else env)
        self.clients = clients or {}
        self.clock = clock
        self.sleep = sleep
        self.templates = json.loads(self.env["TEMPLATES_JSON"])
        need(len(self.templates) == 4 and len({t["id"] for t in self.templates}) == 4,
             "exactly four distinct configured templates are required")
        need(all(set(t) == {"region", "id", "kind", "arch"}
                 and t["region"] == self.env["COMPUTE_REGION"] and t["arch"] == "x86_64"
                 and re.fullmatch(r"lt-[0-9a-f]{17}", t["id"])
                 and t["kind"] in {"host", "tile"} for t in self.templates)
             and sum(t["kind"] == "host" for t in self.templates) == 1,
             "template configuration is outside the fixed image-pair scope")
        self.build_id = None
        self.lock_info = None
        self.transaction = None
        self.committed = False
        self.recovery_failed = False

    def request(self, source, input_sha):
        name = self.env["STATE_PREFIX"] + "/requests/" + input_sha
        value = self.load(name)
        need(value.get("request_id") == input_sha and value.get("input_sha256") == input_sha
             and value.get("source_sha") == source and value.get("build_id") == self.build_id
             and value.get("priority") in {"normal", "critical"}, "accepted request provenance changed")
        return name, value

    def current_request(self, source, input_sha):
        need(self.load(self.env["STATE_PREFIX"] + "/control").get("active") == self.build_id,
             "a superseded build cannot publish")
        _, request = self.request(source, input_sha)
        need(request.get("status") not in {"superseded", "failed"}, "a superseded build cannot publish")

    def client(self, service, fleet=False, region=None):
        region = region or (self.env["PIPELINE_REGION"] if service == "s3" and not fleet
                            else self.env["COMPUTE_REGION" if fleet else "CONTROL_REGION"])
        key = (service, fleet, region)
        if key not in self.clients:
            import boto3
            from botocore.config import Config
            options = {"region_name": region,
                       "config": Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=60)}
            if fleet:
                credentials = self.client("sts").assume_role(RoleArn=self.env["FLEET_ROLE"],
                    RoleSessionName="fleet-image-publisher", DurationSeconds=3600)["Credentials"]
                options.update(aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"], aws_session_token=credentials["SessionToken"])
            self.clients[key] = boto3.client(service, **options)
        return self.clients[key]

    def load(self, name, fleet=False):
        response = self.client("ssm", fleet).get_parameter(Name=name, WithDecryption=False)
        raw = response["Parameter"]["Value"]
        need(len(raw.encode()) <= 4000, "publication metadata exceeds its fixed bound")
        return json.loads(raw)

    def save(self, name, value, fleet=False):
        raw = encoded(value)
        need(len(raw.encode()) <= 4000, "publication metadata exceeds its fixed bound")
        self.client("ssm", fleet).put_parameter(Name=name, Type="String", Value=raw, Overwrite=True)

    def prior_transaction(self):
        try:
            value = self.load(self.env["STATE_PREFIX"] + "/publications/" + digest(self.build_id))
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            raise
        need(value.get("build_id") == self.build_id, "publication receipt identity changed")
        return value

    def acquire(self):
        # This is the same LockID used by Terraform's S3 backend, not a parallel lock.
        self.lock_info = encoded({"ID": str(uuid.uuid4()), "Operation": "OperationTypeApply",
            "Who": "fcvm-image-publisher", "Version": "1.10.3",
            "Created": datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).isoformat(),
            "Path": self.env["LOCK_ID"], "Info": self.build_id})
        self.client("dynamodb").put_item(TableName=self.env["LOCK_TABLE"],
            Item={"LockID": {"S": self.env["LOCK_ID"]}, "Info": {"S": self.lock_info}},
            ConditionExpression="attribute_not_exists(LockID)")

    def release(self):
        if self.lock_info is not None:
            self.client("dynamodb").delete_item(TableName=self.env["LOCK_TABLE"],
                Key={"LockID": {"S": self.env["LOCK_ID"]}}, ConditionExpression="#info = :ours",
                ExpressionAttributeNames={"#info": "Info"}, ExpressionAttributeValues={":ours": {"S": self.lock_info}})
            self.lock_info = None

    def event(self, event):
        need(isinstance(event, dict) and event.get("source") == "aws.codebuild"
             and event.get("detail-type") == "CodeBuild Build State Change"
             and event.get("account") == self.env["CONTROL_ACCOUNT"]
             and event.get("region") == self.env["CONTROL_REGION"], "not the configured CodeBuild event")
        detail = event.get("detail", {})
        need(detail.get("build-status") == "SUCCEEDED"
             and detail.get("project-name") == self.env["PROJECT_NAME"], "only successful factory builds may publish")
        build_id = detail.get("build-id", "")
        prefix = f"arn:aws:codebuild:{self.env['CONTROL_REGION']}:{self.env['CONTROL_ACCOUNT']}:build/"
        if build_id.startswith(prefix):
            build_id = build_id[len(prefix):]
        need(re.fullmatch(re.escape(self.env["PROJECT_NAME"]) + r":[0-9a-f-]{36}", build_id), "invalid factory build identity")
        self.build_id = build_id
        builds = self.client("codebuild").batch_get_builds(ids=[build_id])["builds"]
        need(len(builds) == 1, "factory build is missing")
        build = builds[0]
        need(build["id"] == build_id and build["projectName"] == self.env["PROJECT_NAME"]
             and build["buildStatus"] == "SUCCEEDED" and build["buildComplete"] is True,
             "actual CodeBuild result is not successful")
        variables = {item["name"]: item["value"] for item in build["environment"]["environmentVariables"]}
        source, input_sha = variables.get("SOURCE_SHA", ""), variables.get("INPUT_SHA256", "")
        need(SHA.fullmatch(source) and DIGEST.fullmatch(input_sha)
             and build.get("resolvedSourceVersion") == source, "factory source provenance changed")
        need(0 < (build["endTime"] - build["startTime"]).total_seconds() <= DEADLINE_SECONDS,
             "build exceeded its fixed execution lease")
        return build, source, input_sha

    def proof(self, build, source, input_sha):
        prefix = self.env["STATE_PREFIX"]
        control = self.load(prefix + "/control")
        need(control.get("active") == self.build_id, "a superseded build cannot publish")
        journal = self.load(prefix + "/builds/" + digest(self.build_id))
        need(journal.get("build_id") == self.build_id and journal.get("source_sha") == source
             and journal.get("input_sha256") == input_sha and journal.get("request_id") == input_sha
             and set(journal.get("slots", {})) == SLOTS
             and set(journal.get("candidates", {})) == {"host", "tile"}
             and journal["deadline_unix"] == int(build["startTime"].timestamp()) + DEADLINE_SECONDS
             and build["endTime"].timestamp() <= journal["deadline_unix"], "broker proof does not match this completed build")
        ids = [journal["slots"][slot].get("instance_id", "") for slot in sorted(SLOTS)]
        need(len(set(ids)) == 4 and all(re.fullmatch(r"i-[0-9a-f]{17}", item) for item in ids),
             "broker has no four distinct instance receipts")
        rows = [item for group in self.client("ec2", True).describe_instances(InstanceIds=ids)["Reservations"] for item in group["Instances"]]
        need({item["InstanceId"] for item in rows} == set(ids)
             and all(item["State"]["Name"] == "terminated" for item in rows), "factory instances are not all terminated")
        key = "runs/" + digest(self.build_id) + "/manifest.json"
        response = self.client("s3").get_object(Bucket=self.env["PIPELINE_BUCKET"], Key=key)
        raw = response["Body"].read(32769)
        need(len(raw) <= 32768, "factory manifest exceeds its fixed bound")
        manifest = json.loads(raw)
        tls = manifest.get("tls", {})
        need(manifest.get("version") == 1 and manifest.get("build_id") == self.build_id
             and manifest.get("source_commit") == source and manifest.get("input_sha256") == input_sha
             and DIGEST.fullmatch(manifest.get("source_tree_sha256", ""))
             and manifest.get("guard_sha256") == self.env["GUARD_SHA256"]
             and manifest.get("deadline_unix") == journal["deadline_unix"]
             and set(manifest.get("images", {})) == {"host", "tile"}
             and set(tls) == {"build_cert_sha256", "test_cert_sha256"}
             and all(DIGEST.fullmatch(value) for value in tls.values())
             and tls["build_cert_sha256"] != tls["test_cert_sha256"], "factory manifest provenance does not match")
        for kind in ("host", "tile"):
            image = manifest["images"][kind]
            validation = image.get("validation", {})
            boots = validation.get("boot_ids", [])
            need(image.get("image_id") == journal["candidates"][kind] and IMAGE.fullmatch(image["image_id"])
                 and DIGEST.fullmatch(image.get("renderer_sha256", ""))
                 and re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("renderer_image_id", ""))
                 and all(validation.get(field) is True for field in ("guard", "frames", "sanitized", "tls_rotation"))
                 and all(validation.get(key) == value for key, value in tls.items())
                 and (kind != "host" or validation.get("phone_clone") is True)
                 and len(boots) == len(set(boots)) == 3
                 and all(re.fullmatch(r"[0-9a-f-]{36}", value) for value in boots), "candidate lacks the full boot/security proof")
        need(manifest["images"]["host"]["image_id"] != manifest["images"]["tile"]["image_id"]
             and manifest["images"]["host"]["renderer_image_id"] == manifest["images"]["tile"]["renderer_image_id"]
             and manifest["images"]["host"]["renderer_sha256"] == manifest["images"]["tile"]["renderer_sha256"],
             "host and tile are not one paired renderer release")
        return manifest

    def images(self, manifest, source, input_sha):
        ec2 = self.client("ec2", True)
        resources = []
        for kind in ("host", "tile"):
            image_id = manifest["images"][kind]["image_id"]
            rows = ec2.describe_images(ImageIds=[image_id])["Images"]
            need(len(rows) == 1, "candidate image is missing")
            image = rows[0]
            expected = {"Purpose": "fcvm-fleet", "FleetImageKind": kind,
                "GuardHelperSHA256": self.env["GUARD_SHA256"], "SourceCommit": source,
                "InputSHA256": input_sha, "PipelineBuildId": self.build_id, "PipelineSanitized": "true"}
            tags = {item["Key"]: item["Value"] for item in image.get("Tags", [])}
            need(image["OwnerId"] == self.env["FLEET_ACCOUNT"] and image.get("Public") is False
                 and image.get("State") == "available" and image.get("Architecture") == "x86_64"
                 and image.get("RootDeviceType") == "ebs" and all(tags.get(key) == value for key, value in expected.items()),
                 "candidate image is outside the private validated factory lineage")
            snapshots = [item["Ebs"]["SnapshotId"] for item in image.get("BlockDeviceMappings", []) if item.get("Ebs", {}).get("SnapshotId")]
            need(0 < len(snapshots) <= 4, "candidate has no bounded EBS snapshot set")
            found = ec2.describe_snapshots(SnapshotIds=snapshots)["Snapshots"]
            need({item["SnapshotId"] for item in found} == set(snapshots), "candidate snapshot is missing")
            for snapshot in found:
                tags = {item["Key"]: item["Value"] for item in snapshot.get("Tags", [])}
                need(snapshot["OwnerId"] == self.env["FLEET_ACCOUNT"] and snapshot.get("State") == "completed"
                     and snapshot.get("Encrypted") is True and all(tags.get(key) == value for key, value in expected.items()),
                     "candidate snapshot provenance does not match")
            resources.extend([image_id, *snapshots])
        return resources

    def store(self, manifest, input_sha):
        """Copy only immutable, proof-bound source versions into runtime assets.

        The backend lock serializes the sole Fleet-prefix writer. CodeBuild and
        builders cannot write this destination; differing existing bytes refuse
        publication rather than overwriting another release.
        """
        proof = manifest.get("store", {})
        source_prefix = "runs/" + digest(self.build_id) + "/store/"
        target_prefix = "factory/" + input_sha + "/store/"
        destination = {"bucket": self.env["ASSET_BUCKET"], "region": self.env["ASSET_REGION"], "prefix": target_prefix}
        objects = proof.get("objects", {})
        need(proof.get("bucket") == self.env["PIPELINE_BUCKET"]
             and proof.get("region") == self.env["PIPELINE_REGION"] and proof.get("prefix") == source_prefix
             and proof.get("destination") == destination and set(objects) == STORE_FILES,
             "runtime store proof is outside the fixed source and destination")
        source = self.client("s3")
        target = self.client("s3", True, self.env["ASSET_REGION"])
        for name, item in objects.items():
            need(set(item) == {"sha256", "version_id", "size"} and DIGEST.fullmatch(item.get("sha256", ""))
                 and isinstance(item.get("version_id"), str) and item["version_id"] not in {"", "null"}
                 and len(item["version_id"]) <= 1024 and type(item.get("size")) is int
                 and 0 < item["size"] < (5 * 1024**3 if name in ARCHIVES else 65536),
                 "runtime store object has no bounded immutable digest receipt")
        for name in ("parts", "sha256.json"):
            item = objects[name]
            response = source.get_object(Bucket=proof["bucket"], Key=source_prefix + name,
                VersionId=item["version_id"], ExpectedBucketOwner=self.env["CONTROL_ACCOUNT"])
            data = response["Body"].read(65537)
            need(len(data) == item["size"] and hashlib.sha256(data).hexdigest() == item["sha256"],
                 "runtime store manifest digest changed")
            if name == "parts":
                need(data == ("\n".join(f"part-{i:02d}" for i in range(16)) + "\n").encode(),
                     "runtime store is not the complete sixteen-part image")
            else:
                need(json.loads(data) == {name: objects[name]["sha256"] for name in ARCHIVES},
                     "runtime archive digests disagree with their manifest")
        existing = set()
        arguments = {"Bucket": destination["bucket"], "Prefix": target_prefix,
                     "ExpectedBucketOwner": self.env["FLEET_ACCOUNT"]}
        for _ in range(5):
            page = target.list_objects_v2(**arguments)
            existing.update(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                break
            arguments["ContinuationToken"] = page["NextContinuationToken"]
        else:
            raise Refused("runtime destination exceeds the fixed object bound")
        need(existing <= {target_prefix + name for name in STORE_FILES}, "runtime destination contains unrelated objects")
        for name in sorted(STORE_FILES):
            item = objects[name]
            head = source.head_object(Bucket=proof["bucket"], Key=source_prefix + name,
                VersionId=item["version_id"], ExpectedBucketOwner=self.env["CONTROL_ACCOUNT"], ChecksumMode="ENABLED")
            checksum = base64.b64encode(bytes.fromhex(item["sha256"])).decode()
            need(head.get("VersionId") == item["version_id"] and head["ContentLength"] == item["size"]
                 and head.get("Metadata", {}).get("sha256") == item["sha256"]
                 and head.get("ChecksumSHA256") == checksum, "immutable source object receipt changed")
            key = target_prefix + name
            if key not in existing:
                response = target.copy_object(Bucket=destination["bucket"], Key=key,
                    CopySource={"Bucket": proof["bucket"], "Key": source_prefix + name, "VersionId": item["version_id"]},
                    CopySourceIfMatch=head["ETag"], ExpectedBucketOwner=self.env["FLEET_ACCOUNT"],
                    ExpectedSourceBucketOwner=self.env["CONTROL_ACCOUNT"], MetadataDirective="REPLACE",
                    Metadata={"sha256": item["sha256"], "factory-input-sha256": input_sha},
                    ServerSideEncryption="AES256", ChecksumAlgorithm="SHA256")
                need(response.get("CopySourceVersionId") == item["version_id"]
                     and response.get("CopyObjectResult", {}).get("ChecksumSHA256") == checksum,
                     "runtime copy did not confirm its content checksum")
            copied = target.head_object(Bucket=destination["bucket"], Key=key,
                ExpectedBucketOwner=self.env["FLEET_ACCOUNT"], ChecksumMode="ENABLED")
            need(copied["ContentLength"] == item["size"] and copied.get("Metadata", {}).get("sha256") == item["sha256"]
                 and copied.get("Metadata", {}).get("factory-input-sha256") == input_sha
                 and copied.get("ChecksumSHA256") == checksum,
                 "runtime destination differs; existing assets were not overwritten")

    def current(self, template):
        ec2 = self.client("ec2", True)
        rows = ec2.describe_launch_templates(LaunchTemplateIds=[template])["LaunchTemplates"]
        need(len(rows) == 1 and rows[0]["LaunchTemplateId"] == template, "configured template is missing")
        number = rows[0]["DefaultVersionNumber"]
        need(type(number) is int and number > 0, "template has no explicit default version")
        versions = ec2.describe_launch_template_versions(LaunchTemplateId=template, Versions=[str(number)])["LaunchTemplateVersions"]
        need(len(versions) == 1 and versions[0]["VersionNumber"] == number, "default template version is missing")
        return versions[0]

    def version(self, template, current, image, label, entry=None):
        ec2 = self.client("ec2", True)
        description = "fleet-image-" + digest(self.build_id + "/" + template + "/" + label)
        try:
            response = ec2.create_launch_template_version(LaunchTemplateId=template,
                SourceVersion=str(current["VersionNumber"]), LaunchTemplateData={"ImageId": image},
                VersionDescription=description, ClientToken=digest(description))
            created = response["LaunchTemplateVersion"]
        except Exception:
            # Never replay an uncertain create. Discover only our immutable label.
            found = []
            arguments = {"LaunchTemplateId": template, "MaxResults": 100}
            for _ in range(20):
                page = ec2.describe_launch_template_versions(**arguments)
                found.extend(item for item in page["LaunchTemplateVersions"]
                             if item.get("VersionDescription") == description)
                if not page.get("NextToken"):
                    break
                arguments["NextToken"] = page["NextToken"]
            else:
                self.recovery_failed = True
                raise Refused("uncertain template create exceeds the discovery bound") from None
            if len(found) != 1:
                self.recovery_failed = True
                raise Refused("uncertain template create needs administrator reconciliation") from None
            created = found[0]
        if not (created.get("LaunchTemplateId") == template and created.get("VersionDescription") == description
                and type(created.get("VersionNumber")) is int):
            self.recovery_failed = True
            raise Refused("created version identity cannot be reconciled")
        if entry is not None:
            entry["candidate"] = created["VersionNumber"]
            self.record()
        need(created.get("LaunchTemplateId") == template and created.get("VersionDescription") == description
             and type(created.get("VersionNumber")) is int
             and created["LaunchTemplateData"].get("ImageId") == image
             and without_image(created["LaunchTemplateData"]) == without_image(current["LaunchTemplateData"]),
             "candidate version changed infrastructure configuration")
        return created["VersionNumber"]

    def select(self, template, version):
        ec2 = self.client("ec2", True)
        try:
            ec2.modify_launch_template(LaunchTemplateId=template, DefaultVersion=str(version),
                ClientToken=digest(self.build_id + "/" + template + "/default/" + str(version)))
        except Exception:
            # A lost response is resolved by reading the actual explicit default.
            if self.current(template)["VersionNumber"] != version:
                raise Refused("template default update was not confirmed") from None
        need(self.current(template)["VersionNumber"] == version, "template default update was not confirmed")

    def record(self):
        self.save(self.env["STATE_PREFIX"] + "/publications/" + digest(self.build_id), self.transaction)

    def rollback(self):
        ec2 = self.client("ec2", True)
        for template, entry in reversed(list(self.transaction["entries"].items())):
            current = self.current(template)
            if current["VersionNumber"] == entry.get("candidate"):
                # Preserve the CURRENT configuration, even during failure recovery.
                restored = self.version(template, current, entry["old_image"], "rollback-" + str(current["VersionNumber"]))
                self.select(template, restored)
            else:
                need(current["LaunchTemplateData"].get("ImageId") == entry["old_image"],
                     "another default writer prevents automatic rollback")
            candidate = entry.get("candidate")
            if candidate:
                need(self.current(template)["VersionNumber"] != candidate, "cannot delete a selected candidate version")
                result = ec2.delete_launch_template_versions(LaunchTemplateId=template, Versions=[str(candidate)])
                need(not result.get("UnsuccessfullyDeletedLaunchTemplateVersions"), "candidate version cleanup was not confirmed")
        self.transaction["status"] = "rolled_back"
        self.record()

    def promote(self, manifest, source, input_sha, resources):
        name = self.env["RELEASE_PARAMETER"]
        before = self.load(name, True)
        need(before.get("version") == 1 and before.get("guard_sha256") == self.env["GUARD_SHA256"]
             and type(before.get("generation")) is int and before["generation"] >= 0,
             "active release schema is incompatible")
        if before.get("build_id") == self.build_id:
            return before
        self.transaction = {"status": "preparing", "build_id": self.build_id, "entries": {}, "before": before}
        # Persist the rollback image identities before changing any default.
        self.record()
        for template in self.templates:
            current = self.current(template["id"])
            old_image = before[template["kind"] + "_amis"][template["region"]][template["arch"]]
            need(current["LaunchTemplateData"].get("ImageId") == old_image, "release pointer and template default disagree")
            entry = {"old_image": old_image, "old_version": current["VersionNumber"]}
            self.transaction["entries"][template["id"]] = entry
            self.record()
            entry["candidate"] = self.version(template["id"], current, manifest["images"][template["kind"]]["image_id"], "candidate", entry)
            self.record()
        self.client("ec2", True).create_tags(Resources=resources, Tags=[{"Key": "FleetImageValidated", "Value": "true"}])
        for template, entry in self.transaction["entries"].items():
            need(self.current(template)["VersionNumber"] == entry["old_version"], "template default changed during publication")
            self.select(template, entry["candidate"])
        after = copy.deepcopy(before)
        after.update(generation=before["generation"] + 1, source_sha=source, build_id=self.build_id,
                     previous={key: value for key, value in before.items() if key != "previous"})
        after["versions"] = {t["region"] + "/" + t["id"]: self.transaction["entries"][t["id"]]["candidate"] for t in self.templates}
        for kind in ("host", "tile"):
            after[kind + "_amis"][self.env["COMPUTE_REGION"]]["x86_64"] = manifest["images"][kind]["image_id"]
        self.current_request(source, input_sha)
        try:
            self.save(name, after, True)
        except Exception:
            need(self.load(name, True) == after, "release pointer update was not confirmed")
        need(self.load(name, True) == after, "release pointer update was not confirmed")
        self.committed = True
        self.transaction["status"] = "committed"
        self.record()
        return after

    def retire(self, release):
        """Critical only: reclaim old-image Fleet placements, never dev machines.

        This runs AFTER the pair is committed. Failure must keep the new release
        and retry reclamation, rather than rolling back to vulnerable images.
        """
        templates = {item["id"]: item for item in self.templates}
        ec2 = self.client("ec2", True)

        def eligible(instance, owner):
            tags = {item["Key"]: item["Value"] for item in instance.get("Tags", [])}
            # DescribeInstances exposes these AWS-managed, immutable tags, not
            # a LaunchTemplate object. Never trust a synthetic fallback field.
            template = templates.get(tags.get("aws:ec2launchtemplate:id"))
            if (template is None or owner != self.env["FLEET_ACCOUNT"]
                    or not re.fullmatch(r"[1-9][0-9]*", tags.get("aws:ec2launchtemplate:version", ""))):
                return False
            kind = template["kind"]
            current = release[kind + "_amis"][template["region"]][template["arch"]]
            return (instance.get("InstanceType") == {"host": "g4dn.metal", "tile": "g4dn.xlarge"}[kind]
                and tags.get("Purpose") == "fcvm-fleet-" + kind
                and IMAGE.fullmatch(instance.get("ImageId", "")) and instance["ImageId"] != current
                and instance.get("State", {}).get("Name") in {"pending", "running", "stopping", "stopped", "shutting-down"})

        ids = set()
        arguments = {"Filters": [{"Name": "tag:Purpose", "Values": ["fcvm-fleet-host", "fcvm-fleet-tile"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}]}
        for _ in range(20):
            page = ec2.describe_instances(**arguments)
            for reservation in page["Reservations"]:
                ids.update(item["InstanceId"] for item in reservation["Instances"] if eligible(item, reservation.get("OwnerId")))
            if not page.get("NextToken"):
                break
            arguments["NextToken"] = page["NextToken"]
        else:
            raise Refused("critical Fleet inventory exceeds its fixed bound")
        need(len(ids) <= 64, "critical Fleet inventory exceeds its fixed bound")
        if not ids:
            return
        # Recheck exact identities immediately before the only runtime mutation.
        rows = ec2.describe_instances(InstanceIds=sorted(ids))["Reservations"]
        actual = {item["InstanceId"] for reservation in rows for item in reservation["Instances"]
                  if eligible(item, reservation.get("OwnerId"))}
        need(actual == ids, "critical Fleet placement changed before reclamation")
        instances = {item["InstanceId"]: item for reservation in rows for item in reservation["Instances"]}
        spot = {item["SpotInstanceRequestId"]: item for item in instances.values() if item.get("SpotInstanceRequestId")}
        if spot:
            requests = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=sorted(spot))["SpotInstanceRequests"]
            need({item["SpotInstanceRequestId"] for item in requests} == set(spot), "Fleet Spot request receipt is incomplete")
            persistent = []
            for request in requests:
                instance = spot[request["SpotInstanceRequestId"]]
                need(request.get("InstanceId") == instance["InstanceId"] and request.get("Type") in {"one-time", "persistent"},
                     "Fleet Spot request is bound to a different placement")
                if request["Type"] == "persistent":
                    tags = {item["Key"]: item["Value"] for item in request.get("Tags", [])}
                    spec = request.get("LaunchSpecification", {})
                    need(tags.get("Purpose") == "fcvm-fleet-tile" and instance["InstanceType"] == "g4dn.xlarge"
                         and spec.get("InstanceType") == instance["InstanceType"] and spec.get("ImageId") == instance["ImageId"],
                         "persistent Spot request is outside the owned tile scope")
                    need(request.get("State") in {"open", "active", "disabled", "cancelled", "closed"}, "persistent Spot request state is unknown")
                    persistent.append(request["SpotInstanceRequestId"])
            if persistent:
                # A persistent request otherwise reopens its OLD launch
                # specification when the instance is terminated.
                pending = [item["SpotInstanceRequestId"] for item in requests
                           if item["SpotInstanceRequestId"] in persistent and item["State"] not in {"cancelled", "closed"}]
                if pending:
                    ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=sorted(pending))
                verified = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=sorted(persistent))["SpotInstanceRequests"]
                need({item["SpotInstanceRequestId"] for item in verified} == set(persistent)
                     and all(item["State"] in {"cancelled", "closed"} for item in verified), "persistent Spot cancellation is not confirmed")
        # Cancelling a disabled request can automatically terminate its stopped
        # instance. Never mistake that expected transition for lost ownership.
        rows = ec2.describe_instances(InstanceIds=sorted(ids))["Reservations"]
        remaining = []
        for reservation in rows:
            for instance in reservation["Instances"]:
                need(instance["InstanceId"] in ids, "unexpected placement appeared during reclamation")
                if instance["State"]["Name"] not in {"terminated", "shutting-down"}:
                    need(eligible(instance, reservation.get("OwnerId")), "Fleet placement changed during cancellation")
                    remaining.append(instance["InstanceId"])
        if remaining:
            ec2.terminate_instances(InstanceIds=sorted(remaining))
        for _ in range(12):
            rows = ec2.describe_instances(InstanceIds=sorted(ids))["Reservations"]
            found = {item["InstanceId"]: item["State"]["Name"] for reservation in rows for item in reservation["Instances"]}
            if set(found) == ids and all(value == "terminated" for value in found.values()):
                return
            self.sleep(10)
        raise Refused("critical release committed; old Fleet reclamation is still pending")

    def finish(self, result, source, input_sha):
        name, request = self.request(source, input_sha)
        if request["priority"] == "critical":
            try:
                self.retire(result)
            except Exception:
                request["status"] = "awaiting-publish"
                self.save(name, request)
                raise
        request.update(status="promoted", generation=result["generation"])
        self.save(name, request)
        return {"status": "promoted", "generation": result["generation"]}

    def run(self, event):
        build, source, input_sha = self.event(event)
        active = self.load(self.env["RELEASE_PARAMETER"], True)
        manifest = None
        resources = None
        if active.get("build_id") != self.build_id:
            prior = self.prior_transaction()
            if prior and prior.get("status") == "rolled_back":
                name, request = self.request(source, input_sha)
                request.update(status="failed", message="publication rolled back; automatic replay refused")
                self.save(name, request)
                return {"status": "failed", "reason": "rolled_back"}
            self.request(source, input_sha)
            manifest = self.proof(build, source, input_sha)
            resources = self.images(manifest, source, input_sha)
            # Large content transfers need not hold the infrastructure state lock.
            self.store(manifest, input_sha)
        self.acquire()
        try:
            active = self.load(self.env["RELEASE_PARAMETER"], True)
            if active.get("build_id") == self.build_id:
                self.committed = True
                return self.finish(active, source, input_sha)
            self.request(source, input_sha)
            self.current_request(source, input_sha)
            need(self.proof(build, source, input_sha) == manifest,
                 "factory proof changed before the publication lock")
            result = self.promote(manifest, source, input_sha, resources)
            return self.finish(result, source, input_sha)
        except Exception:
            if self.transaction and not self.committed:
                try:
                    self.rollback()
                except Exception:
                    self.recovery_failed = True
                    # Retain the exact backend lock on uncertain recovery. Never
                    # let Terraform proceed through a partially selected pair.
                    raise Refused("image publication recovery needs administrator reconciliation") from None
            raise
        finally:
            if not self.recovery_failed:
                self.release()


def lambda_handler(event, context):
    try:
        return Publisher().run(event)
    except Exception:
        # SDK errors may contain request details. Only a static failure escapes.
        raise RuntimeError("validated image publication failed; inspect the bounded publication journal") from None
