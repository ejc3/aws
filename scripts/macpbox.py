#!/usr/bin/env python3
"""One shared EC2 Mac. Reads are free of allocation; up requires a live price approval."""
import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

REGION = "us-west-2"
TYPE = "mac-m4pro.metal"
PARAMETER = "/infra/macpbox"
HOURS = 24


class Error(Exception):
    pass


def need(condition, message):
    if not condition:
        raise Error(message)


def aws(service, operation, parameters=None, region=REGION, imds=False):
    command = ["aws", service, operation, "--region", region, "--output", "json", "--no-cli-pager"]
    if parameters:
        command += ["--cli-input-json", json.dumps(parameters)]
    environment = {k: v for k, v in os.environ.items() if not k.startswith("AWS_ENDPOINT_URL")}
    environment.update(AWS_PAGER="", AWS_MAX_ATTEMPTS="1")
    if imds:
        environment = {k: v for k, v in environment.items() if not k.startswith("AWS_")}
        environment.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                           AWS_MAX_ATTEMPTS="1", AWS_PAGER="")
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=90)
    except (subprocess.TimeoutExpired, OSError):
        raise Error("AWS response unavailable; billing requests must be reconciled with status before retrying.") from None
    if result.returncode:
        code = re.search(r"An error occurred \(([^)]+)\)", result.stderr)
        raise Error("AWS " + operation + " failed" + (": " + code[1] if code else "; response uncertain"))
    try:
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        raise Error("AWS returned an unreadable response; run status before any new billing request.") from None


def metadata(call=aws):
    data = json.loads(call("ssm", "get-parameter", {"Name": PARAMETER}, "us-west-1")["Parameter"]["Value"])
    need(data.get("version") == 1 and data.get("region") == REGION and data.get("type") == TYPE
         and data.get("name") == data.get("purpose") == "macpbox" and data.get("lease_hours") == HOURS,
         "Unsupported Mac metadata; the control plane must publish version 1.")
    need(re.fullmatch(r"lt-[0-9a-f]+", data.get("launch_template_id", ""))
         and data.get("ssh_user") == "ec2-user" and isinstance(data.get("admin_role_arns"), list)
         and data["admin_role_arns"], "Incomplete Mac metadata.")
    return data


def administrator(caller, data):
    arn = caller["Arn"]
    for role in data["admin_role_arns"]:
        need(re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+", role), "Invalid administrator role metadata.")
        account = role.split(":")[4]
        if arn == role or (caller["Account"] == account and arn.startswith(
                "arn:aws:sts::" + account + ":assumed-role/" + role.rsplit("/", 1)[-1] + "/")):
            return True
    return False


def primary_executor(caller, call):
    def native(service, operation, parameters=None, region=REGION):
        return call(service, operation, parameters, region, imds=True)
    identity = native("sts", "get-caller-identity", region="us-west-1")
    need(identity == caller, "Paid launch requires the administration instance's own IMDS credentials.")
    identifier = identity["Arn"].rsplit("/", 1)[-1]
    need(re.fullmatch(r"i-[0-9a-f]{8,17}", identifier), "Paid launch requires a primary-jumpbox instance session.")
    found = native("ec2", "describe-instances", {"InstanceIds": [identifier]}, "us-west-1")["Reservations"]
    instances = [item for reservation in found for item in reservation["Instances"]]
    need(len(instances) == 1 and instances[0]["InstanceId"] == identifier
         and tags(instances[0]).get("Name") == "jumpbox" and instances[0]["State"]["Name"] == "running",
         "Only the primary jumpbox can make paid Mac requests. Backup/dev machines may read or reuse an active Mac.")
    return native


def tags(item):
    return {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}


def owned(item, host=False):
    value = tags(item)
    need(value.get("Name") == value.get("Purpose") == "macpbox" and value.get("LeaseHours") == "24",
         "An existing Mac has unexpected ownership tags; nothing will be allocated.")
    try:
        lease = uuid.UUID(value["LeaseId"])
        need(lease.version == 4 and str(lease) == value["LeaseId"], "Invalid Mac lease identity.")
    except (KeyError, ValueError):
        raise Error("Invalid Mac lease identity.") from None
    if host:
        properties = item.get("HostProperties", {})
        need(properties.get("InstanceType") == TYPE or properties.get("InstanceFamily") == "mac-m4pro",
             "Existing host is not the managed M4 Pro family.")
    else:
        need(item.get("InstanceType") == TYPE and item.get("Placement", {}).get("Tenancy") == "host",
             "Existing instance is not the managed dedicated Mac.")
    return str(lease)


def inventory(call=aws):
    filters = [{"Name": "tag:Name", "Values": ["macpbox"]}, {"Name": "tag:Purpose", "Values": ["macpbox"]}]
    hosts = [h for h in call("ec2", "describe-hosts", {"Filter": filters}).get("Hosts", [])
             if not h.get("State", "").startswith("released")]
    reservations = call("ec2", "describe-instances", {"Filters": filters}).get("Reservations", [])
    instances = [i for r in reservations for i in r["Instances"] if i["State"]["Name"] != "terminated"]
    need(len(hosts) <= 1 and len(instances) <= 1, "Multiple managed Macs found; refusing to create, replace or choose one.")
    for host in hosts:
        owned(host, True)
    for instance in instances:
        lease = owned(instance)
        need(len(hosts) == 1 and lease == owned(hosts[0], True)
             and instance["Placement"].get("HostId") == hosts[0]["HostId"], "Mac instance/host lease mismatch.")
    return hosts, instances


def deadline(host):
    allocated = dt.datetime.fromisoformat(host["AllocationTime"].replace("Z", "+00:00"))
    need(allocated.tzinfo is not None, "Host allocation time has no timezone.")
    return (allocated + dt.timedelta(hours=HOURS)).isoformat()


def template(data, call=aws):
    versions = call("ec2", "describe-launch-template-versions", {
        "LaunchTemplateId": data["launch_template_id"], "Versions": ["$Latest"]})["LaunchTemplateVersions"]
    need(len(versions) == 1, "Launch template version is ambiguous.")
    version = versions[0]
    value = version["LaunchTemplateData"]
    need(value.get("InstanceType") == TYPE and value.get("Placement", {}).get("Tenancy") == "host"
         and not value["Placement"].get("HostId"), "Unexpected Mac launch template placement/type.")
    interfaces = value.get("NetworkInterfaces", [])
    need(len(interfaces) == 1 and interfaces[0].get("DeviceIndex") == 0
         and interfaces[0].get("AssociatePublicIpAddress") is True and interfaces[0].get("Groups")
         and interfaces[0].get("DeleteOnTermination") is True and interfaces[0].get("SubnetId"),
         "The Mac template must own its complete public network interface.")
    return version


def quote(call=aws):
    filters = [{"Type": "TERM_MATCH", "Field": key, "Value": value} for key, value in (
        ("instanceType", "mac-m4pro"), ("tenancy", "Host"), ("operatingSystem", "NA"),
        ("location", "US West (Oregon)"))]
    products = call("pricing", "get-products", {"ServiceCode": "AmazonEC2", "Filters": filters}, "us-east-1")
    prices = set()
    for encoded in products.get("PriceList", []):
        product = json.loads(encoded)
        attributes = product["product"]["attributes"]
        need(attributes.get("instanceType") == "mac-m4pro" and attributes.get("tenancy") == "Host"
             and attributes.get("operatingSystem") == "NA", "Pricing returned an instance SKU instead of a dedicated host.")
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dimension in term["priceDimensions"].values():
                need(dimension.get("unit") == "Hrs", "Unexpected dedicated-host billing unit.")
                price = Decimal(dimension["pricePerUnit"]["USD"])
                need(price > 0, "Zero-dollar EC2 instance pricing is not the dedicated-host price.")
                prices.add(price)
    need(len(prices) == 1, "No unambiguous live dedicated-host price; no allocation is permitted.")
    price = prices.pop()
    return {"hourly_host_usd": str(price), "minimum_24h_host_usd": str(price * HOURS),
            "additional_charges": "EBS, public IPv4 and data transfer are additional.",
            "commitment": "The 24-hour minimum starts at AllocateHosts, even if boot fails. Stop does not release the host."}


def preflight(data, call=aws):
    version = template(data, call)
    value = version["LaunchTemplateData"]
    images = call("ec2", "describe-images", {"ImageIds": [value["ImageId"]], "Owners": ["amazon"]})["Images"]
    need(len(images) == 1 and images[0]["State"] == "available" and images[0]["Architecture"] == "arm64_mac",
         "The pinned Amazon Mac AMI is not available/ARM64.")
    subnet = call("ec2", "describe-subnets", {"SubnetIds": [value["NetworkInterfaces"][0]["SubnetId"]]})["Subnets"]
    need(len(subnet) == 1, "Mac subnet is ambiguous.")
    zone = subnet[0]["AvailabilityZone"]
    offers = call("ec2", "describe-instance-type-offerings", {"LocationType": "availability-zone", "Filters": [
        {"Name": "instance-type", "Values": [TYPE]}, {"Name": "location", "Values": [zone]}]})["InstanceTypeOfferings"]
    need(offers, "M4 Pro is not offered in the configured subnet's availability zone.")
    quotas = call("service-quotas", "list-service-quotas", {"ServiceCode": "ec2"})["Quotas"]
    limit = [q["Value"] for q in quotas if "mac-m4pro" in q["QuotaName"].lower() and "dedicated" in q["QuotaName"].lower()]
    hosts = call("ec2", "describe-hosts").get("Hosts", [])
    used = sum(not h.get("State", "").startswith("released") and (
        h.get("HostProperties", {}).get("InstanceType") == TYPE or
        h.get("HostProperties", {}).get("InstanceFamily") == "mac-m4pro") for h in hosts)
    need(len(limit) == 1 and limit[0] >= used + 1, "Dedicated M4 Pro host quota is unavailable/exhausted.")
    return version, zone


class LeaseLock:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path.home() / ".local/state/macpbox")

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        need(not self.directory.is_symlink() and self.directory.stat().st_uid == os.getuid(), "Unsafe Mac lease directory.")
        self.fd = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def read(self):
        path = self.directory / "pending.json"
        need(not path.is_symlink(), "Unsafe Mac lease journal.")
        return json.loads(path.read_text()) if path.exists() else None

    def write(self, value):
        temporary = self.directory / "pending.new"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.directory / "pending.json")
        finally:
            temporary.unlink(missing_ok=True)

    def __exit__(self, *args):
        os.close(self.fd)


def summary(hosts, instances):
    return {"hosts": [{"id": h["HostId"], "state": h["State"], "earliest_release": deadline(h)} for h in hosts],
            "instances": [{"id": i["InstanceId"], "state": i["State"]["Name"], "public_ip": i.get("PublicIpAddress")}
                          for i in instances], "note": "Stopped Macs still incur the dedicated-host charge until the host is released."}


def confirm_previous_release(lock, account, call=aws):
    previous = lock.read()
    if not previous:
        return None
    proof = previous.get("release_confirmation", {})
    if (proof.get("account") == account and proof.get("region") == REGION
            and proof.get("host_id") == previous.get("host_id") and proof.get("observed_at")):
        return True
    if not previous.get("host_id"):
        return False
    try:
        old = call("ec2", "describe-hosts", {"HostIds": [previous["host_id"]]}).get("Hosts", [])
    except Error:
        return False  # Missing history, wrong account/region, and API errors are not release proof.
    if (len(old) != 1 or old[0].get("HostId") != previous["host_id"]
            or old[0].get("State") not in ("released", "released-permanent-failure")):
        return False
    previous["release_confirmation"] = {
        "account": account, "region": REGION, "host_id": previous["host_id"],
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    lock.write(previous)
    return True


def up(data, caller, approved=False, cap=None, call=aws, directory=None):
    hosts, instances = inventory(call)
    if hosts or instances:
        return {"reused_without_mutation": True, **summary(hosts, instances)}
    need(administrator(caller, data), "Only the administration control plane can allocate a Mac. Ask the owner to run macpbox up on a jumpbox; dev boxes cannot delegate this. Use ssh/workspace for an existing Mac.")
    with LeaseLock(directory) as lock:
        hosts, instances = inventory(call)
        if hosts or instances:
            return {"reused_without_mutation": True, **summary(hosts, instances)}
        need(confirm_previous_release(lock, caller["Account"], call) is not False,
             "Previous Mac release is unconfirmed. Run macpbox status on the primary jumpbox while released history exists. "
             "If history expired, an administration agent must verify successful ReleaseHosts in CloudTrail before reconciling "
             "the journal. Never delete it or infer release from elapsed time.")
        price = quote(call)
        if not approved or cap is None:
            return {"approval_required": True, **price,
                    "next": "Obtain the owner's approval, then use --approved-minimum-24h --approved-usd-hour CAP."}
        need(Decimal(cap).is_finite() and Decimal(cap) >= Decimal(price["hourly_host_usd"]), "Live host price exceeds the approved hourly cap.")
        call = primary_executor(caller, call)
        version, zone = preflight(data, call)
        lease = str(uuid.uuid4())
        journal = {"lease": lease, "phase": "allocate_requested"}
        lock.write(journal)
        value = [{"Key": key, "Value": val} for key, val in (
            ("Name", "macpbox"), ("Purpose", "macpbox"), ("LeaseId", lease), ("LeaseHours", "24"))]
        try:
            allocated = call("ec2", "allocate-hosts", {"AvailabilityZone": zone, "InstanceType": TYPE,
                "Quantity": 1, "AutoPlacement": "off", "ClientToken": "macpbox-host-" + lease,
                "TagSpecifications": [{"ResourceType": "dedicated-host", "Tags": value}]})
            need(len(allocated.get("HostIds", [])) == 1, "Allocation reply is ambiguous; run status, never allocate another host.")
            journal.update(host_id=allocated["HostIds"][0], phase="allocation_returned")
            lock.write(journal)
            known = call("ec2", "describe-hosts", {"HostIds": [journal["host_id"]]})["Hosts"]
            need(len(known) == 1 and owned(known[0], True) == lease, "Allocated host identity is not yet verifiable; run status.")
            journal.update(allocation_time=known[0]["AllocationTime"], earliest_release=deadline(known[0]), phase="run_requested")
            lock.write(journal)
            launched = call("ec2", "run-instances", {"MinCount": 1, "MaxCount": 1, "ClientToken": "macpbox-run-" + lease,
                "LaunchTemplate": {"LaunchTemplateId": data["launch_template_id"], "Version": str(version["VersionNumber"])},
                "Placement": {"Tenancy": "host", "HostId": journal["host_id"]},
                "TagSpecifications": [{"ResourceType": kind, "Tags": value} for kind in ("instance", "volume")]})
            need(len(launched.get("Instances", [])) == 1, "Instance reply is ambiguous; run status.")
            journal["instance_id"] = launched["Instances"][0]["InstanceId"]
        except Exception:
            raise Error("Mac billing request did not complete clearly. No automatic retry/replacement. Run macpbox status; the 24-hour host commitment may already have started.") from None
        journal["phase"] = "complete"
        lock.write(journal)
        return {"launched": True, **summary(known, launched["Instances"]), **price}


def ssh_command(data, caller, call=aws):
    hosts, instances = inventory(call)
    need(len(hosts) == len(instances) == 1 and instances[0]["State"]["Name"] == "running", "No verified running shared Mac. Ask the owner; this command never starts one.")
    instance = instances[0]
    address = instance.get("PublicIpAddress", "")
    need(ipaddress.ip_address(address).version == 4 and ipaddress.ip_address(address).is_global, "Mac has no public IPv4 address.")
    identity = data["admin_identity_file" if administrator(caller, data) else "dev_identity_file"]
    return ["ssh", "-i", os.path.expanduser(identity), "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "HostKeyAlias=macpbox-" + instance["InstanceId"], "ec2-user@" + address]


WORKSPACE = r'''
import os,sys,json
agent=sys.argv[1]
import re
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}",agent)
flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
base=os.open("/Users/ec2-user",flags)
try:
    try: os.mkdir("work",0o700,dir_fd=base)
    except FileExistsError: pass
    work=os.open("work",flags,dir_fd=base)
    try:
        os.mkdir(agent,0o700,dir_fd=work)
        own=os.open(agent,flags,dir_fd=work)
        try:
            for name in ("repos","build","cache","tmp","bin"): os.mkdir(name,0o700,dir_fd=own)
            path="/Users/ec2-user/work/"+agent
            env="export TMPDIR='"+path+"/tmp'\nexport CARGO_HOME='"+path+"/cache/cargo'\nexport CARGO_TARGET_DIR='"+path+"/build/cargo'\nexport XDG_CACHE_HOME='"+path+"/cache'\nexport MACPBOX_DERIVED_DATA='"+path+"/build/DerivedData'\nexport PATH='"+path+"/bin':$PATH\n"
            fd=os.open("env",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=own)
            with os.fdopen(fd,"w") as out: out.write(env)
            binary=os.open("bin",flags,dir_fd=own)
            try:
                fd=os.open("xcodebuild",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o700,dir_fd=binary)
                with os.fdopen(fd,"w") as out: out.write("#!/bin/sh\nexec /usr/bin/xcodebuild -derivedDataPath '"+path+"/build/DerivedData' \"$@\"\n")
            finally: os.close(binary)
            print(json.dumps({"workspace":path,"source_environment":path+"/env"}))
        finally: os.close(own)
    finally: os.close(work)
finally: os.close(base)
'''


def workspace(identifier, data, caller, call=aws, run=subprocess.run):
    need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", identifier), "Agent ID must be 1–48 ASCII letters, digits, underscores or hyphens, starting with a letter/digit.")
    command = ssh_command(data, caller, call) + ["/usr/bin/python3 - " + shlex.quote(identifier)]
    result = run(command, input=WORKSPACE, text=True)
    need(result.returncode == 0, "Workspace refused: ID already exists, a path is a symlink, or remote Python/SSH is unavailable. No global settings were changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("quote")
    create = sub.add_parser("up")
    create.add_argument("--approved-minimum-24h", action="store_true")
    create.add_argument("--approved-usd-hour", type=Decimal)
    sub.add_parser("ssh")
    work = sub.add_parser("workspace")
    work.add_argument("agent_id")
    args = parser.parse_args()
    data = metadata()
    caller = aws("sts", "get-caller-identity", region="us-west-1")
    if args.command == "quote":
        result = quote()
    elif args.command == "up":
        result = up(data, caller, args.approved_minimum_24h, args.approved_usd_hour)
    elif args.command == "status":
        result = summary(*inventory())
        result["ami"] = template(data)["LaunchTemplateData"]["ImageId"]
        ids = [item["id"] for item in result["instances"]]
        result["ssm"] = aws("ssm", "describe-instance-information", {"Filters": [{"Key": "InstanceIds", "Values": ids}]})["InstanceInformationList"] if ids else []
        if administrator(caller, data):
            with LeaseLock() as lock:
                result["previous_lease_release_confirmed"] = confirm_previous_release(lock, caller["Account"])
    elif args.command == "workspace":
        workspace(args.agent_id, data, caller)
        return
    else:
        raise SystemExit(subprocess.run(ssh_command(data, caller)).returncode)
    print(json.dumps(result, indent=2))
    if result.get("approval_required"):
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except (Error, KeyError, ValueError, InvalidOperation) as error:
        print("macpbox: " + str(error), file=sys.stderr)
        raise SystemExit(1)
