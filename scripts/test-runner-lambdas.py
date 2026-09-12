#!/usr/bin/env python3
"""Exercise the runner Lambdas' launch and queue-scan logic against fakes.

Why this exists: both Lambdas live as inline Python inside `runner-autoscale.tf`,
so nothing runs them before they are deployed. On 2026-08-07 that cost hours of
queued CI -- the x86 launcher retried one instance type forever because AWS
reports a spot capacity failure asynchronously and the fallback loop only
advanced on an exception, and the queue poll sampled five workflow runs when six
undrainable phantom runs were sitting at the head of the list.

This extracts the real heredoc bodies out of `runner-autoscale.tf` rather than
keeping a copy that can drift, and runs them with boto3, urllib and the clock
replaced by fakes. No AWS or GitHub credentials, and no network.

Run from the repo root:  python3 scripts/test-runner-lambdas.py
Exit code 1 if any case fails.
"""
import contextlib
import base64
import gzip
import io
import json
import os
import re
import sys
import textwrap
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

TF_FILE = Path(__file__).resolve().parent.parent / "runner-autoscale.tf"
NOW = datetime(2026, 8, 7, 20, 0, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "928413605543"
REGION = "us-west-1"


# --------------------------------------------------------------------------
# Loading the Lambda source out of the Terraform heredocs
# --------------------------------------------------------------------------

def extract_lambda_sources():
    """The two `content = <<-EOF ... EOF` bodies, in file order.

    Terraform strips the indentation of the least-indented line from a `<<-`
    heredoc, which is exactly what textwrap.dedent does, so the text handed to
    exec() here is byte-identical to the lambda_function.py that gets zipped.
    """
    lines = TF_FILE.read_text().splitlines()
    sources, body, collecting = [], [], False
    for line in lines:
        if collecting:
            if line.strip() == "EOF":
                sources.append(textwrap.dedent("\n".join(body)))
                body, collecting = [], False
            else:
                body.append(line)
        elif re.match(r"^\s*content\s*=\s*<<-EOF\s*$", line):
            collecting = True
    return sources


class FakeEC2:
    """Just enough EC2 to drive the launcher and the reaper."""

    def __init__(self, instances=(), run_instances_errors=None, terminate_error=None,
                 lookup_error=None, journal=None, create_tags_error=None,
                 create_tags_reject_keys=None):
        self.instances = list(instances)
        self.run_instances_errors = run_instances_errors or {}
        # CreateTags rejected. With no reject_keys every write fails; with them,
        # only a write whose keys all fall inside the set fails, which models
        # the standalone RunnerSeenAt stamp failing while a LeaseExpires
        # renewal still lands (two calls, two fates).
        self.create_tags_error = create_tags_error
        self.create_tags_reject_keys = set(create_tags_reject_keys or ())
        # TerminateInstances rejected: an IAM change, DisableApiTermination, a
        # transient 5xx. The instance stays alive, so a poll that reports success
        # here would be claiming the fleet's lifetime bound took effect when it
        # did not.
        self.terminate_error = terminate_error
        # DescribeInstances(InstanceIds=...) failing. Distinct from "no such
        # instance": one is "could not ask", the other is "it is gone", and the
        # orphan phase deregisters on the second.
        self.lookup_error = lookup_error
        # Optional list shared with FakeGitHub, so the ORDER of AWS and GitHub work
        # within one invocation can be asserted. Phase order is a safety property
        # here: a Lambda timeout is not catchable, so anything unbounded that runs
        # ahead of the age sweep can stop the sweep happening at all.
        self.journal = journal if journal is not None else []
        self.calls = []

    def _matches(self, instance, filters):
        for f in filters:
            name, values = f["Name"], f["Values"]
            if name == "instance-state-name":
                if instance["State"]["Name"] not in values:
                    return False
            elif name.startswith("tag:"):
                key = name.split(":", 1)[1]
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                if tags.get(key) not in values:
                    return False
        return True

    def describe_instances(self, **kw):
        self.calls.append(("describe_instances", kw))
        if "InstanceIds" in kw and self.lookup_error:
            raise self.lookup_error
        matched = [i for i in self.instances if self._matches(i, kw.get("Filters", []))]
        # get_instance_state() looks an instance up by id with no filters. Without
        # honouring InstanceIds every lookup would answer with the first instance in
        # the list, so a case about one runner would silently be answered by another.
        if "InstanceIds" in kw:
            matched = [i for i in matched if i["InstanceId"] in kw["InstanceIds"]]
        return {"Reservations": [{"Instances": matched}] if matched else []}

    def describe_images(self, **kw):
        self.calls.append(("describe_images", kw))
        return {"Images": [{"ImageId": "ami-test", "CreationDate": "2026-08-01T00:00:00Z"}]}

    def run_instances(self, **kw):
        self.calls.append(("run_instances", kw))
        self.journal.append(f"ec2:run_instances:{kw['InstanceType']}")
        error = self.run_instances_errors.get(kw["InstanceType"])
        if error:
            raise Exception(error)
        return {"Instances": [{"InstanceId": f"i-new-{kw['InstanceType']}"}]}

    def create_tags(self, **kw):
        self.calls.append(("create_tags", kw))
        keys = {t["Key"] for t in kw["Tags"]}
        self.journal.append(f"ec2:create_tags:{','.join(kw['Resources'])}:{','.join(sorted(keys))}")
        if self.create_tags_error and (not self.create_tags_reject_keys
                                       or keys <= self.create_tags_reject_keys):
            raise self.create_tags_error
        # An accepted CreateTags has to be visible to the NEXT describe_instances,
        # for the same reason an accepted terminate has to move the instance: a
        # case that spans two polls would otherwise assert against a world in
        # which nothing the Lambda wrote ever happened. Both LeaseExpires and
        # RunnerSeenAt are written on one poll and read back on a later one.
        for inst in self.instances:
            if inst.get("InstanceId") in kw["Resources"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                tags.update({t["Key"]: t["Value"] for t in kw["Tags"]})
                inst["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]

    def terminate_instances(self, **kw):
        self.calls.append(("terminate_instances", kw))
        self.journal.append(f"ec2:terminate:{','.join(kw['InstanceIds'])}")
        if self.terminate_error:
            raise self.terminate_error
        # An accepted terminate MOVES the instance. Leaving it 'running' let a case
        # assert an outcome that only held because the fake froze the world: later
        # phases in the same poll see 'shutting-down' in reality, not 'running'.
        for inst in self.instances:
            if inst.get("InstanceId") in kw["InstanceIds"]:
                inst["State"] = {"Name": "shutting-down"}

    def ops(self, name):
        return [kw for op, kw in self.calls if op == name]


class FakeClientError(Exception):
    """Shaped like botocore's ClientError, which carries its code in .response.

    The Lambda cannot import botocore (it is present in the runtime, but the
    handler only ever sees the exception), so it reads the code off the object.
    """

    def __init__(self, code, message="fake"):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeDynamoDB:
    """Registration rows with conditional-create semantics.

    Every accepted write changes `items`, and every read answers from it, so a
    case asserts the row that exists after the poll rather than the calls the
    Lambda made. `put_error` rejects the write without landing it (a throttle,
    a permissions change); `write_then_error` lands it and then raises, which
    is a PutItem whose answer was lost on the wire; `concurrent_item` is a row
    another writer creates in the instant before this PutItem, so the
    conditional create fails the way it does when bootstrap wins the race.
    """

    def __init__(self, items=(), put_error=None, write_then_error=False,
                 concurrent_item=None, get_error=None):
        self.items = {}
        for item in items:
            self.items[item["InstanceArn"]["S"]] = item
        self.put_error = put_error
        self.write_then_error = write_then_error
        self.concurrent_item = concurrent_item
        self.get_error = get_error
        self.calls = []

    def get_item(self, **kw):
        self.calls.append(("get_item", kw))
        assert kw.get("ConsistentRead") is True, kw
        if self.get_error:
            raise self.get_error
        item = self.items.get(kw["Key"]["InstanceArn"]["S"])
        return {"Item": item} if item is not None else {}

    def put_item(self, **kw):
        self.calls.append(("put_item", kw))
        arn = kw["Item"]["InstanceArn"]["S"]
        if self.concurrent_item is not None:
            concurrent = self.concurrent_item
            self.concurrent_item = None
            self.items[concurrent["InstanceArn"]["S"]] = concurrent
        if kw.get("ConditionExpression") == "attribute_not_exists(InstanceArn)" and arn in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        if self.write_then_error:
            self.items[arn] = kw["Item"]
            raise self.put_error or OSError("PutItem outcome unknown")
        if self.put_error:
            raise self.put_error
        self.items[arn] = kw["Item"]
        return {}

    def ops(self, name):
        return [kw for op, kw in self.calls if op == name]


class FakeSSM:
    def __init__(self, pat="ghp_test", journal=None, put_error=None, user_data=None,
                 parameters=(), parameter_tags=None, metadata_error=None,
                 tag_errors=None, delete_errors=None, metadata_pages=None):
        self.pat = pat
        self.journal = journal if journal is not None else []
        self.put_error = put_error
        self.puts = []
        self.deletes = []
        self.parameters = list(parameters)
        self.parameter_tags = parameter_tags or {}
        self.metadata_error = metadata_error
        self.tag_errors = tag_errors or {}
        self.delete_errors = delete_errors or {}
        self.metadata_pages = metadata_pages
        self.metadata_calls = []
        self.tag_calls = []
        self.user_data = user_data if user_data is not None else base64.b64encode(gzip.compress(
            b'#!/bin/bash\nREGISTRATION_TABLE="github-runner-registration"\n'
            b'BOOTSTRAP_PARAM="/github-runner/bootstrap/$INSTANCE_ID"\n'
        )).decode()

    def get_parameter(self, **kw):
        self.journal.append(f"ssm:get_parameter:{kw['Name']}")
        if kw["Name"].endswith("/pat"):
            return {"Parameter": {"Value": self.pat}}
        return {"Parameter": {"Value": self.user_data}}

    def put_parameter(self, **kw):
        self.journal.append(f"ssm:put_parameter:{kw['Name']}")
        self.puts.append(kw)
        if self.put_error:
            raise self.put_error
        return {"Version": 1}

    def delete_parameter(self, **kw):
        self.journal.append(f"ssm:delete_parameter:{kw['Name']}")
        self.deletes.append(kw)
        if kw['Name'] in self.delete_errors:
            raise self.delete_errors[kw['Name']]
        return {}

    def describe_parameters(self, **kw):
        self.journal.append('ssm:describe_parameters')
        self.metadata_calls.append(kw)
        if self.metadata_error:
            raise self.metadata_error
        if self.metadata_pages is not None:
            return self.metadata_pages[min(len(self.metadata_calls) - 1, len(self.metadata_pages) - 1)]
        return {"Parameters": self.parameters}

    def list_tags_for_resource(self, **kw):
        self.journal.append(f"ssm:list_tags:{kw['ResourceId']}")
        self.tag_calls.append(kw)
        if kw['ResourceId'] in self.tag_errors:
            raise self.tag_errors[kw['ResourceId']]
        return {"TagList": [{"Key": k, "Value": v} for k, v in self.parameter_tags.get(kw['ResourceId'], {}).items()]}


class FakeLambdaClient:
    def __init__(self):
        self.invokes = []

    def invoke(self, **kw):
        self.invokes.append(json.loads(json.loads(kw["Payload"])["body"]))

    def arch_invokes(self):
        """Total runners REQUESTED per architecture.

        The cleanup poll sends one invocation per architecture whose payload
        carries the whole deficit as launch_count (a burst of single-launch
        invocations could overshoot MAX_RUNNERS: DescribeInstances is eventually
        consistent, so each invocation can miss the instance the previous one
        just launched). Summing launch_count keeps these assertions about
        demand, and invocations_per_arch() below asserts the one-invoke shape.
        """
        counts = {}
        for payload in self.invokes:
            labels = [x.lower() for x in payload["workflow_job"]["labels"]]
            arch = "x86_64" if "x64" in labels else "arm64"
            counts[arch] = counts.get(arch, 0) + payload.get("launch_count", 1)
        return counts

    def invocations_per_arch(self):
        counts = {}
        for payload in self.invokes:
            labels = [x.lower() for x in payload["workflow_job"]["labels"]]
            arch = "x86_64" if "x64" in labels else "arm64"
            counts[arch] = counts.get(arch, 0) + 1
        return counts


class FakeGitHub:
    """Routes GitHub REST URLs to canned JSON, honouring status and page."""

    def __init__(self, runs=(), jobs=None, runners=(), runners_error=False,
                 delete_error=False, recheck=None, journal=None, runners_total=None,
                 omit_runners_total=False, runner_payloads=None,
                 registration_error=False, registration_payload=None):
        self.runs = list(runs)
        self.jobs = jobs or {}
        self.runners = list(runners)
        # Every /actions/runners call raises: a PAT that lost its scope, a 5xx, a
        # rate limit. get_runners() turns it into an unread roster, so the reaper
        # has no record for any instance - the state a decision must survive.
        self.runners_error = runners_error
        # A deregistration that fails must never stop a termination.
        self.delete_error = delete_error
        # What GET /actions/runners/{id} answers, when that must differ from the
        # list this poll already read: {runner_id: record or None-to-fail}.
        self.recheck = recheck or {}
        # What the roster CLAIMS is registered, when that must differ from what it
        # hands back: a page shorter than total_count is a truncated read, and it
        # is indistinguishable from "that runner is not registered" to anything
        # that does not compare the two.
        self.runners_total = runners_total
        # The response carries no total_count key at all, so completeness
        # cannot be checked against anything.
        self.omit_runners_total = omit_runners_total
        # Exact successive /actions/runners payloads, when pagination itself is
        # the input under test: a roster that changes between two complete
        # reads, or a page boundary that shifts under a concurrent registration.
        # Once exhausted, the last payload keeps answering, so a later phase
        # does not get an unrelated fake failure.
        self.runner_payloads = list(runner_payloads) if runner_payloads is not None else None
        self.runner_payload_index = 0
        self.journal = journal if journal is not None else []
        self.requests = []
        self.deletes = []
        self.registration_error = registration_error
        self.registration_payload = registration_payload

    def _payload(self, req):
        url = getattr(req, "url", req)
        method = getattr(req, "method", None)
        self.requests.append(url)
        self.journal.append(f"github:{method or 'GET'}:{url.split('/repos/ejc3/fcvm')[-1]}")
        if method == "POST" and url.endswith('/actions/runners/registration-token'):
            if self.registration_error:
                raise OSError("GitHub registration refused")
            if self.registration_payload is not None:
                return self.registration_payload
            return {"token": "registration-token", "expires_at": (NOW + timedelta(hours=1)).isoformat()}
        if method == "DELETE":
            self.deletes.append(url)
            if self.delete_error:
                raise OSError("GitHub refused the deregistration")
            # Deregistration REMOVES the runner, and a second DELETE for the same id
            # 404s. Without that, a case could not tell "cleaned one orphan" from
            # "deregistered the same runner twice".
            gone = int(url.rsplit("/", 1)[-1])
            before = len(self.runners)
            self.runners = [r for r in self.runners if r.get("id") != gone]
            if len(self.runners) == before:
                raise OSError("HTTP Error 404: Not Found")
            return {}
        page_match = re.search(r"[?&]page=(\d+)", url)
        per_page_match = re.search(r"per_page=(\d+)", url)
        page = int(page_match.group(1)) if page_match else 1
        per_page = int(per_page_match.group(1)) if per_page_match else 30
        one_runner = re.search(r"/actions/runners/(\d+)$", url)
        if one_runner:
            record = self.recheck.get(int(one_runner.group(1)), "unset")
            if record == "unset":
                record = next((r for r in self.runners
                               if r.get("id") == int(one_runner.group(1))), None)
            if record is None:
                raise OSError("GitHub is unreachable")
            return record
        if "/actions/runners" in url:
            if self.runners_error:
                raise OSError("GitHub is unreachable")
            if self.runner_payloads:
                index = min(self.runner_payload_index, len(self.runner_payloads) - 1)
                self.runner_payload_index += 1
                return self.runner_payloads[index]
            window = self.runners[(page - 1) * per_page: page * per_page]
            if self.omit_runners_total:
                return {"runners": window}
            total = len(self.runners) if self.runners_total is None else self.runners_total
            return {"total_count": total, "runners": window}
        jobs_match = re.search(r"/actions/runs/(\d+)/jobs", url)
        if jobs_match:
            jobs = self.jobs.get(int(jobs_match.group(1)), [])
            window = jobs[(page - 1) * per_page: page * per_page]
            return {"total_count": len(jobs), "jobs": window}
        status_match = re.search(r"[?&]status=(\w+)", url)
        if not status_match:
            # Fail LOUDLY: the Lambdas wrap GitHub calls in broad except blocks,
            # so a silently unrouted URL would read as "no demand" and pass.
            raise ValueError(f"FakeGitHub has no route for {url}")
        status = status_match.group(1)
        matched = [r for r in self.runs if r["status"] == status]
        window = matched[(page - 1) * per_page: page * per_page]
        return {"total_count": len(matched), "workflow_runs": window}

    def as_urllib(self):
        github = self

        class Response:
            def __init__(self, data):
                self.data = json.dumps(data).encode()

            def read(self, limit=None):
                return self.data if limit is None else self.data[:limit]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        # Request objects carry the method so a DELETE (deregistration) is
        # distinguishable from a GET at the point urlopen is actually called.
        class Req:
            def __init__(self, url, method=None):
                self.url = url
                self.method = method

        module = types.SimpleNamespace()
        module.request = types.SimpleNamespace(
            Request=lambda url, **kw: Req(url, kw.get("method")),
            urlopen=lambda req, **kw: Response(github._payload(req)),
        )
        return module


_BASE_ENV = dict(os.environ)



def capture_emf(emit, **kwargs):
    """Run emit_decision and return the EMF dict it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emit(**kwargs)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def load_lambda(source, ec2, ssm, lambda_client=None, github=None, env=None, now=NOW,
                dynamodb=None):
    """exec the Lambda source with its AWS and GitHub edges replaced.

    os.environ is reset to the process baseline first: exec does not isolate
    imports, so the env vars below land in the REAL environment and would
    otherwise leak between cases (a WEBHOOK_SECRET set by one case must not
    still be set for the next).
    """
    os.environ.clear()
    os.environ.update(_BASE_ENV)
    clients = {"ec2": ec2, "ssm": ssm, "lambda": lambda_client,
               "dynamodb": dynamodb or FakeDynamoDB()}
    fake_boto3 = types.ModuleType("boto3")
    client_options = []
    def client(service, **kw):
        client_options.append((service, kw))
        return clients[service]
    fake_boto3.client = client
    sys.modules["boto3"] = fake_boto3
    fake_botocore = types.ModuleType("botocore")
    fake_config = types.ModuleType("botocore.config")
    fake_config.Config = lambda **kw: types.SimpleNamespace(**kw)
    fake_botocore.config = fake_config
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.config"] = fake_config

    namespace = {"__name__": "lambda_function"}
    exec(compile(source, "<lambda_function.py>", "exec"), namespace)
    namespace['_test_client_options'] = client_options

    if github is not None:
        namespace["urllib"] = github.as_urllib()
    namespace["os"].environ.update({
        "SUBNET_ID": "subnet-test",
        "SECURITY_GROUP_ID": "sg-test",
        "INSTANCE_PROFILE": "runner-profile",
        "USER_DATA_PARAM": "/github-runner/user-data",
        "WEBHOOK_FUNCTION": "github-runner-webhook",
        "MAX_RUNNERS": "4",
        "REGISTRATION_TABLE": "github-runner-registration",
        "RUNNER_ACCOUNT_ID": ACCOUNT_ID,
        **(env or {}),
    })

    # Freeze the clock so "stalled for 20 minutes" is a fact, not a race.
    #
    # The tz argument is honoured rather than ignored: every Lambda call site
    # passes timezone.utc, and one that stopped would start comparing a naive
    # clock against boto3's aware LaunchTime. A fake that returns an aware value
    # either way hides that, so `now()` with no tz returns naive here exactly as
    # the real datetime does.
    class FixedDatetime(namespace["datetime"]):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    namespace["datetime"] = FixedDatetime
    return namespace


def instance(instance_id, instance_type, state, age_minutes, arch="x86_64", tags=None,
             state_reason=None):
    all_tags = {"Role": "github-runner", "Name": f"github-runner-{arch}"}
    all_tags.update(tags or {})
    record = {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "State": {"Name": state},
        "LaunchTime": NOW - timedelta(minutes=age_minutes),
        "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()],
    }
    if state_reason:
        record["StateReason"] = {"Code": state_reason}
    return record


def run(run_id, status):
    return {"id": run_id, "status": status}


def job(status, arch, name="job"):
    labels = ["self-hosted", "Linux", "X64" if arch == "x86_64" else "ARM64"]
    return {"name": name, "status": status, "labels": labels}


def instance_arn(instance_id):
    return f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/{instance_id}"


def registered_item(instance_id="i-lease", runner_id=77, runner_name=None):
    """One registration claim as user data writes it."""
    name = runner_name or f"runner-{instance_id}"
    return {
        "InstanceArn": {"S": instance_arn(instance_id)},
        "State": {"S": "registered"},
        "InstanceId": {"S": instance_id},
        "RunnerName": {"S": name},
        "RunnerId": {"N": str(runner_id)},
        "RegisteredAt": {"S": NOW.isoformat()},
    }


def reaping_item(instance_id="i-lease"):
    """One cleanup-owned claim, after which bootstrap can no longer start the service."""
    return {
        "InstanceArn": {"S": instance_arn(instance_id)},
        "State": {"S": "reaping"},
        "InstanceId": {"S": instance_id},
        "ReapingAt": {"S": NOW.isoformat()},
    }


# --------------------------------------------------------------------------
# Cases: the x86 spot fallback
# --------------------------------------------------------------------------

def webhook(ec2, **kw):
    kw.setdefault("github", FakeGitHub())
    return load_lambda(WEBHOOK_SRC, ec2, kw.pop("ssm", FakeSSM()), **kw)


def launched_types(ec2):
    return [kw["InstanceType"] for kw in ec2.ops("run_instances")]


def type_order(arch="x86_64"):
    """The launcher's own list, so these cases follow it instead of pinning it.

    Pinned copies went stale the moment the storeless types were removed.
    """
    return webhook(FakeEC2([]))["get_instance_types"](arch)


def case_aws_capacity_verdict_advances_the_type():
    """AWS's own state reason on a dead instance moves the launcher along."""
    ec2 = FakeEC2([instance("i-dead", "c5d.metal", "terminated", 60,
                            state_reason="Server.InsufficientInstanceCapacity")])
    webhook(ec2)["launch_runner"]("x86_64")
    assert launched_types(ec2) == [type_order()[1]], launched_types(ec2)


def case_stalled_pending_launch_advances_the_type():
    """A launch still pending past the startup window is a capacity failure."""
    ec2 = FakeEC2([instance("i-stuck", "c5d.metal", "pending", 20)])
    webhook(ec2)["launch_runner"]("x86_64")
    assert launched_types(ec2) == [type_order()[1]], launched_types(ec2)


def case_capacity_failed_tag_advances_the_type():
    """The tag the cleanup Lambda stamps survives the instance's termination."""
    ec2 = FakeEC2([instance("i-reaped", "c5d.metal", "terminated", 30,
                            tags={"CapacityFailedAt": NOW.isoformat()})])
    webhook(ec2)["launch_runner"]("x86_64")
    assert launched_types(ec2) == [type_order()[1]], launched_types(ec2)


def case_pending_inside_the_startup_window_is_not_a_failure():
    """A metal instance that is merely still booting must not rotate the list."""
    ec2 = FakeEC2([instance("i-booting", "c5d.metal", "pending", 5)])
    webhook(ec2)["launch_runner"]("x86_64")
    assert launched_types(ec2) == ["c5d.metal"], launched_types(ec2)


def case_every_type_failed_still_launches():
    """Deprioritising must never empty the list."""
    module = webhook(FakeEC2([]))
    every = module["get_instance_types"]("x86_64")
    dead = [instance(f"i-{t}", t, "terminated", 10, state_reason="Server.InsufficientInstanceCapacity")
            for t in every]
    ec2 = FakeEC2(dead)
    module = webhook(ec2)
    order = module["get_instance_types"]("x86_64", set(every))
    assert sorted(order) == sorted(every), order
    module["launch_runner"]("x86_64")
    assert launched_types(ec2) == [every[0]], launched_types(ec2)


def case_every_launchable_type_has_instance_storage():
    """A storeless type is unusable to THIS bootstrap, so do not launch one.

    user_data builds /mnt/fcvm-btrfs only from instance-store NVMe. fcvm itself
    does not need instance store (setup falls back to a loopback btrfs image,
    src/setup/storage.rs); what it requires is btrfs, for reflink CoW. So the
    constraint is the bootstrap's, and holds until user_data grows that
    fallback. On 2026-08-15 a c7g.metal spot instance came up, died in
    user_data with no disk to find, never registered, and billed while jobs
    queued -- while c5.metal and c6i.metal sat in the x86 list with the same
    defect.

    AWS marks instance storage with a 'd' in the family (c5d, m5d, c7gd,
    m6id). Confirm any addition for real with:
      aws ec2 describe-instance-types --instance-types <t> \
        --query 'InstanceTypes[].InstanceStorageSupported'
    """
    module = webhook(FakeEC2([]))
    for arch in ("x86_64", "arm64"):
        types = module["get_instance_types"](arch)
        assert types, f"{arch} has no launchable types"
        for t in types:
            family = t.split(".")[0]
            assert family.endswith("d") or "d" in family[2:], (
                f"{arch}: {t} has no instance storage; it would boot a runner "
                f"that cannot build /mnt/fcvm-btrfs"
            )


def case_every_new_runner_declares_the_registration_protocol():
    """Only a launch tagged with the handshake may be reaped through its row."""
    ec2 = FakeEC2()
    webhook(ec2)["launch_runner"]("arm64")
    calls = ec2.ops("run_instances")
    assert len(calls) == 1, calls
    instance_tags = next(spec["Tags"] for spec in calls[0]["TagSpecifications"]
                         if spec["ResourceType"] == "instance")
    tags = {tag["Key"]: tag["Value"] for tag in instance_tags}
    assert tags["RunnerRegistrationProtocol"] == "ddb-v1", tags


def case_controller_first_accepts_older_pat_script_without_unused_credential():
    for claims in (False, True):
        script = '#!/bin/bash\n' + ('REGISTRATION_TABLE="table"\n' if claims else '')
        script += 'PAT=$(aws ssm get-parameter --name /github-runner/pat)\n'
        ssm = FakeSSM(user_data=base64.b64encode(gzip.compress(script.encode())).decode())
        ec2 = FakeEC2()
        github = FakeGitHub()
        webhook(ec2, ssm=ssm, github=github)["launch_runner"]("arm64")
        assert not ssm.puts, "old script leaves an unused bootstrap credential"
        assert not any("registration-token" in url for url in github.requests)
        tags = {t["Key"]: t["Value"] for t in ec2.ops("run_instances")[0]["TagSpecifications"][0]["Tags"]}
        assert (tags.get("RunnerRegistrationProtocol") == "ddb-v1") == claims, tags


def case_invalid_or_oversized_user_data_cannot_allocate_runner():
    invalid = ["not-base64!", base64.b64encode(b"not a bash script").decode(),
               base64.b64encode(gzip.compress(b"#!/bin/bash\n" + b"x" * 262144)).decode(),
               base64.b64encode(gzip.compress(b'#!/bin/bash\nBOOTSTRAP_PARAM="/github-runner/bootstrap/$INSTANCE_ID"\n')).decode()]
    for value in invalid:
        ec2 = FakeEC2()
        module = webhook(ec2, ssm=FakeSSM(user_data=value))
        try:
            module["launch_runner"]("arm64")
            raise AssertionError("invalid script was accepted")
        except RuntimeError:
            pass
        assert not ec2.ops("run_instances"), "bad script allocated billable metal"


def case_controller_recognizes_real_terraform_user_data_document():
    source = TF_FILE.read_text()
    script = source.split('runner_user_data = <<-EOF\n', 1)[1].split('\nEOF\n', 1)[0] + '\n'
    wire = base64.b64encode(gzip.compress(script.encode())).decode()
    # IAM cutoff keeps the broker document; it cannot restore a PAT-reading boot.
    assert webhook(FakeEC2())["user_data_protocol"](wire) == (True, True)
    assert '/github-runner/pat' not in script
    runner_grants = (TF_FILE.parent / 'runner-vpc.tf').read_text()
    assert 'aws_iam_policy.ssm_managed_instance.arn' in runner_grants
    assert 'DenyReusableParameterPayloads' in runner_grants
    assert 'Resource = aws_ssm_parameter.github_runner_pat[0].arn' not in runner_grants


def case_registration_credential_is_brokered_only_after_instance_exists():
    journal = []
    ec2 = FakeEC2(journal=journal)
    ssm = FakeSSM(journal=journal)
    module = webhook(ec2, ssm=ssm, github=FakeGitHub(journal=journal))
    instance_id, _ = module["launch_runner"]("arm64")
    mint = journal.index("github:POST:/actions/runners/registration-token")
    launch = next(i for i, value in enumerate(journal) if value.startswith("ec2:run_instances:"))
    put = journal.index(f"ssm:put_parameter:/github-runner/bootstrap/{instance_id}")
    assert mint < launch < put, journal
    credential = ssm.puts[0]
    assert credential["Type"] == "SecureString" and credential["Overwrite"] is False, credential
    tags = {t["Key"]: t["Value"] for t in credential["Tags"]}
    assert tags["InstanceArn"] == f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/{instance_id}", tags
    assert tags["CredentialExpiresAt"] == (NOW + timedelta(hours=1)).isoformat(), tags
    assert "registration-token" not in json.dumps(ec2.ops("run_instances")), "credential leaked into EC2"
    assert "ghp_test" not in json.dumps(ec2.ops("run_instances")), "PAT leaked into EC2"


def case_broker_failure_never_falls_through_to_another_launch():
    ec2 = FakeEC2()
    ssm = FakeSSM(put_error=OSError("do-not-print-this-secret"))
    module = webhook(ec2, ssm=ssm)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        try:
            module["launch_runner"]("arm64")
        except module["RunnerBootstrapError"]:
            pass
        else:
            raise AssertionError("postlaunch broker failure was accepted")
    assert len(ec2.ops("run_instances")) == 1, ec2.calls
    assert len(ec2.ops("terminate_instances")) == 1, ec2.calls
    assert len(ssm.deletes) == 1, ssm.deletes
    assert "do-not-print-this-secret" not in output.getvalue(), output.getvalue()


def case_unusable_registration_token_refuses_before_allocating_metal():
    for github in (
        FakeGitHub(registration_error=True),
        FakeGitHub(registration_payload={"token": "expired", "expires_at": NOW.isoformat()}),
        FakeGitHub(registration_payload={"token": None}),
        FakeGitHub(registration_payload={"token": "too-long-lived", "expires_at": (NOW + timedelta(hours=2)).isoformat()}),
        FakeGitHub(registration_payload={"token": "no-timezone", "expires_at": (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()}),
        FakeGitHub(registration_payload={"token": "x" * 4097, "expires_at": (NOW + timedelta(hours=1)).isoformat()}),
        FakeGitHub(registration_payload={"token": "x" * 16385}),
        FakeGitHub(registration_payload=[]),
    ):
        ec2 = FakeEC2()
        module = webhook(ec2, github=github)
        try:
            module["launch_runner"]("arm64")
        except (RuntimeError, OSError):
            pass
        else:
            raise AssertionError("unusable token was accepted")
        assert not ec2.ops("run_instances"), ec2.calls


def case_broker_failure_is_handled_without_async_launch_retry():
    ec2 = FakeEC2()
    module = webhook(ec2, ssm=FakeSSM(put_error=OSError("SSM refused")))
    event = {"body": json.dumps({"action": "queued", "workflow_job": {"labels": ["ARM64"]}, "launch_count": 4})}
    result = module["handler"](event, None)
    assert result["statusCode"] == 503, result
    assert len(ec2.ops("run_instances")) == 1, ec2.calls


def case_partial_batch_broker_failure_preserves_prior_successful_launch():
    class UniqueEC2(FakeEC2):
        def run_instances(self, **kw):
            super().run_instances(**kw)
            return {'Instances': [{'InstanceId': f'i-{len(self.ops("run_instances")):017x}'}]}

    class FailSecondPublication(FakeSSM):
        def put_parameter(self, **kw):
            result = super().put_parameter(**kw)
            if len(self.puts) == 2:
                raise OSError('second publication answer lost')
            return result

    ec2, ssm = UniqueEC2(), FailSecondPublication()
    module = webhook(ec2, ssm=ssm)
    event = {'body': json.dumps({'action': 'queued',
                                'workflow_job': {'labels': ['ARM64']}, 'launch_count': 4})}
    result = module['handler'](event, None)
    assert result['statusCode'] == 503, result
    assert len(ec2.ops('run_instances')) == 2, ec2.calls
    assert ec2.ops('terminate_instances') == [{'InstanceIds': ['i-00000000000000002']}], ec2.calls
    assert ssm.deletes == [{'Name': '/github-runner/bootstrap/i-00000000000000002'}], ssm.deletes
    assert len(ssm.puts) == 2, ssm.puts


def case_new_runners_are_encrypted_imdsv2_and_shutdown_terminates():
    ec2 = FakeEC2()
    webhook(ec2)["launch_runner"]("arm64")
    launch = ec2.ops("run_instances")[0]
    assert launch["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] is True, launch
    assert launch["MetadataOptions"]["HttpTokens"] == "required", launch
    assert launch["InstanceInitiatedShutdownBehavior"] == "terminate", launch
    tags = {s["ResourceType"]: {t["Key"]: t["Value"] for t in s["Tags"]} for s in launch["TagSpecifications"]}
    for kind in ("instance", "volume", "network-interface"):
        assert tags[kind]["Role"] == "github-runner", tags


def case_controller_passrole_is_pinned_to_runner_role_and_ec2():
    """Guard the actual Terraform policy and every runtime architecture profile.

    This is a source regression guard, not a substitute for post-plan AWS IAM
    simulation against the complete rendered policy and current attachments.
    """
    source = TF_FILE.read_text()
    policy = re.search(r'^resource "aws_iam_role_policy" "runner_lambda" \{\n.*?^\}',
                       source, re.M | re.S).group()
    statements = re.split(r'\n      \},\n      \{\n', policy)
    passing = [statement for statement in statements if '"iam:PassRole"' in statement]
    assert len(passing) == 3, passing
    by_sid = {re.search(r'Sid\s*=\s*"([^"]+)"', statement).group(1): statement
              for statement in passing}
    allow = by_sid['PassRunnerRoleToEC2Only']
    assert re.search(r'Effect\s*=\s*"Allow"', allow), allow
    assert re.search(r'Action\s*=\s*"iam:PassRole"', allow), allow
    assert re.search(r'Resource\s*=\s*aws_iam_role\.runner\[0\]\.arn\s*\n', allow), allow
    assert re.search(r'StringEquals\s*=\s*\{ "iam:PassedToService" = "ec2.amazonaws.com" \}', allow), allow
    deny_roles = by_sid['DenyPassingOtherRoles']
    assert re.search(r'Effect\s*=\s*"Deny"', deny_roles), deny_roles
    assert re.search(r'NotResource\s*=\s*aws_iam_role\.runner\[0\]\.arn\s*$', deny_roles), deny_roles
    assert 'Condition' not in deny_roles, deny_roles
    deny_service = by_sid['DenyPassingRunnerRoleOutsideEC2']
    assert re.search(r'Effect\s*=\s*"Deny"', deny_service), deny_service
    assert re.search(r'Resource\s*=\s*aws_iam_role\.runner\[0\]\.arn\s*\n', deny_service), deny_service
    assert re.search(r'StringNotEqualsIfExists\s*=\s*\{ "iam:PassedToService" = "ec2.amazonaws.com" \}', deny_service), deny_service
    assert re.search(r'INSTANCE_PROFILE\s*=\s*aws_iam_instance_profile\.runner\[0\]\.name', source)
    profile_source = (TF_FILE.parent / 'runner-vpc.tf').read_text()
    profile = re.search(r'^resource "aws_iam_instance_profile" "runner" \{\n.*?^\}',
                        profile_source, re.M | re.S).group()
    assert re.search(r'role\s*=\s*aws_iam_role\.runner\[0\]\.name', profile), profile
    for arch in ('arm64', 'x86_64'):
        ec2 = FakeEC2()
        webhook(ec2, env={'INSTANCE_PROFILE': 'github-runner-profile'})['launch_runner'](arch)
        for call in ec2.ops('run_instances'):
            assert call['IamInstanceProfile'] == {'Name': 'github-runner-profile'}, call


def case_controller_bootstrap_deletion_uses_supported_global_resource_tag():
    source = (TF_FILE.parent / 'runner-bootstrap.tf').read_text()
    controller = re.search(r'^resource "aws_iam_role_policy" "runner_bootstrap_controller" \{\n.*?^\}',
                           source, re.M | re.S).group()
    statements = re.split(r'\n      \},\n      \{\n', controller)
    deletion = next(s for s in statements if '"DeleteControllerBootstrapCredentialOnly"' in s)
    assert re.search(r'Effect\s*=\s*"Allow"', deletion), deletion
    assert re.search(r'Action\s*=\s*"ssm:DeleteParameter"', deletion), deletion
    assert 'parameter/github-runner/bootstrap/*' in deletion, deletion
    assert re.search(r'StringEquals\s*=\s*\{ "aws:ResourceTag/Role" = "github-runner" \}', deletion), deletion
    assert 'ssm:resourceTag/' not in controller, controller
    assert '"ssm:GetParameter"' not in controller, controller


def case_arm_types_are_graviton3_or_newer():
    """Graviton2 cannot run the nested-virtualisation tests.

    fcvm's test_kvm cases need FEAT_NV2, which arrived with Graviton3. On
    2026-08-15 c6gd.metal and m6gd.metal were added to the ARM list for spot
    availability -- both have instance storage, so the storage check passed --
    and the next job to land on a c6gd.metal turned main red with 9 failures
    (nested KVM, NFS, reflink, copy_file_range). Having a local disk is
    necessary, not sufficient.

    The family digit is the generation: c6gd -> 6 (Graviton2),
    c7gd -> 7 (Graviton3), r8gd -> 8 (Graviton4).
    """
    module = webhook(FakeEC2([]))
    for t in module["get_instance_types"]("arm64"):
        family = t.split(".")[0]
        digits = re.findall(r"\d+", family)
        assert digits, f"cannot read a generation out of {t}"
        assert int(digits[0]) >= 7, (
            f"{t} is Graviton{int(digits[0]) - 4} class (family digit "
            f"{digits[0]}); nested virtualisation needs FEAT_NV2, so ARM "
            f"runners must be Graviton3+ (family digit >= 7)"
        )


def case_synchronous_exception_still_falls_through():
    """The original exception path still advances when AWS does raise."""
    ec2 = FakeEC2(run_instances_errors={type_order()[0]: "InsufficientInstanceCapacity"})
    webhook(ec2)["launch_runner"]("x86_64")
    assert launched_types(ec2) == type_order()[:2], launched_types(ec2)


def case_incident_replay_three_dead_c5d_launches():
    """2026-08-07: three c5d.metal launches at 18:41, 18:46 and 18:51.

    All three returned an instance ID, none reached `running`, and AWS did not
    report instance-terminated-no-capacity until 20:31. The old loop saw no
    exception, so every retry picked c5d.metal again and the rest of the list
    was never tried.
    """
    ec2 = FakeEC2([
        instance("i-001e64c56eb71053b", "c5d.metal", "pending", 79),
        instance("i-012093644a97c1b37", "c5d.metal", "pending", 74),
        instance("i-00ead8c13e0bfffff", "c5d.metal", "pending", 69),
    ])
    # GitHub is reachable and has no runner registered for any husk -- required:
    # without a GitHub view, get_capacity deliberately degrades to the instance
    # count and husks WOULD hold slots (fail toward over-counting).
    module = webhook(ec2, github=FakeGitHub())
    module["launch_runner"]("x86_64")
    assert launched_types(ec2) == [type_order()[1]], launched_types(ec2)
    # And the husks are not counted as runners, so the pool is not "full":
    # stalled pending instances are past BOOT_GRACE_MINUTES and not online.
    assert module["get_capacity"]("x86_64")["counted"] == 0


def case_handler_launches_next_type_when_the_pool_is_all_husks():
    """End to end through the webhook entry point, not just launch_runner.

    Four x86 instances exist, so the old count reported the pool full and returned
    "Max x86_64 runners (4) reached". All four are dead launches, so the pool is
    really empty and the next instance type is the one to try.
    """
    ec2 = FakeEC2([
        instance("i-001e64c56eb71053b", "c5d.metal", "pending", 79),
        instance("i-012093644a97c1b37", "c5d.metal", "pending", 74),
        instance("i-00ead8c13e0bfffff", "c5d.metal", "pending", 69),
        instance("i-0fb9768c36e56129d", "c5d.metal", "pending", 60),
    ])
    event = {"body": json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "X64"]},
    })}
    # GitHub reachable, nothing registered: see the husk-counting note above.
    result = webhook(ec2, github=FakeGitHub())["handler"](event, None)
    assert f"Launched 1 x86_64 runner(s) ({type_order()[1]})" in result["body"], result
    assert launched_types(ec2) == [type_order()[1]], launched_types(ec2)


def case_handler_still_refuses_when_the_pool_is_genuinely_full():
    """The cap must still hold for runners that can actually take a job."""
    ec2 = FakeEC2([instance(f"i-live{n}", "c5d.metal", "running", 20) for n in range(4)])
    event = {"body": json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "X64"]},
    })}
    github = FakeGitHub(runners=[
        {"id": n + 1, "name": f"runner-i-live{n}", "status": "online", "busy": False}
        for n in range(4)
    ])
    result = webhook(ec2, github=github)["handler"](event, None)
    assert result["body"] == "Max x86_64 runners (4) reached", result
    assert launched_types(ec2) == [], launched_types(ec2)


def case_ordinary_spot_reclaim_does_not_rotate():
    """A reclaimed runner is not a failed launch.

    Server.SpotInstanceTermination covers a runner that worked for an hour and was
    taken back, so it must not push the working type to the back of the list.
    """
    ec2 = FakeEC2([instance("i-reclaimed", "c7gd.metal", "terminated", 90, arch="arm64",
                            state_reason="Server.SpotInstanceTermination")])
    webhook(ec2)["launch_runner"]("arm64")
    assert launched_types(ec2) == ["c7gd.metal"], launched_types(ec2)


def case_starved_fires_when_wedged_runners_hold_the_cap():
    """The 2026-08-07 shape: the pool is FULL of runners that cannot take work.

    Both wedged ARM runners were GitHub-online for the whole 3.5-hour window, so
    `counted` was 4 of 4 and the original `counted < max_runners` gate held
    ScaleUpStarved at 0 for exactly the incident it was written for. Queued work plus
    a refusal is the signal; whether the cap is full is not.
    """
    emit = webhook(FakeEC2([]))["emit_decision"]
    line = capture_emf(emit, arch="arm64", queued_jobs=3,
                       capacity={"counted": 4, "instances": 4, "online": 4,
                                 "booting": 0, "degraded": False},
                       max_runners=4, decision="blocked", detail="cap reached")
    assert line["ScaleUpStarved"] == 1, line
    assert line["OnlineRunners"] == 4, line
    # Runners exist and answer GitHub; they just cannot drain the queue.
    assert line["ZeroOnlineRunners"] == 0, line


def case_starved_is_quiet_when_nothing_is_queued():
    """A refusal with no queued work is just the cap doing its job."""
    emit = webhook(FakeEC2([]))["emit_decision"]
    line = capture_emf(emit, arch="arm64", queued_jobs=0,
                       capacity={"counted": 4, "instances": 4, "online": 4,
                                 "booting": 0, "degraded": False},
                       max_runners=4, decision="blocked", detail="cap reached")
    assert line["ScaleUpStarved"] == 0, line


def case_zero_online_fires_when_the_pool_is_empty():
    """Queued work, nothing online, and nothing on the way either.

    `booting: 0` is the point. An earlier version of this case used booting: 4, which
    is a cold start rather than an outage -- it asserted the very false positive the
    boot-grace suppression exists to prevent, and only passed because the bug was there.
    """
    emit = webhook(FakeEC2([]))["emit_decision"]
    line = capture_emf(emit, arch="x86_64", queued_jobs=2,
                       capacity={"counted": 0, "instances": 0, "online": 0,
                                 "booting": 0, "degraded": False},
                       max_runners=4, decision="blocked", detail="all husks reaped")
    assert line["ZeroOnlineRunners"] == 1, line
    assert line["OnlineRunners"] == 0, line


def case_zero_online_is_quiet_during_a_cold_start():
    """A normal cold start has online == 0 while metal boots -- that is not an outage.

    BOOT_GRACE_MINUTES allows 15 minutes for registration, so keying on `online` alone
    would raise this on the poll after every cold start and page in ten minutes while
    the capacity that was just requested was arriving exactly as intended.
    """
    emit = webhook(FakeEC2([]))["emit_decision"]
    line = capture_emf(emit, arch="arm64", queued_jobs=2,
                       capacity={"counted": 2, "instances": 2, "online": 0,
                                 "booting": 2, "degraded": False},
                       max_runners=4, decision="launched", detail="cold start")
    assert line["ZeroOnlineRunners"] == 0, line


def case_zero_online_is_suppressed_while_degraded():
    """With GitHub unreachable `online` is 0 by construction -- pure noise."""
    emit = webhook(FakeEC2([]))["emit_decision"]
    line = capture_emf(emit, arch="x86_64", queued_jobs=2,
                       capacity={"counted": 4, "instances": 4, "online": 0,
                                 "booting": 0, "degraded": True},
                       max_runners=4, decision="blocked", detail="github unreachable")
    assert line["ZeroOnlineRunners"] == 0, line


def case_arm_is_unaffected():
    ec2 = FakeEC2([instance("i-ok", "c7gd.metal", "running", 30, arch="arm64")])
    webhook(ec2)["launch_runner"]("arm64")
    assert launched_types(ec2) == ["c7gd.metal"], launched_types(ec2)


def case_one_arch_failure_does_not_rotate_the_other():
    """x86 instances must not deprioritise an ARM type, or vice versa."""
    ec2 = FakeEC2([instance("i-x", "c5d.metal", "pending", 40, arch="x86_64")])
    webhook(ec2)["launch_runner"]("arm64")
    assert launched_types(ec2) == ["c7gd.metal"], launched_types(ec2)


# --------------------------------------------------------------------------
# Cases: the cleanup Lambda's reaper and queue scan
# --------------------------------------------------------------------------

def cleanup(ec2, github, lambda_client=None, env=None, ssm=None, dynamodb=None):
    return load_lambda(CLEANUP_SRC, ec2, ssm or FakeSSM(), lambda_client or FakeLambdaClient(),
                       github, env, dynamodb=dynamodb)


def bootstrap_fixture(instance_id="i-0123456789abcdef0", expires=None):
    name = f"/github-runner/bootstrap/{instance_id}"
    return ({"Name": name, "Type": "SecureString"}, {
        "Role": "github-runner",
        "InstanceArn": instance_arn(instance_id),
        "CredentialExpiresAt": (expires or NOW - timedelta(minutes=1)).isoformat(),
    })


def case_expired_bootstrap_cleanup_reads_metadata_not_values_or_ec2():
    metadata, tags = bootstrap_fixture()
    ssm = FakeSSM(parameters=[metadata], parameter_tags={metadata["Name"]: tags})
    ec2 = FakeEC2()
    result = cleanup(ec2, FakeGitHub(), ssm=ssm)["cleanup_expired_bootstrap_parameters"](NOW)
    assert result == {"deleted": [metadata["Name"]], "errors": 0, "truncated": False}, result
    assert not any(entry.startswith("ssm:get_parameter:") for entry in ssm.journal), ssm.journal
    assert not ec2.calls, "expired token cleanup must not infer absence from an EC2 lookup"
    assert ssm.metadata_calls == [{"ParameterFilters": [{"Key": "Name", "Option": "BeginsWith",
        "Values": ["/github-runner/bootstrap/"]}], "MaxResults": 50}], ssm.metadata_calls


def case_bootstrap_cleanup_preserves_live_credentials_and_canaries():
    metadata, tags = bootstrap_fixture(expires=NOW + timedelta(minutes=20))
    canary = {"Name": "/github-runner/bootstrap/security-canary-20260908-first", "Type": "SecureString"}
    other = {"Name": "/github-runner/pat", "Type": "SecureString"}
    ssm = FakeSSM(parameters=[metadata, canary, other], parameter_tags={metadata["Name"]: tags})
    result = cleanup(FakeEC2(), FakeGitHub(), ssm=ssm)["cleanup_expired_bootstrap_parameters"](NOW)
    assert result["deleted"] == [] and not ssm.deletes, result
    assert ssm.tag_calls == [{"ResourceType": "Parameter", "ResourceId": metadata["Name"]}], ssm.tag_calls


def case_bootstrap_cleanup_requires_complete_matching_provenance():
    for modification in (
        {"Role": "other"}, {"InstanceArn": instance_arn("i-fedcba98765432100")},
        {"CredentialExpiresAt": ""}, {"CredentialExpiresAt": "not-a-time"},
        {"CredentialExpiresAt": (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat()},
    ):
        metadata, tags = bootstrap_fixture()
        tags.update(modification)
        ssm = FakeSSM(parameters=[metadata], parameter_tags={metadata["Name"]: tags})
        result = cleanup(FakeEC2(), FakeGitHub(), ssm=ssm)["cleanup_expired_bootstrap_parameters"](NOW)
        assert not ssm.deletes and not result["deleted"], (modification, result)
    metadata, tags = bootstrap_fixture()
    metadata["Type"] = "String"
    ssm = FakeSSM(parameters=[metadata], parameter_tags={metadata["Name"]: tags})
    assert not cleanup(FakeEC2(), FakeGitHub(), ssm=ssm)["cleanup_expired_bootstrap_parameters"](NOW)["deleted"]
    assert not ssm.tag_calls


def case_bootstrap_cleanup_failure_holds_only_that_parameter_and_redacts_errors():
    first, first_tags = bootstrap_fixture()
    second, second_tags = bootstrap_fixture("i-0123456789abcdef1")
    for failure in ("tag_errors", "delete_errors"):
        ssm = FakeSSM(parameters=[first, second],
            parameter_tags={first["Name"]: first_tags, second["Name"]: second_tags},
            **{failure: {first["Name"]: OSError("do-not-print-credential")}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cleanup(FakeEC2(), FakeGitHub(), ssm=ssm)["cleanup_expired_bootstrap_parameters"](NOW)
        assert result["deleted"] == [second["Name"]] and result["errors"] == 1, result
        assert "do-not-print-credential" not in output.getvalue(), output.getvalue()


def case_bootstrap_cleanup_absent_after_bootstrap_delete_is_not_an_error():
    metadata, tags = bootstrap_fixture()
    ssm = FakeSSM(parameters=[metadata], parameter_tags={metadata["Name"]: tags},
                  delete_errors={metadata["Name"]: FakeClientError("ParameterNotFound")})
    result = cleanup(FakeEC2(), FakeGitHub(), ssm=ssm)["cleanup_expired_bootstrap_parameters"](NOW)
    assert result == {"deleted": [], "errors": 0, "truncated": False}, result


def case_bootstrap_cleanup_is_bounded_by_pages_and_short_client_timeouts():
    ssm = FakeSSM(metadata_pages=[{"Parameters": [], "NextToken": "more"}])
    module = cleanup(FakeEC2(), FakeGitHub(), ssm=ssm)
    result = module["cleanup_expired_bootstrap_parameters"](NOW)
    assert len(ssm.metadata_calls) == 2 and result["truncated"] is True, result
    assert ssm.metadata_calls[1]["NextToken"] == "more", ssm.metadata_calls
    options = [kw["config"] for service, kw in module["_test_client_options"] if service == "ssm" and "config" in kw]
    assert len(options) == 1 and options[0].connect_timeout == 2 and options[0].read_timeout == 2
    assert options[0].retries == {"total_max_attempts": 1}, options


def case_bootstrap_cleanup_time_budget_prevents_late_deletion():
    metadata, tags = bootstrap_fixture()
    ssm = FakeSSM(parameters=[metadata], parameter_tags={metadata["Name"]: tags})
    module = cleanup(FakeEC2(), FakeGitHub(), ssm=ssm)
    ticks = iter([0, 0, 0, 11])  # expires while reading the tags, before DeleteParameter
    module["time"] = types.SimpleNamespace(monotonic=lambda: next(ticks))
    result = module["cleanup_expired_bootstrap_parameters"](NOW)
    assert not ssm.deletes and result["truncated"] is True, result


def case_bootstrap_cleanup_cannot_run_without_exact_account_context():
    ssm = FakeSSM()
    module = cleanup(FakeEC2(), FakeGitHub(), ssm=ssm, env={"RUNNER_ACCOUNT_ID": ""})
    result = module["cleanup_expired_bootstrap_parameters"](NOW)
    assert result["errors"] == 1 and not ssm.metadata_calls and not ssm.deletes, result


def case_bootstrap_cleanup_never_precedes_any_instance_hard_ceiling():
    journal = []
    ec2 = FakeEC2([
        instance("i-aged-first", "c5d.metal", "running", 14 * 60),
        instance("i-aged-second", "c5d.metal", "running", 14 * 60),
    ], journal=journal)
    ssm = FakeSSM(journal=journal, metadata_error=OSError("metadata unavailable"))
    invoker = FakeLambdaClient()
    module = cleanup(ec2, FakeGitHub(journal=journal), lambda_client=invoker, ssm=ssm)
    result = module["handler"]({}, None)
    scan = journal.index("ssm:describe_parameters")
    assert journal.index("ec2:terminate:i-aged-first") < scan
    assert journal.index("ec2:terminate:i-aged-second") < scan
    assert journal.index("ssm:get_parameter:/github-runner/pat") > scan
    assert result["bootstrap_cleanup"]["errors"] == 1 and set(result["terminated"]) == {"i-aged-first", "i-aged-second"}
    assert any("/actions/runs?status=queued" in event for event in journal), journal


def case_stalled_launch_is_tagged_then_terminated():
    """The tag has to land before the terminate, or the reason is lost."""
    ec2 = FakeEC2([instance("i-stuck", "c5d.metal", "pending", 30)])
    result = cleanup(ec2, FakeGitHub())["handler"]({}, None)
    assert result["stalled_launches"] == ["i-stuck"], result
    ops = [op for op, _ in ec2.calls if op in ("create_tags", "terminate_instances")]
    assert ops == ["create_tags", "terminate_instances"], ops
    tags = {t["Key"]: t["Value"] for t in ec2.ops("create_tags")[0]["Tags"]}
    assert tags["CapacityFailedAt"] == NOW.isoformat(), tags


def case_young_pending_launch_is_left_alone():
    ec2 = FakeEC2([instance("i-booting", "c5d.metal", "pending", 5)])
    result = cleanup(ec2, FakeGitHub())["handler"]({}, None)
    assert result["stalled_launches"] == [], result
    assert ec2.ops("terminate_instances") == []


# --------------------------------------------------------------------------
# Cases: the age policy - drain at the soft cap, hard ceiling above it
# --------------------------------------------------------------------------

# The policy these cases assert, in minutes. Spelled out rather than read back
# from the Lambda, so a case says what the fleet is supposed to do instead of
# following whatever the constants happen to hold;
# case_absolute_runner_lifetime_is_13h30m is what ties the two together.
SOFT_CAP_MINUTES = 12 * 60          # MAX_INSTANCE_AGE_HOURS: stop taking new work
DRAIN_GRACE_MINUTES = 90            # time an in-flight job gets to finish
HARD_CEILING_MINUTES = SOFT_CAP_MINUTES + DRAIN_GRACE_MINUTES   # 13h30m, unconditional


def runner_record(instance_id="i-aged", busy=True, status="online", runner_id=77):
    """One entry as GitHub's /actions/runners list returns it."""
    return {"id": runner_id, "name": f"runner-{instance_id}", "busy": busy, "status": status}


def age_poll(age_minutes, runners=(), runners_error=False, delete_error=False,
             instance_id="i-aged", runs=(), terminate_error=None, recheck=None,
             extra_instances=(), create_tags_error=None):
    """One cleanup poll over a single running runner of the given age.

    Returns (result, ec2, github, stdout). Driving the real handler rather than
    age_policy() alone is deliberate: it is the wiring, not the predicate, that
    decides whether an instance is actually terminated.
    """
    ec2 = FakeEC2([instance(instance_id, "c7g.metal", "running", age_minutes, arch="arm64",
                            tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})]
                  + list(extra_instances),
                  terminate_error=terminate_error, create_tags_error=create_tags_error)
    github = FakeGitHub(runs=list(runs), runners=list(runners), recheck=recheck,
                        runners_error=runners_error, delete_error=delete_error)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = cleanup(ec2, github)["handler"]({}, None)
    return result, ec2, github, buf.getvalue()


def terminated_ids(ec2):
    return [i for kw in ec2.ops("terminate_instances") for i in kw["InstanceIds"]]


def still_running(ec2):
    """Instances the poll left running, read off the fake's state.

    Call counts answer "was terminate_instances invoked". This answers "is the
    instance still there", which is the question a case about not killing a
    runner is actually asking.
    """
    return sorted(i["InstanceId"] for i in ec2.instances
                  if i["State"]["Name"] == "running")


def case_busy_runner_past_the_soft_cap_is_drained_not_killed():
    """ejc3/fcvm#884: a job in flight at 12h must be allowed to finish.

    The pre-fix Lambda terminated at MAX_INSTANCE_AGE_HOURS regardless of busy.
    On 2026-08-28/29 that killed i-09fff3a7d97fd4066 at 12.07h and
    i-02fefa9deeb59e9c8 at 12.02h, both mid-job, both reported to GitHub as
    "The self-hosted runner lost communication with the server".
    """
    result, ec2, github, _ = age_poll(SOFT_CAP_MINUTES + 30, runners=[runner_record()])
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == ["i-aged"], result
    # And it is NOT deregistered while busy. GitHub documents that DELETE as
    # "forces the removal of a self-hosted runner" and says nothing about a job
    # the runner is part-way through; a forced removal that ends the job is the
    # failure this case exists to prevent. The runner stops taking new work by
    # being terminated on the first poll that sees it idle, which is at most one
    # 5-minute interval after its job ends.
    assert github.deletes == [], github.deletes


def case_busy_runner_just_under_the_hard_ceiling_is_still_draining():
    """The grace is a real window, not a rounding error on the soft cap."""
    result, ec2, _, _ = age_poll(HARD_CEILING_MINUTES - 5, runners=[runner_record()])
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == ["i-aged"], result


def case_busy_runner_past_the_hard_ceiling_is_terminated():
    """The ceiling is unconditional. ejc3/fcvm#871's wedged host reports busy forever.

    Draining on `busy` is only safe because this bound exists: a host whose
    scheduler has failed holds its job for as long as it is alive, so waiting for
    "not busy" is waiting for something that never comes.
    """
    result, ec2, github, _ = age_poll(HARD_CEILING_MINUTES + 5, runners=[runner_record()])
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result
    assert github.deletes, "the runner must be deregistered as well as terminated"


def case_hard_ceiling_kill_of_a_busy_runner_is_loud():
    """The log line is the only trace that the loss was ours and not AWS's.

    All three causes render identically on GitHub as "lost communication with
    the server", so the Lambda has to say which one it was.
    """
    _, _, _, out = age_poll(HARD_CEILING_MINUTES + 5, runners=[runner_record()])
    assert "HARD-CEILING KILL" in out, out
    assert "not an AWS spot reclaim" in out, out
    assert "busy=True" in out, out


def case_idle_runner_past_the_soft_cap_is_terminated_immediately():
    """Draining is not a reprieve: an idle over-age runner goes now.

    This is what keeps the drain from becoming a way to live longer. It is also
    the only mechanism stopping a drained runner picking up new work, so it must
    fire on the first poll that observes idleness, not at lease expiry.
    """
    result, ec2, github, _ = age_poll(SOFT_CAP_MINUTES + 5,
                                      runners=[runner_record(busy=False)])
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["over_age"] == ["i-aged"], result
    assert result["draining"] == [], result
    assert result["hard_killed"] == [], result
    assert github.deletes, "an idle drained runner must be deregistered"


def case_unreachable_github_drains_inside_the_grace():
    """No answer from GitHub is not evidence that the runner is idle.

    Reading an API failure as "idle" would terminate a runner mid-job on every
    GitHub blip, which is ejc3/fcvm#884 again with a different trigger. The ceiling below
    is what makes it safe to wait instead.
    """
    result, ec2, _, _ = age_poll(SOFT_CAP_MINUTES + 30, runners_error=True)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == ["i-aged"], result


def case_unreachable_github_is_still_terminated_at_the_ceiling():
    """The ceiling cannot be deferred by a GitHub error.

    get_runners() swallows the exception and returns {}, so the reaper has no
    record at all for this instance - exactly the state in which a bound that
    needed one would leak the instance forever.
    """
    result, ec2, _, out = age_poll(HARD_CEILING_MINUTES + 5, runners_error=True)
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["over_age"] == ["i-aged"], result
    assert result["hard_killed"] == ["i-aged"], result
    assert "HARD-CEILING KILL" in out, out


def case_runner_missing_from_github_is_still_terminated_at_the_ceiling():
    """GitHub answers and has no record for this instance: same outcome."""
    result, ec2, _, _ = age_poll(HARD_CEILING_MINUTES + 5, runners=[])
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result


def case_ceiling_terminates_even_when_deregistration_fails():
    """A failed DELETE must not abort the terminate that follows it."""
    result, ec2, github, _ = age_poll(HARD_CEILING_MINUTES + 5, runners=[runner_record()],
                                      delete_error=True)
    assert github.deletes, "the deregistration must at least be attempted"
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result


def case_busy_runner_under_the_soft_cap_is_untouched():
    """Ordinary life is unchanged: renew the lease, terminate nothing."""
    result, ec2, github, _ = age_poll(SOFT_CAP_MINUTES - 60, runners=[runner_record()])
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == [], result
    assert result["over_age"] == [], result
    assert result["renewed"] == ["i-aged"], result
    assert github.deletes == [], github.deletes


def case_age_policy_ceiling_ignores_every_reported_runner_state():
    """Past the ceiling the verdict is the same for every possible input.

    The point of the ceiling is that it is computable from the launch time
    alone. Anything it consulted - a busy flag, a status, a tag, a GitHub
    response - is something that can be missing or wrong, and a bound that can be
    deferred by a broken component is not a bound.
    """
    module = cleanup(FakeEC2(), FakeGitHub())
    policy = module["age_policy"]
    launched = NOW - timedelta(minutes=HARD_CEILING_MINUTES + 1)
    states = [
        None, {},
        {"busy": True, "status": "online"},
        {"busy": True, "status": "offline"},
        {"busy": True, "status": None},
        {"busy": False, "status": "online"},
        {"busy": None, "status": None},
        {"status": "online"},
        {"id": 1},
    ]
    for state in states:
        verdict = policy(NOW, launched, state)
        assert verdict == module["TERMINATE_CEILING"], (state, verdict)


def case_malformed_github_timestamp_cannot_block_the_ceiling():
    """A bad value from GitHub must not abort the poll before anything is reaped.

    get_stuck_runners() runs before the age phase. parse_ts() used to hand back a
    NAIVE datetime for a timestamp carrying no timezone designator, and comparing
    that to the aware `now` raises TypeError out of the entire poll: no lease
    renewals, no reaping, and in particular no hard ceiling. That is the ceiling
    being deferred by a GitHub value, which is the one thing it must never be.
    """
    result, ec2, _, _ = age_poll(
        HARD_CEILING_MINUTES + 5, runners=[runner_record()],
        runs=[{"id": 1, "status": "in_progress", "run_started_at": "2026-08-07T16:00:00"}])
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result


def case_a_bad_runner_record_cannot_block_the_ceiling():
    """One unusable entry in GitHub's runner list must not kill the poll.

    The orphan phase calls runner_name.startswith() on every key, so a record whose
    name is not a string raises AttributeError out of the whole invocation, and
    every instance - including one past the ceiling - survives it.
    """
    result, ec2, _, _ = age_poll(
        HARD_CEILING_MINUTES + 5,
        runners=[{"id": 1, "name": None, "busy": True, "status": "online"},
                 runner_record()])
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result


def case_one_unreadable_instance_cannot_block_another_ceiling_kill():
    """A malformed EC2 record must cost that instance, not the whole sweep.

    The age of an instance with no LaunchTime cannot be computed, so there is no
    safe verdict for it and it is skipped and logged. What must not happen is the
    exception ending the loop, because everything after it - here a runner well
    past the hard ceiling - then outlives the bound.
    """
    broken = instance("i-broken", "c7g.metal", "running", 120, arch="arm64")
    del broken["LaunchTime"]
    aged = instance("i-aged", "c7g.metal", "running", HARD_CEILING_MINUTES + 5, arch="arm64",
                    tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})
    ec2 = FakeEC2([broken, aged])
    github = FakeGitHub(runners=[runner_record()])
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        result = cleanup(ec2, github)["handler"]({}, None)
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result
    assert "i-broken" in buf.getvalue(), "the skipped instance must be named in the log"


def case_stuck_scan_still_works_on_a_timestamp_with_no_offset():
    """Surviving a bad timestamp is not enough; the scan must still do its job.

    The handler now swallows anything get_stuck_runners() throws, so a naive
    timestamp would no longer kill the poll - it would silently disable the
    MAX_JOB_RUNTIME_MINUTES reaper for that invocation instead, which is the
    fail-open form of the same bug. parse_ts() stamping UTC is what keeps the scan
    working rather than merely surviving.
    """
    stale = (NOW - timedelta(minutes=300)).replace(tzinfo=None).isoformat()
    ec2 = FakeEC2([instance("i-stuck", "c7g.metal", "running", 60, arch="arm64",
                            tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})])
    github = FakeGitHub(
        runners=[runner_record("i-stuck")],
        runs=[{"id": 5, "status": "in_progress", "run_started_at": stale}],
        jobs={5: [{"name": "Host-Root-arm64", "status": "in_progress",
                   "started_at": stale, "runner_name": "runner-i-stuck",
                   "labels": ["self-hosted", "Linux", "ARM64"]}]},
    )
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github)["handler"]({}, None)
    assert result["stuck_terminated"] == ["i-stuck"], result
    assert terminated_ids(ec2) == ["i-stuck"], terminated_ids(ec2)


def case_a_failing_stuck_scan_cannot_block_the_ceiling():
    """Whatever get_stuck_runners() does with a payload, the reaping still runs.

    It is the only GitHub-derived phase ahead of the age phase and it parses
    arbitrary API responses, so it is the likeliest thing to throw something
    nobody predicted. Injecting the failure directly covers the shapes a fixture
    cannot enumerate.
    """
    ec2 = FakeEC2([instance("i-aged", "c7g.metal", "running", HARD_CEILING_MINUTES + 5,
                            arch="arm64",
                            tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})])
    module = cleanup(ec2, FakeGitHub(runners=[runner_record()]))

    def explode(pat, now):
        raise RuntimeError("GitHub returned something unparseable")

    module["get_stuck_runners"] = explode
    with contextlib.redirect_stdout(io.StringIO()):
        result = module["handler"]({}, None)
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result


def case_drain_outranks_an_expired_lease():
    """A draining runner is not handed back to the lease phase.

    A runner that drains does not renew, so its lease expires 60 minutes later
    while it is still inside the grace. If the drain fell through instead of
    ending the iteration, the lease phase would terminate it on that evidence -
    which is the original defect wearing a different hat, since "GitHub did not
    answer" is why the runner is draining in the first place.
    """
    ec2 = FakeEC2([instance("i-aged", "c7g.metal", "running", SOFT_CAP_MINUTES + 30,
                            arch="arm64",
                            tags={"LeaseExpires": (NOW - timedelta(minutes=45)).isoformat()})])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, FakeGitHub(runners_error=True))["handler"]({}, None)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == ["i-aged"], result
    assert result["expired"] == [], result


def case_the_ceiling_fires_at_the_ceiling_not_after_it():
    """Exactly 13h30m is over, not under. Pins >= against a silent >.

    A boundary only reached by a test at the boundary; +/-5 minutes on either
    side cannot tell the two comparisons apart.
    """
    module = cleanup(FakeEC2(), FakeGitHub())
    policy, busy = module["age_policy"], {"id": 1, "busy": True, "status": "online"}
    exactly = NOW - timedelta(minutes=HARD_CEILING_MINUTES)
    a_moment_earlier = NOW - timedelta(minutes=HARD_CEILING_MINUTES, microseconds=-1)
    assert policy(NOW, exactly, busy) == module["TERMINATE_CEILING"]
    assert policy(NOW, a_moment_earlier, busy) == module["DRAIN"]
    # And the soft cap keeps its own boundary: exactly 12h is still ordinary life.
    assert policy(NOW, NOW - timedelta(minutes=SOFT_CAP_MINUTES), busy) == module["KEEP"]


def case_a_failed_terminate_is_never_reported_as_a_termination():
    """A terminate that EC2 rejects leaves the instance RUNNING. Say so.

    The per-instance guard around the loop body must not absorb this. An
    invocation that swallows the rejection, returns terminated=[] and logs it as
    an unreadable EC2 *record* is claiming the fleet's only lifetime bound took
    effect when the instance is still up: fail-open, and mislabelled at that.
    """
    result, ec2, _, out = age_poll(
        HARD_CEILING_MINUTES + 5, runners=[runner_record()],
        terminate_error=FakeClientError("UnauthorizedOperation"))
    assert terminated_ids(ec2) == ["i-aged"], "the terminate must at least be attempted"
    assert result["terminated"] == [], result
    assert result["hard_killed"] == [], result
    assert result["terminate_failed"] == ["i-aged"], result
    assert "TERMINATE FAILED" in out, out
    assert "UNREADABLE INSTANCE RECORD" not in out, out


def case_a_record_with_no_instance_id_cannot_stop_the_later_phases():
    """The sweep's guard names "no InstanceId", so that input must be survivable.

    The stuck-job phase rebuilds the instance-id set from the same reservations
    OUTSIDE the per-instance guard. One record without the key raised KeyError there
    and took every later phase with it - the stuck-job reaper, the orphan cleanup,
    the AMI and stalled-launch sweeps and the launcher - on every poll for as long
    as the record existed.
    """
    nameless = instance("i-nameless", "c7g.metal", "running", 120, arch="arm64")
    del nameless["InstanceId"]
    stalled = instance("i-stalled", "c5d.metal", "pending", 30)
    result, ec2, _, _ = age_poll(HARD_CEILING_MINUTES + 5, runners=[runner_record()],
                                 extra_instances=[nameless, stalled])
    assert result["hard_killed"] == ["i-aged"], result
    # The stalled-launch phase runs after the sweep; if the KeyError is back, the
    # poll never gets there.
    assert result["stalled_launches"] == ["i-stalled"], result


def case_an_idle_verdict_is_rechecked_before_the_runner_is_terminated():
    """The busy flag read at the top of the poll is seconds stale by now.

    Between that read and this terminate comes the rest of the poll. GitHub hands
    out jobs the whole time, so terminating a runner past the soft cap on the
    ORIGINAL observation destroys the job it picked up in the gap - and on that path
    the poll reports the benign "GitHub reports it idle", so the HARD-CEILING KILL
    line would not even record the loss.
    """
    result, ec2, _, _ = age_poll(
        SOFT_CAP_MINUTES + 5,
        runners=[runner_record(busy=False)],
        recheck={77: {"id": 77, "name": "runner-i-aged", "busy": True, "status": "online"}})
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == ["i-aged"], result


def case_an_unanswerable_recheck_drains_rather_than_terminating():
    """If the re-read fails there is no fresh evidence, so do not act on stale.

    Costs at most the rest of the grace window, which the ceiling bounds.
    """
    result, ec2, _, _ = age_poll(SOFT_CAP_MINUTES + 5,
                                 runners=[runner_record(busy=False)], recheck={77: None})
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == ["i-aged"], result


def case_a_confirmed_idle_runner_is_still_terminated_at_the_soft_cap():
    """The re-check must not turn the drain into a way to live forever."""
    result, ec2, github, _ = age_poll(SOFT_CAP_MINUTES + 5,
                                      runners=[runner_record(busy=False)])
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["over_age"] == ["i-aged"], result
    assert github.deletes, "an idle drained runner must be deregistered"


def case_a_ceiling_kill_of_an_idle_runner_is_quiet():
    """Loud means "we may have destroyed work", so it must not cry wolf.

    A runner can reach the ceiling and only then be observed idle: GitHub was
    unreachable for the whole grace window and answers on the last poll. That is a
    clean reap, and printing HARD-CEILING KILL for it would report a job loss that
    did not happen - the inverse of the mislabelling in ejc3/fcvm#884.
    """
    result, ec2, _, out = age_poll(HARD_CEILING_MINUTES + 5,
                                   runners=[runner_record(busy=False)])
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["over_age"] == ["i-aged"], result
    assert result["hard_killed"] == [], result
    assert "HARD-CEILING KILL" not in out, out


def case_a_busy_runner_is_not_deregistered_when_ec2_cannot_be_read():
    """The orphan phase must not read "could not ask EC2" as "the instance is gone".

    It deregisters a runner whose instance has disappeared, and GitHub documents
    that DELETE as forcing the removal. One transient DescribeInstances failure
    therefore forced out a runner in the middle of a job, which is exactly the
    class of loss this file is trying to stop.
    """
    # Deliberately NOT in the sweep's running set, so the cross-check there cannot
    # answer for this and get_instance_state's own verdict is what is under test.
    ec2 = FakeEC2([], lookup_error=FakeClientError("RequestLimitExceeded"))
    github = FakeGitHub(runners=[runner_record("i-live")])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github)["handler"]({}, None)
    assert github.deletes == [], github.deletes
    assert result["orphans_cleaned"] == [], result
    assert terminated_ids(ec2) == [], terminated_ids(ec2)


def case_a_runner_whose_instance_really_is_gone_is_still_deregistered():
    """The other half: EC2 saying "no such instance" must still clean the orphan.

    A terminated instance drops out of DescribeInstances entirely about an hour
    later and the call then raises InvalidInstanceID.NotFound, so "gone" arrives
    as an exception too. Telling the two apart is the whole point.
    """
    ec2 = FakeEC2([], lookup_error=FakeClientError("InvalidInstanceID.NotFound"))
    github = FakeGitHub(runners=[runner_record("i-vanished")])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github)["handler"]({}, None)
    assert result["orphans_cleaned"] == ["runner-i-vanished"], result
    assert github.deletes, "the orphaned registration must be removed"


def case_the_age_sweep_runs_before_the_optional_cleanups():
    """Phase order is a safety property, because a Lambda timeout is not catchable.

    Everything ahead of the sweep can spend the 240-second budget. The orphan scan
    issues one EC2 describe per registered runner and deregisters at a 10-second
    timeout, and the stuck-job scan can spend eleven GitHub calls at five seconds
    each. If either runs long the invocation dies before a single instance has been
    examined, and the hard ceiling simply does not happen that poll - with nothing
    able to catch it and nothing in this account alarming on Lambda errors. So the
    bound goes first and the optional work goes after it.
    """
    journal = []
    ec2 = FakeEC2([instance("i-aged", "c7g.metal", "running", HARD_CEILING_MINUTES + 5,
                            arch="arm64",
                            tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})],
                  journal=journal)
    github = FakeGitHub(
        runners=[runner_record(), {"id": 99, "name": "runner-i-vanished",
                                   "busy": False, "status": "offline"}],
        runs=[run(1, "in_progress")], journal=journal)
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github)["handler"]({}, None)
    assert result["hard_killed"] == ["i-aged"], result
    assert result["orphans_cleaned"] == ["runner-i-vanished"], result

    kill = journal.index("ec2:terminate:i-aged")
    orphan_delete = next(i for i, e in enumerate(journal) if e.startswith("github:DELETE:/actions/runners/99"))
    stuck_scan = next(i for i, e in enumerate(journal) if "/actions/runs?status=in_progress" in e)
    assert kill < orphan_delete, journal
    assert kill < stuck_scan, journal


def case_a_runner_record_with_no_id_is_not_acted_on():
    """A record with no id can be neither re-read nor deregistered, so drop it.

    Keeping it lets two things happen. The idle path terminates on a snapshot it
    cannot confirm, because the fresh re-check is skipped when there is no id to
    re-read. And the orphan phase indexes runner_info['id'] directly, so a None
    would be formatted straight into the DELETE URL. Dropping it in get_runners
    makes the instance simply unknown, which drains and dies at the ceiling.
    """
    result, ec2, github, _ = age_poll(
        SOFT_CAP_MINUTES + 5,
        runners=[{"name": "runner-i-aged", "busy": False, "status": "online"}])
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["draining"] == ["i-aged"], result
    assert not any("None" in url for url in github.requests), github.requests


def case_a_rejected_stuck_reap_does_not_kill_the_poll():
    """The stuck-job reap must go through terminate() like every other site.

    It called ec2.terminate_instances directly, outside any guard, so one rejected
    call raised out of the handler and took every later phase with it - including
    the queued-job launcher, which this file calls the last line of defence for
    webhooks GitHub does not redeliver. The reorder widened that: the orphan
    cleanup used to have run by this point and now has not.
    """
    stale = (NOW - timedelta(minutes=300)).isoformat()
    ec2 = FakeEC2([instance("i-stuck", "c7g.metal", "running", 60, arch="arm64",
                            tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})],
                  terminate_error=FakeClientError("UnauthorizedOperation"))
    github = FakeGitHub(
        runners=[runner_record("i-stuck")],
        runs=[{"id": 5, "status": "in_progress", "run_started_at": stale}],
        jobs={5: [{"name": "Host-Root-arm64", "status": "in_progress", "started_at": stale,
                   "runner_name": "runner-i-stuck",
                   "labels": ["self-hosted", "Linux", "ARM64"]},
                  job("queued", "arm64")]})
    invoker = FakeLambdaClient()
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        result = cleanup(ec2, github, invoker)["handler"]({}, None)
    assert result["stuck_terminated"] == [], result
    assert result["terminate_failed"] == ["i-stuck"], result
    assert "TERMINATE FAILED" in buf.getvalue()
    # The launcher is the LAST phase, so reaching it proves the poll survived.
    # It is also the phase that matters most: GitHub does not redeliver webhooks,
    # so this poll is the only retry a queued job gets.
    assert invoker.arch_invokes() == {"arm64": 1}, invoker.arch_invokes()


def case_a_rejected_ami_builder_terminate_does_not_kill_the_poll():
    """Same class, the other raw call site."""
    ec2 = FakeEC2([instance("i-ami", "c7g.metal", "running", 200,
                            tags={"Name": "ami-builder-temp"})],
                  terminate_error=FakeClientError("UnauthorizedOperation"))
    queued = run(9, "queued")
    github = FakeGitHub(runs=[queued], jobs={queued["id"]: [job("queued", "x86_64")]})
    invoker = FakeLambdaClient()
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github, invoker)["handler"]({}, None)
    assert result["ami_builder_terminated"] == [], result
    assert invoker.arch_invokes() == {"x86_64": 1}, invoker.arch_invokes()


def case_a_runner_is_deregistered_only_after_ec2_accepts_the_terminate():
    """Deregistering first, then failing to terminate, is the worst outcome.

    It forces a runner out of GitHub - which is how a job in flight dies - and
    leaves the instance alive anyway. The next poll then has no GitHub record for
    it, so it reads as unknown, drains, and is finally hard-killed 85 minutes
    later while being loudly reported as work we may have destroyed. Terminate
    first: if EC2 refuses, the runner keeps working and keeps its registration.
    """
    result, ec2, github, _ = age_poll(
        SOFT_CAP_MINUTES + 5, runners=[runner_record(busy=False)],
        terminate_error=FakeClientError("UnauthorizedOperation"))
    assert result["terminate_failed"] == ["i-aged"], result
    assert github.deletes == [], github.deletes


def case_a_failed_ceiling_terminate_is_not_announced_as_a_dead_job():
    """HARD-CEILING KILL says a job "is now dead". Only say it if it is.

    Logging before attempting the call produced both "is now dead" and "STILL
    RUNNING" for one instance in one poll, and counted it in hard_killed - the
    number documented as work we may have destroyed.
    """
    result, _, github, out = age_poll(
        HARD_CEILING_MINUTES + 5, runners=[runner_record()],
        terminate_error=FakeClientError("UnauthorizedOperation"))
    assert result["hard_killed"] == [], result
    assert "HARD-CEILING KILL" not in out, out
    assert "TERMINATE FAILED" in out, out
    # And the runner keeps its registration. Deregistering first, then failing to
    # terminate, forces a live runner out of GitHub (killing whatever it runs) and
    # leaves the instance up regardless - the worst of both, for no gain.
    assert github.deletes == [], github.deletes


def case_a_running_instance_is_never_deregistered_on_a_notfound_blip():
    """DescribeInstances is eventually consistent; the bulk read is the witness.

    AWS documents InvalidInstanceID.NotFound as transient after RunInstances, so
    it is not by itself proof that an instance is gone. The sweep has already
    listed every running runner instance this poll, so if the id is in that list
    it is demonstrably alive and its registration must be left alone.
    """
    ec2 = FakeEC2([instance("i-live", "c7g.metal", "running", 60, arch="arm64",
                            tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})],
                  lookup_error=FakeClientError("InvalidInstanceID.NotFound"))
    github = FakeGitHub(runners=[runner_record("i-live")])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github)["handler"]({}, None)
    assert result["orphans_cleaned"] == [], result
    assert github.deletes == [], github.deletes


def case_absolute_runner_lifetime_is_13h30m():
    """The one number a reader of this fleet needs, pinned so it cannot drift.

    12h soft cap + 90m grace. The cleanup Lambda polls every 5 minutes, so the
    observed maximum is this plus at most one poll interval.
    """
    module = cleanup(FakeEC2(), FakeGitHub())
    total = module["MAX_INSTANCE_AGE_HOURS"] * 60 + module["DRAIN_GRACE_MINUTES"]
    assert total == HARD_CEILING_MINUTES == 13 * 60 + 30, total


# --------------------------------------------------------------------------
# Cases: the lease phase - what GitHub's answer is allowed to decide
# --------------------------------------------------------------------------

def lease_poll(lease_minutes_ago=45, age_minutes=60, tags=None, runners=(),
               runners_error=False, runners_total=None, ssm=None,
               instance_id="i-lease", omit_runners_total=False, create_tags_error=None,
               create_tags_reject_keys=None, runner_payloads=None, dynamodb=None,
               recheck=None):
    """One cleanup poll over a single running runner UNDER the soft cap.

    The lease is what is under test, so the instance is deliberately young
    enough that age_policy() answers KEEP and every verdict below comes from
    the lease phase. `lease_minutes_ago` is how long ago the lease expired.
    """
    all_tags = {"LeaseExpires": (NOW - timedelta(minutes=lease_minutes_ago)).isoformat()}
    all_tags.update(tags or {})
    ec2 = FakeEC2([instance(instance_id, "c7g.metal", "running", age_minutes,
                            arch="arm64", tags=all_tags)],
                  create_tags_error=create_tags_error,
                  create_tags_reject_keys=create_tags_reject_keys)
    github = FakeGitHub(runners=list(runners), runners_error=runners_error,
                        runners_total=runners_total, omit_runners_total=omit_runners_total,
                        runner_payloads=runner_payloads, recheck=recheck)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = cleanup(ec2, github, ssm=ssm, dynamodb=dynamodb)["handler"]({}, None)
    return result, ec2, github, buf.getvalue()


def case_a_busy_runner_survives_a_github_outage_past_its_lease():
    """ejc3/aws#45: an unread roster is not evidence that a runner is idle.

    The lease made a single blip survivable and an OUTAGE fatal: once GitHub
    had been unreadable for longer than the remaining lease, every runner took
    the idle path, its lease was allowed to expire, and it was terminated
    mid-job while still under the soft cap. On GitHub that reads as "The
    self-hosted runner lost communication with the server", which is
    indistinguishable from a spot reclaim (ejc3/fcvm#884).
    """
    result, ec2, _, _ = lease_poll(
        lease_minutes_ago=45, runners_error=True,
        tags={"RunnerSeenAt": (NOW - timedelta(minutes=50)).isoformat()})
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["expired"] == [], result
    assert result["held"] == ["i-lease"], result
    # HELD, not renewed. Renewing on an unread roster would make a wedged host
    # immortal, which is the bound ejc3/fcvm#871 needs; the lease stays where it
    # is and the age ceiling remains the thing that ends this instance.
    lease_writes = [kw for kw in ec2.ops("create_tags")
                    if any(t["Key"] == "LeaseExpires" for t in kw["Tags"])]
    assert lease_writes == [], lease_writes


def case_an_idle_runner_past_its_lease_is_still_terminated():
    """Scale-down itself, which the fix above must not disable.

    GitHub answered and reported busy=false. That is a verdict about this
    runner, so the lease is allowed to lapse and the instance goes.
    """
    result, ec2, _, _ = lease_poll(runners=[runner_record("i-lease", busy=False)])
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result
    assert result["held"] == [], result


def case_a_runner_that_never_registered_still_dies_at_its_lease():
    """A box that booted and never joined must not be held to the ceiling.

    user_data registers only when the IPv6 gate passes AND the PAT reads back
    from SSM, so an instance that stays `running` and never registers is a
    designed-for outcome, not a hypothetical. Nothing else reaps it: phase 5
    only walks `pending`, and the age phase would leave it running for 13h30m.
    The lease is what kills it, and GitHub listing other runners while never
    having listed this one is a real answer about it.
    """
    result, ec2, _, _ = lease_poll(runners=[runner_record("i-other")])
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result


def case_an_initial_lease_that_ec2_refused_is_reported_as_held():
    """An instance with no lease tag whose CreateTags failed still has no lease.

    The poll result is what the alarm and the operator read. Reporting nothing
    for this instance says the sweep handled it; it did not, the lease tag is
    still absent, and the next poll has to try again. `held` is what that is.
    """
    ec2 = FakeEC2([instance("i-lease", "c7g.metal", "running", 60, arch="arm64", tags={})],
                  create_tags_error=OSError("CreateTags throttled"))
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, FakeGitHub(
            runners=[runner_record("i-lease", busy=False)]))["handler"]({}, None)
    assert result["held"] == ["i-lease"], result
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["expired"] == [], result
    lease_tags = [t for i in ec2.instances for t in i.get("Tags", [])
                  if t["Key"] == "LeaseExpires"]
    assert lease_tags == [], lease_tags


def case_a_registered_runner_missing_from_a_readable_roster_holds_its_lease():
    """Seen before, absent now: one of the two explanations is a live job.

    A roster we could read is authoritative about REGISTRATION. It is not
    authoritative about a box that was registered on the previous poll: a real
    deregistration and an answer that dropped records look identical from here.
    Hold the lease and let the ceiling bound it.
    """
    result, ec2, _, _ = lease_poll(
        runners=[runner_record("i-other")],
        tags={"RunnerSeenAt": (NOW - timedelta(minutes=6)).isoformat()})
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["held"] == ["i-lease"], result


def case_a_truncated_roster_cannot_expire_a_lease():
    """A short page reads exactly like "this runner is not registered".

    GitHub reports total_count beside the page, so a read that came back short
    is detectable rather than silently partial. Detect it and the whole roster
    is unread; miss it and truncation terminates whichever runners fell off the
    end - the fail-open of ejc3/aws#45 arriving through pagination instead of
    through an exception.
    """
    result, ec2, _, _ = lease_poll(runners=[runner_record("i-other")], runners_total=7)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["held"] == ["i-lease"], result


def case_a_missing_pat_is_not_evidence_that_a_runner_is_idle():
    """No PAT means no answer, not "nothing is registered".

    get_github_pat() returns None for a missing or placeholder parameter, and
    the roster was then skipped entirely and treated as empty - so an SSM
    problem, or a rotated-out PAT, reaped the whole fleet an hour later.
    """
    result, ec2, _, _ = lease_poll(ssm=FakeSSM(pat="placeholder"))
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["held"] == ["i-lease"], result


def case_a_record_with_no_busy_flag_is_not_read_as_idle():
    """`.get('busy', False)` read a missing key as "holds no job"."""
    result, ec2, _, _ = lease_poll(
        runners=[{"id": 5, "name": "runner-i-lease", "status": "online"}])
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["held"] == ["i-lease"], result


def case_a_sustained_outage_says_how_long_the_runner_has_been_unobserved():
    """A poll that decides nothing must still say that, and for how long.

    Holding leases silently turns an outage into an absence of log lines, which
    is the same state as a healthy quiet poll. The line carries the duration so
    a blip and a three-hour outage are distinguishable in CloudWatch.
    """
    result, _, _, out = lease_poll(
        runners_error=True,
        tags={"RunnerSeenAt": (NOW - timedelta(hours=3)).isoformat()})
    assert "180m" in out, out
    assert result["held"] == ["i-lease"], result


def case_the_roster_answer_is_recorded_on_the_instance():
    """Two polls: the second has to remember what the first was told.

    The whole distinction between "never joined" and "was here and vanished"
    rests on this tag reaching the instance, so this drives two real polls
    against one EC2 rather than hand-placing the tag and asserting on it.
    """
    inst = instance("i-lease", "c7g.metal", "running", 60, arch="arm64",
                    tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})
    ec2 = FakeEC2([inst])
    with contextlib.redirect_stdout(io.StringIO()):
        first = cleanup(ec2, FakeGitHub(
            runners=[runner_record("i-lease", busy=False)]))["handler"]({}, None)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
    assert tags.get("RunnerSeenAt") == NOW.isoformat(), tags
    assert first["held"] == [], first

    # The clock is frozen, so the hour that would pass between the two polls is
    # applied to the lease instead: this is the same instance, one lease later.
    tags["LeaseExpires"] = (NOW - timedelta(minutes=45)).isoformat()
    inst["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]

    with contextlib.redirect_stdout(io.StringIO()):
        second = cleanup(ec2, FakeGitHub(
            runners=[runner_record("i-other")]))["handler"]({}, None)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert second["held"] == ["i-lease"], second


def case_a_roster_of_exactly_the_page_limit_is_still_a_complete_read():
    """Ten full pages that add up to GitHub's own total_count are a complete read.

    The reader ended on a short page or on the page limit, and only the first
    counted as finishing. A roster of exactly ROSTER_PAGE_LIMIT full pages fell
    into the for/else and was reported unread, which held every lease in the
    fleet on an answer that was complete. Reaching total_count ends the read.
    """
    roster = [{"id": n, "name": f"runner-i-other{n}", "busy": False, "status": "online"}
              for n in range(1, 1001)]
    result, ec2, github, _ = lease_poll(runners=roster)
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result
    # Ten pages per pass, and get_runners() makes two passes.
    reads = [url for url in github.requests if "/actions/runners?" in url]
    assert len(reads) == 20, reads


def case_the_webhook_accepts_a_complete_roster_at_the_page_limit():
    """The launch-side reader finishes on the same count proof at its page limit."""
    roster = [{"id": n, "name": f"runner-i-live{n}", "busy": False,
               "status": "online"} for n in range(1, 1001)]
    github = FakeGitHub(runners=roster)
    names = webhook(FakeEC2(), github=github)["get_online_runner_names"]()
    assert names == {r["name"] for r in roster}, names
    reads = [url for url in github.requests if "/actions/runners?" in url]
    assert len(reads) == 10, reads


def case_an_unusable_runner_identity_makes_the_roster_unread():
    """A name needs text and an id needs to be a positive, non-bool integer.

    Every action on a runner formats its id into a URL, and `True` is an int
    to isinstance(). A record with any of these identities held the lease on
    the old check only when the field was None; the rest passed as usable.
    """
    invalid = [
        {"id": 7, "name": ""},
        {"id": 7, "name": "   "},
        {"id": True, "name": "runner-i-lease"},
        {"id": False, "name": "runner-i-lease"},
        {"id": 0, "name": "runner-i-lease"},
        {"id": -1, "name": "runner-i-lease"},
        {"id": 7.0, "name": "runner-i-lease"},
        {"id": "7", "name": "runner-i-lease"},
    ]
    for identity in invalid:
        record = {**identity, "busy": False, "status": "online"}
        result, ec2, github, _ = lease_poll(runners=[record])
        assert still_running(ec2) == ["i-lease"], (identity, still_running(ec2))
        assert result["held"] == ["i-lease"], (identity, result)
        assert github.deletes == [], (identity, github.deletes)


def case_a_roster_that_changes_between_reads_cannot_expire_a_lease():
    """Two complete reads that disagree are a roster that was moving.

    The first pass lists one runner and adds up to its total_count; the
    second lists two. One pass alone was accepted and the lease of the runner
    the first pass did not list expired on it.
    """
    other = runner_record("i-other", runner_id=1)
    target = runner_record("i-lease", runner_id=2)
    payloads = [
        {"total_count": 1, "runners": [other]},
        {"total_count": 2, "runners": [other, target]},
    ]
    result, ec2, github, _ = lease_poll(runners=[other, target], runner_payloads=payloads)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    reads = [url for url in github.requests if "/actions/runners?" in url]
    assert len(reads) == 2, reads


def case_a_total_count_that_moves_between_pages_makes_the_roster_unread():
    """A count that differs from one page to the next is a roster that changed mid-read.

    Only the between-page comparison can catch this fixture. The pages never
    hold more records than the count they claim, so the overflow check passes
    on every one of them, and the read adds up to the LAST count reported and
    passes the completeness check too. Reading `total = reported` on every
    page, as this used to, accepts all three pages and reports a complete
    roster that is missing whatever the shrinking count dropped.

    Both passes are served, so the check under test is the only thing holding
    the lease: read_roster_once() returning a short second pass would hold it
    whatever the per-page checks did.
    """
    records = [runner_record(f"i-other-{n}", runner_id=n + 1) for n in range(250)]
    payloads = [
        {"total_count": 300, "runners": records[:100]},
        {"total_count": 250, "runners": records[100:200]},
        {"total_count": 250, "runners": records[200:]},
    ]
    result, ec2, _, _ = lease_poll(runners=records, runner_payloads=payloads * 2)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result


def case_more_records_than_total_count_makes_the_roster_unread():
    """A page holding more records than GitHub's own count is not a complete read.

    The completeness check after the loop is `collected < total`, so a read
    that came back with MORE than total_count passes it. That is a roster
    whose count and contents disagree, and treating it as authoritative lets
    it expire the lease of any instance it does not list.
    """
    result, ec2, _, _ = lease_poll(
        runners=[runner_record(f"i-other-{n}", runner_id=n + 1) for n in range(100)],
        runners_total=50)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["expired"] == [], result
    assert result["held"] == ["i-lease"], result


def case_a_duplicate_across_pages_makes_the_roster_unread():
    """A repeated name or id is a page boundary that shifted under the read.

    The record it displaced is missing from an answer that still adds up to
    total_count, so the count check passes and the missing runner reads as
    absent. Repeated by both fields, by name only and by id only.
    """
    first = [runner_record(f"i-other-{n}", runner_id=n + 1) for n in range(100)]
    repeats = [
        first[-1],
        {**first[-1], "name": "runner-i-different-name"},
        {**first[-1], "id": 1001},
    ]
    for repeated in repeats:
        # Both passes served. With only the two pages, the second pass reads
        # one short page and the read is held by the completeness check
        # instead of by the identity check this case is about.
        result, ec2, _, _ = lease_poll(runners=first, runner_payloads=[
            {"total_count": 101, "runners": first},
            {"total_count": 101, "runners": [repeated]},
        ] * 2)
        assert still_running(ec2) == ["i-lease"], (repeated, still_running(ec2))
        assert result["held"] == ["i-lease"], (repeated, result)


def case_a_repeated_name_degrades_the_webhook_to_the_instance_count():
    """The launch-side reader, same defect class: a repeated record hides a displaced one.

    A full first page and a second page that repeats its last record add up
    to total_count, so the count check passes. The fourth live runner is the
    record the repeat displaced; unseen, it dropped `counted` to three and
    metal was launched into a full pool.
    """
    live = [{"id": n + 1, "name": f"runner-i-live{n}", "status": "online", "busy": True}
            for n in range(3)]
    filler = [{"id": 100 + n, "name": f"runner-i-fill{n}", "status": "online", "busy": True}
              for n in range(97)]
    result, ec2 = webhook_capacity_poll(live, runner_payloads=[
        {"total_count": 101, "runners": live + filler},
        {"total_count": 101, "runners": [live[2]]},
    ])
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result


def case_a_total_count_that_moves_between_pages_degrades_the_webhook():
    """The launch-side reader, same fixture: only the between-page check catches it.

    Every page holds fewer records than the count it reports, and the read
    adds up to the last count, so the overflow check and the completeness
    check both pass. Accepted, the roster names none of the four live
    instances, `counted` falls to zero and metal is launched into a full pool.
    """
    records = [{"id": n + 1, "name": f"runner-i-other-{n}", "status": "online", "busy": True}
               for n in range(250)]
    result, ec2 = webhook_capacity_poll(records, runner_payloads=[
        {"total_count": 300, "runners": records[:100]},
        {"total_count": 250, "runners": records[100:200]},
        {"total_count": 250, "runners": records[200:]},
    ])
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result


def case_more_records_than_total_count_degrades_the_webhook():
    """A page holding more records than total_count is not a readable roster either."""
    records = [{"id": n + 1, "name": f"runner-i-other-{n}", "status": "online", "busy": True}
               for n in range(100)]
    result, ec2 = webhook_capacity_poll(records, runners_total=50)
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result


def case_every_ceiling_terminate_precedes_every_optional_tag_write():
    """No instance in the fleet is written to before every ceiling has been applied.

    A younger instance is listed first and needs both the seen stamp and a
    lease renewal. Either write can stall for the rest of the budget, and the
    older instance later in the response must already be gone by then.
    Ordering per instance did not cover this: the older one had not been
    reached when the younger one's writes ran.
    """
    journal = []
    ec2 = FakeEC2([
        instance("i-young", "c7g.metal", "running", 60, arch="arm64",
                 tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()}),
        instance("i-aged", "c7g.metal", "running", HARD_CEILING_MINUTES + 5,
                 arch="arm64",
                 tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()}),
    ], journal=journal)
    github = FakeGitHub(runners=[runner_record("i-young", runner_id=1),
                                 runner_record("i-aged", runner_id=2)], journal=journal)
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github)["handler"]({}, None)
    assert result["hard_killed"] == ["i-aged"], result
    assert result["renewed"] == ["i-young"], result
    kill = journal.index("ec2:terminate:i-aged")
    tag_writes = [i for i, e in enumerate(journal) if e.startswith("ec2:create_tags:")]
    assert tag_writes, journal
    assert all(kill < i for i in tag_writes), journal


def case_every_ceiling_terminate_precedes_the_pat_read_and_the_roster():
    """The SSM read and the roster pages are optional to a launch-time-only bound.

    Both can stall (SSM at boto3's retried read timeout, GitHub at ten pages
    of a 10-second call each), so both come after every ceiling termination.
    """
    journal = []
    ec2 = FakeEC2([
        instance("i-aged-a", "c7g.metal", "running", HARD_CEILING_MINUTES + 5,
                 arch="arm64",
                 tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()}),
        instance("i-aged-b", "c7g.metal", "running", HARD_CEILING_MINUTES + 10,
                 arch="arm64",
                 tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()}),
    ], journal=journal)
    github = FakeGitHub(runners=[runner_record("i-aged-a", runner_id=1),
                                 runner_record("i-aged-b", runner_id=2)], journal=journal)
    ssm = FakeSSM(journal=journal)
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, github, ssm=ssm)["handler"]({}, None)
    assert sorted(result["hard_killed"]) == ["i-aged-a", "i-aged-b"], result
    kills = [i for i, e in enumerate(journal) if e.startswith("ec2:terminate:i-aged-")]
    assert len(kills) == 2, journal
    optional = [i for i, e in enumerate(journal)
                if e.startswith("ssm:") or e.startswith("github:GET:")]
    assert optional, journal
    assert all(kill < i for kill in kills for i in optional), journal


def case_the_pat_value_is_never_logged():
    """Not even a prefix of the credential belongs in CloudWatch."""
    secret = "ghp_super_secret_value"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cleanup(FakeEC2(), FakeGitHub(), ssm=FakeSSM(pat=secret))["handler"]({}, None)
    assert secret not in buf.getvalue(), buf.getvalue()
    assert secret[:10] not in buf.getvalue(), buf.getvalue()


def case_a_rejected_renewal_write_is_not_reported_as_a_renewal():
    """`renewed` names leases EC2 accepted, not leases the poll meant to extend."""
    result, ec2, _, _ = lease_poll(runners=[runner_record("i-lease", busy=True)],
                                   create_tags_error=OSError("CreateTags unavailable"))
    assert result["renewed"] == [], result
    assert result["held"] == ["i-lease"], result
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    tags = {t["Key"]: t["Value"] for t in ec2.instances[0]["Tags"]}
    assert tags["LeaseExpires"] == (NOW - timedelta(minutes=45)).isoformat(), tags


def case_lease_expiry_rechecks_the_runner_before_termination():
    """A job handed out after the roster snapshot survives the lease.

    The roster read at the top of the poll says idle; the runner picked up a
    job in the seconds since. The expired lease terminated on the snapshot.
    """
    result, ec2, _, _ = lease_poll(
        runners=[runner_record("i-lease", busy=False)],
        recheck={77: runner_record("i-lease", busy=True)})
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["expired"] == [], result
    assert result["held"] == ["i-lease"], result


def case_a_lease_expiry_recheck_that_answers_for_another_runner_holds():
    """GET /runners/{id} answering with a different name is not this runner."""
    result, ec2, _, _ = lease_poll(
        runners=[runner_record("i-lease", busy=False)],
        recheck={77: {"id": 77, "name": "runner-i-other", "busy": False, "status": "online"}})
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result


def case_a_protocol_box_that_never_claimed_registration_dies_at_its_lease():
    """The ddb-v1 form of the never-registered case, reaped through the claim.

    No row means bootstrap never got as far as registering. Cleanup creates
    the `reaping` row first, so a bootstrap that is about to register finds
    the row taken and refuses to start the service, and only then terminates.
    """
    dynamodb = FakeDynamoDB()
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"},
        runners=[runner_record("i-other")], dynamodb=dynamodb)
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result
    claims = dynamodb.ops("put_item")
    assert len(claims) == 1, claims
    assert claims[0]["ConditionExpression"] == "attribute_not_exists(InstanceArn)", claims
    assert dynamodb.items[instance_arn("i-lease")] == reaping_item()


def case_a_lost_registration_row_cannot_reap_a_runner_that_did_register():
    """An absent row is proof of "never registered" only while nothing contradicts it.

    The row is the only registration evidence ddb-v1 writes, and nothing in the
    protocol deletes one, so cleanup reads absence as "bootstrap never got that
    far". That inference fails the moment a row is lost rather than never
    written: replacing the table (a rename, a hash_key change, a bad import) or
    deleting one item leaves a registered, busy runner with no row, and the
    no-row branch then claims `reaping` and terminates it at its next lease
    expiry, without deregistering it. The signals ejc3/aws#46 established say
    the opposite about the same instance: the roster lists the runner, a
    RunnerSeenAt stamp records that some poll saw it, or the lease is later
    than the launcher's initial one. Any of them makes the missing row a lost
    row, and the instance is held to the ceiling.
    """
    seen = (NOW - timedelta(minutes=6)).isoformat()
    ddb = {"RunnerRegistrationProtocol": "ddb-v1"}
    signals = [
        # The roster lists this runner right now, busy and online.
        ("on the roster", dict(ddb), [runner_record("i-lease")], 60, 45),
        # Absent from a readable roster, but stamped by an earlier poll.
        ("seen before", dict(ddb, RunnerSeenAt=seen), [runner_record("i-other")], 60, 45),
        # No stamp, but the lease sits past launch + LEASE_DURATION_MINUTES, so
        # some poll renewed it on a busy answer.
        ("lease renewed", dict(ddb), [runner_record("i-other")], 130, 5),
    ]
    for label, tags, runners, age_minutes, lease_minutes_ago in signals:
        dynamodb = FakeDynamoDB()
        result, ec2, _, _ = lease_poll(
            tags=tags, runners=runners, dynamodb=dynamodb,
            age_minutes=age_minutes, lease_minutes_ago=lease_minutes_ago)
        assert still_running(ec2) == ["i-lease"], (label, still_running(ec2))
        assert terminated_ids(ec2) == [], (label, terminated_ids(ec2))
        assert result["expired"] == [], (label, result)
        assert result["held"] == ["i-lease"], (label, result)
        # No claim written: the row is missing, not this Lambda's to take.
        assert dynamodb.ops("put_item") == [], (label, dynamodb.ops("put_item"))


def case_a_lost_registration_row_beside_a_reaping_claim_still_holds():
    """The same rule after a claim: a reaping row does not outrank the roster.

    A `reaping` row is a claim an earlier poll won, and the retry path
    terminates on it. If GitHub now lists that runner, the claim was won
    against a bootstrap that had already registered, so the terminate waits
    for the roster to stop listing it or for the ceiling.
    """
    dynamodb = FakeDynamoDB([reaping_item()])
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"},
        runners=[runner_record("i-lease")], dynamodb=dynamodb)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["expired"] == [], result
    assert result["held"] == ["i-lease"], result


def case_a_protocol_box_is_held_until_its_lease_expires():
    """Cleanup does not race a bootstrap that is still inside its lease."""
    dynamodb = FakeDynamoDB()
    result, ec2, _, _ = lease_poll(
        age_minutes=30, lease_minutes_ago=-5,
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[], dynamodb=dynamodb)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert dynamodb.ops("put_item") == [], dynamodb.ops("put_item")


def case_a_registered_row_is_read_by_id_when_the_roster_omits_it():
    """The row's runner id outranks absence from the roster.

    Under the legacy rule this instance holds (its lease was renewed). With a
    row, cleanup asks GitHub about that exact id instead and renews on the
    answer.
    """
    exact = runner_record("i-lease", busy=True, runner_id=77)
    result, ec2, _, _ = lease_poll(
        age_minutes=70, lease_minutes_ago=5,
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[],
        dynamodb=FakeDynamoDB([registered_item()]), recheck={77: exact})
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["renewed"] == ["i-lease"], result
    assert result["held"] == [], result


def case_a_registered_row_whose_runner_reads_idle_expires_the_lease():
    """Positive evidence from the exact read is acted on: idle and expired goes."""
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[],
        dynamodb=FakeDynamoDB([registered_item()]),
        recheck={77: runner_record("i-lease", busy=False)})
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result


def case_a_registered_row_whose_lookup_fails_holds():
    """A 404 or a transport error on the exact read is absence of evidence."""
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[],
        dynamodb=FakeDynamoDB([registered_item()]), recheck={77: None})
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result


def case_a_registered_row_whose_lookup_names_another_runner_holds():
    """A reused or malformed runner id is not evidence about this instance."""
    mismatches = [
        {"id": 78, "name": "runner-i-lease", "busy": False, "status": "online"},
        {"id": 77, "name": "runner-i-other", "busy": False, "status": "online"},
        {"id": True, "name": "runner-i-lease", "busy": False, "status": "online"},
    ]
    for exact in mismatches:
        result, ec2, _, _ = lease_poll(
            tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[],
            dynamodb=FakeDynamoDB([registered_item()]), recheck={77: exact})
        assert still_running(ec2) == ["i-lease"], (exact, still_running(ec2))
        assert result["held"] == ["i-lease"], (exact, result)


def case_a_malformed_registration_row_holds():
    """A row that does not describe this instance decides nothing."""
    rows = []
    bad_id = registered_item()
    bad_id["RunnerId"] = {"N": "not-an-integer"}
    rows.append(bad_id)
    bad_name = registered_item(runner_name="runner-i-other")
    rows.append(bad_name)
    bad_state = registered_item()
    bad_state["State"] = {"S": "pending"}
    rows.append(bad_state)
    other = registered_item("i-other")
    other["InstanceArn"] = {"S": instance_arn("i-lease")}
    rows.append(other)
    # A `reaping` row is a claim, and a claim with no ReapingAt is not one this
    # Lambda can show it made. Read as a claim, it terminates the instance.
    undated = reaping_item()
    del undated["ReapingAt"]
    rows.append(undated)
    for row in rows:
        dynamodb = FakeDynamoDB([row])
        result, ec2, _, _ = lease_poll(
            tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[], dynamodb=dynamodb)
        assert still_running(ec2) == ["i-lease"], (row, still_running(ec2))
        assert result["held"] == ["i-lease"], (row, result)
        assert dynamodb.ops("put_item") == [], (row, dynamodb.ops("put_item"))


def case_a_registration_row_that_describes_another_instance_renews_nothing():
    """The row's identity fields decide whose GitHub answer this lease is read from.

    Each row here would read as a live registration if its identity were not
    checked, and GitHub answers busy and online for the id it carries: an
    accepted row renews THIS instance's lease on evidence about a different
    box. Refused, the row is unread, and unread holds. The lease-expiry
    recheck cannot stand in for these checks: it asks about the instance's own
    runner name, so it never sees a row that renewed the lease before it.
    """
    wrong_instance_id = registered_item()
    wrong_instance_id["InstanceId"] = {"S": "i-other"}
    wrong_arn = registered_item()
    wrong_arn["InstanceArn"] = {"S": instance_arn("i-other")}
    here = runner_record("i-lease", busy=True, runner_id=77)
    there = {"id": 77, "name": "runner-i-other", "busy": True, "status": "online"}
    rows = [
        ("InstanceId names another instance", wrong_instance_id, here),
        ("InstanceArn names another instance", wrong_arn, here),
        ("RunnerName is another runner", registered_item(runner_name="runner-i-other"), there),
    ]
    for label, row, answer in rows:
        dynamodb = FakeDynamoDB()
        # Keyed by the instance under test whatever the row's own ARN says:
        # the key is what GetItem asks for, the attribute is what is checked.
        dynamodb.items[instance_arn("i-lease")] = row
        result, ec2, _, _ = lease_poll(
            age_minutes=70, lease_minutes_ago=5,
            tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[],
            dynamodb=dynamodb, recheck={77: answer})
        assert result["renewed"] == [], (label, result)
        assert result["held"] == ["i-lease"], (label, result)
        assert still_running(ec2) == ["i-lease"], (label, still_running(ec2))


def case_an_unreadable_registration_table_holds():
    """A throttled or unauthorised consistent read is not a missing row."""
    dynamodb = FakeDynamoDB(get_error=OSError("GetItem unavailable"))
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[], dynamodb=dynamodb)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert dynamodb.ops("put_item") == [], dynamodb.ops("put_item")


def case_a_missing_account_id_makes_every_registration_unread():
    """Without the account id no row can be addressed, and unread holds."""
    dynamodb = FakeDynamoDB()
    all_tags = {"LeaseExpires": (NOW - timedelta(minutes=45)).isoformat(),
                "RunnerRegistrationProtocol": "ddb-v1"}
    ec2 = FakeEC2([instance("i-lease", "c7g.metal", "running", 60, arch="arm64", tags=all_tags)])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, FakeGitHub(runners=[]), dynamodb=dynamodb,
                         env={"RUNNER_ACCOUNT_ID": ""})["handler"]({}, None)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert dynamodb.calls == [], dynamodb.calls


def case_a_reaping_claim_that_did_not_land_holds():
    """A rejected PutItem with no row behind it is a claim not made."""
    dynamodb = FakeDynamoDB(put_error=OSError("PutItem throttled"))
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[], dynamodb=dynamodb)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert dynamodb.items == {}, dynamodb.items


def case_a_reaping_claim_whose_answer_was_lost_is_resolved_by_a_consistent_read():
    """A PutItem that landed and then timed out is found by reading the row."""
    dynamodb = FakeDynamoDB(put_error=OSError("response lost"), write_then_error=True)
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[], dynamodb=dynamodb)
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result
    assert dynamodb.items[instance_arn("i-lease")] == reaping_item()


def case_an_existing_reaping_row_retries_a_failed_terminate():
    """A claim won on an earlier poll is still the claim; no second write."""
    dynamodb = FakeDynamoDB([reaping_item()])
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[], dynamodb=dynamodb)
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result
    assert dynamodb.ops("put_item") == [], dynamodb.ops("put_item")


def case_a_bootstrap_that_registers_between_the_read_and_the_claim_wins():
    """ConditionalCheckFailedException is bootstrap winning, not an error to retry."""
    dynamodb = FakeDynamoDB(concurrent_item=registered_item())
    result, ec2, _, _ = lease_poll(
        tags={"RunnerRegistrationProtocol": "ddb-v1"}, runners=[],
        dynamodb=dynamodb, recheck={77: None})
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert len(dynamodb.ops("put_item")) == 1, dynamodb.ops("put_item")
    assert dynamodb.items[instance_arn("i-lease")] == registered_item()


def case_a_protocol_husk_beside_a_held_legacy_runner():
    """One poll, one held legacy box and one ddb-v1 husk reaped through its claim."""
    expired = (NOW - timedelta(minutes=45)).isoformat()
    ec2 = FakeEC2([
        instance("i-live", "c7g.metal", "running", 60, arch="arm64",
                 tags={"LeaseExpires": expired,
                       "RunnerSeenAt": (NOW - timedelta(minutes=6)).isoformat()}),
        instance("i-husk", "c7g.metal", "running", 60, arch="arm64",
                 tags={"LeaseExpires": expired, "RunnerRegistrationProtocol": "ddb-v1"}),
    ])
    dynamodb = FakeDynamoDB()
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, FakeGitHub(runners=[runner_record("i-other")]),
                         dynamodb=dynamodb)["handler"]({}, None)
    assert still_running(ec2) == ["i-live"], still_running(ec2)
    assert terminated_ids(ec2) == ["i-husk"], terminated_ids(ec2)
    assert result["held"] == ["i-live"], result
    assert result["expired"] == ["i-husk"], result
    assert dynamodb.items[instance_arn("i-husk")] == reaping_item("i-husk")


def case_an_unreadable_registration_table_does_not_suppress_the_ceiling():
    """The ceiling is derived from launch time alone; DynamoDB is not consulted."""
    dynamodb = FakeDynamoDB(get_error=OSError("GetItem unavailable"))
    ec2 = FakeEC2([instance("i-aged", "c7g.metal", "running", HARD_CEILING_MINUTES + 5,
                            arch="arm64",
                            tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat(),
                                  "RunnerRegistrationProtocol": "ddb-v1"})])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, FakeGitHub(runners=[]), dynamodb=dynamodb)["handler"]({}, None)
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result
    assert dynamodb.calls == [], dynamodb.calls


def case_a_lost_registration_row_cannot_hold_an_instance_past_the_ceiling():
    """Holding a lost-row instance is a lease decision, and the ceiling is not one.

    Every signal that makes a missing row hold is present here at once: the
    row is gone, GitHub lists the runner busy and online, and the instance
    carries a RunnerSeenAt stamp. It is still terminated, by the pre-pass that
    reads nothing but InstanceId and LaunchTime and runs before the PAT, the
    roster and the table.
    """
    dynamodb = FakeDynamoDB()
    ec2 = FakeEC2([instance("i-aged", "c7g.metal", "running", HARD_CEILING_MINUTES + 5,
                            arch="arm64",
                            tags={"LeaseExpires": (NOW - timedelta(minutes=45)).isoformat(),
                                  "RunnerRegistrationProtocol": "ddb-v1",
                                  "RunnerSeenAt": (NOW - timedelta(minutes=6)).isoformat()})])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, FakeGitHub(runners=[runner_record("i-aged")]),
                         dynamodb=dynamodb)["handler"]({}, None)
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result
    assert result["held"] == [], result
    assert dynamodb.calls == [], dynamodb.calls


def case_a_held_lease_does_not_shield_the_instance_beside_it():
    """The verdict is per instance, and one poll can hold one and reap another.

    This is the mixed answer the RunnerSeenAt tag exists for: a roster that
    lists neither box, one of which was registered a moment ago and one of
    which never registered at all. Holding both would leave a husk running to
    the 13h30m ceiling; reaping both is ejc3/aws#45.
    """
    expired = (NOW - timedelta(minutes=45)).isoformat()
    ec2 = FakeEC2([
        instance("i-live", "c7g.metal", "running", 60, arch="arm64",
                 tags={"LeaseExpires": expired,
                       "RunnerSeenAt": (NOW - timedelta(minutes=6)).isoformat()}),
        instance("i-husk", "c7g.metal", "running", 60, arch="arm64",
                 tags={"LeaseExpires": expired}),
    ])
    with contextlib.redirect_stdout(io.StringIO()):
        result = cleanup(ec2, FakeGitHub(runners=[runner_record("i-other")]))["handler"]({}, None)
    assert still_running(ec2) == ["i-live"], still_running(ec2)
    assert terminated_ids(ec2) == ["i-husk"], terminated_ids(ec2)
    assert result["held"] == ["i-live"], result
    assert result["expired"] == ["i-husk"], result


def case_a_held_runner_is_still_terminated_at_the_ceiling():
    """Holding a lease must not become a way to outlive the ceiling.

    The lease phase never sees an instance this old - the age policy answers
    first and its ceiling is derived from launch_time alone - but that ordering
    is the only thing making a held lease safe, so it is pinned here as well as
    at the age policy.
    """
    result, ec2, _, out = lease_poll(age_minutes=HARD_CEILING_MINUTES + 5,
                                     runners_error=True)
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-lease"], result
    assert result["held"] == [], result
    assert "HARD-CEILING KILL" in out, out


def case_a_listed_runner_with_no_id_holds_its_lease():
    """A record GitHub sent for THIS runner, minus its id, is not absence.

    get_runners() dropped any record without a usable name or id and still
    counted it toward total_count, so the read passed the completeness check
    and simply did not list the runner. For an instance without RunnerSeenAt,
    which is every instance on the first poll after that tag ships, that is
    the never-registered EXPIRE verdict, and an expired lease terminates it:
    a working runner destroyed on a field GitHub did not send. The roster is
    unread instead, the lease is held, and the ceiling stays the bound.
    """
    result, ec2, _, out = lease_poll(
        runners=[{"id": None, "name": "runner-i-lease", "busy": True, "status": "online"}])
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert result["held"] == ["i-lease"], result
    assert result["expired"] == [], result
    assert "ROSTER UNREAD" in out, out


def case_a_nameless_runner_record_makes_the_roster_unread():
    """A record with no name could be any instance's, so no runner is absent for sure.

    Dropping it and keeping the rest is a roster that omits one runner without
    saying which. Every instance without RunnerSeenAt then reads as never
    registered, and the one whose lease has lapsed is terminated.
    """
    result, ec2, _, out = lease_poll(
        runners=[{"id": 5, "name": None, "busy": True, "status": "online"},
                 runner_record("i-other")])
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert "ROSTER UNREAD" in out, out


def case_a_malformed_roster_does_not_suppress_the_ceiling():
    """Unread on a bad record must not become a way past 13h30m.

    The same shape as case_a_held_runner_is_still_terminated_at_the_ceiling,
    with the roster unread for a record it cannot represent rather than for
    an exception. The ROSTER UNREAD line is asserted so this case is known to
    have taken that path and not a readable roster.
    """
    result, ec2, _, out = age_poll(
        HARD_CEILING_MINUTES + 5,
        runners=[{"id": None, "name": "runner-i-aged", "busy": True, "status": "online"}])
    assert "ROSTER UNREAD" in out, out
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert still_running(ec2) == [], still_running(ec2)
    assert result["hard_killed"] == ["i-aged"], result
    assert result["held"] == [], result
    assert "HARD-CEILING KILL" in out, out


def case_a_busy_record_with_no_status_holds_its_lease():
    """busy=true with no status is a missing field, not the wedged host.

    The online qualifier on a busy record exists for a host that wedges
    mid-job: it keeps its job, reports busy=true and goes OFFLINE, and
    renewing on that made it immortal (ejc3/fcvm#871). A record with no
    status at all took that same EXPIRE path, so a working runner whose
    record arrived without the field was terminated at its lease. Held
    instead: not renewed, so the ceiling still bounds a wedge, and not
    expired on a field GitHub did not send.
    """
    result, ec2, _, _ = lease_poll(
        runners=[{"id": 5, "name": "runner-i-lease", "busy": True}])
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert result["renewed"] == [], result
    lease_writes = [kw for kw in ec2.ops("create_tags")
                    if any(t["Key"] == "LeaseExpires" for t in kw["Tags"])]
    assert lease_writes == [], lease_writes


def case_a_busy_but_offline_runner_still_expires():
    """The wedged host of ejc3/fcvm#871: an explicit offline still lets the lease lapse."""
    result, ec2, _, _ = lease_poll(
        runners=[runner_record("i-lease", busy=True, status="offline")])
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result
    assert result["held"] == [], result


def case_an_unreadable_total_count_makes_the_roster_unread():
    """A total_count the completeness check cannot compare must block, not pass.

    The check was `isinstance(total, int) and collected < total`, so a
    total_count GitHub sent as anything but an integer skipped the comparison
    and the read was accepted as complete on the page-size heuristic alone.
    """
    result, ec2, _, out = lease_poll(runners=[runner_record("i-other")], runners_total="7")
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert "ROSTER UNREAD" in out, out

def case_a_roster_without_total_count_is_unread():
    """No total_count is a completeness check with nothing to compare against.

    A response that omits the key left `total` as None and the read was
    accepted as complete on the page-size heuristic alone, the same hole a
    non-integer total_count opened. A short array with no total then reads
    as "that runner is not registered".
    """
    result, ec2, _, out = lease_poll(runners=[runner_record("i-other")], omit_runners_total=True)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert "ROSTER UNREAD" in out, out


def case_a_renewed_lease_is_proof_the_runner_once_registered():
    """A LeaseExpires past the launcher's initial expiry can only come from RENEW.

    The launcher stamps LeaseExpires at request time plus LEASE_DURATION_MINUTES,
    before RunInstances, so it is always earlier than launch_time plus that
    duration. Anything later was written by renew_lease(), which only runs
    when GitHub listed the runner online and busy. That is registration
    evidence the RunnerSeenAt stamp does not depend on: it is there for every
    instance that was busy before the tag shipped, and for one whose stamp
    write failed. Here the instance is 3h old with a lease that expired 45m
    ago, so the lease was renewed at launch+135m; absent from a readable
    roster, it is held rather than read as a box that never joined.
    """
    result, ec2, _, _ = lease_poll(age_minutes=180, lease_minutes_ago=45,
                                   runners=[runner_record("i-other")])
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert result["held"] == ["i-lease"], result
    assert result["expired"] == [], result


def case_a_never_registered_box_with_an_old_initial_lease_still_dies():
    """The evidence above must not over-reach: an initial lease is not a renewal.

    Same age as the case above, but LeaseExpires is exactly the launcher's
    initial value (launch + 60m), so nothing ever renewed it, and a readable
    roster that omits the runner is the real answer about it.
    """
    result, ec2, _, _ = lease_poll(age_minutes=180, lease_minutes_ago=120,
                                   runners=[runner_record("i-other")])
    assert terminated_ids(ec2) == ["i-lease"], terminated_ids(ec2)
    assert result["expired"] == ["i-lease"], result


def case_a_failed_seen_stamp_cannot_turn_a_renewed_runner_into_a_husk():
    """Two polls, with the standalone RunnerSeenAt write failing on the first.

    Poll one lists the runner busy and online: its lease is renewed and the
    seen stamp is attempted and rejected, and the error is swallowed. Poll two
    reads a roster that omits the runner, one lease later. With only the tag
    as evidence the instance is indistinguishable from a box that never
    joined, and its expired lease terminates a runner that was working an
    hour ago. The renewal itself has to carry the proof.
    """
    inst = instance("i-lease", "c7g.metal", "running", 180, arch="arm64",
                    tags={"LeaseExpires": (NOW + timedelta(minutes=30)).isoformat()})
    ec2 = FakeEC2([inst], create_tags_error=OSError("CreateTags throttled"),
                  create_tags_reject_keys={"RunnerSeenAt"})
    with contextlib.redirect_stdout(io.StringIO()):
        first = cleanup(ec2, FakeGitHub(runners=[runner_record("i-lease")]))["handler"]({}, None)
    assert first["renewed"] == ["i-lease"], first

    # The clock is frozen, so the hour between the polls is applied to the
    # lease: expired 45m ago, which is launch+135m, later than any initial lease.
    tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
    tags["LeaseExpires"] = (NOW - timedelta(minutes=45)).isoformat()
    inst["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]

    with contextlib.redirect_stdout(io.StringIO()):
        second = cleanup(ec2, FakeGitHub(runners=[runner_record("i-other")]))["handler"]({}, None)
    assert still_running(ec2) == ["i-lease"], still_running(ec2)
    assert terminated_ids(ec2) == [], terminated_ids(ec2)
    assert second["held"] == ["i-lease"], second


def case_a_renewal_carries_the_seen_stamp_in_the_same_write():
    """A renewed lease and the seen stamp land together or not at all.

    Two separate CreateTags calls have two separate fates, and the one that
    matters for a working runner is the renewal. Carrying RunnerSeenAt on the
    renewal write means a runner whose lease was ever renewed is marked seen
    by construction, and a poll that renews leaves nothing to a second call.
    """
    result, ec2, _, _ = lease_poll(runners=[runner_record("i-lease")])
    assert result["renewed"] == ["i-lease"], result
    renewals = [{t["Key"] for t in kw["Tags"]} for kw in ec2.ops("create_tags")
                if any(t["Key"] == "LeaseExpires" for t in kw["Tags"])]
    assert renewals == [{"LeaseExpires", "RunnerSeenAt"}], renewals
    tags = {t["Key"] for t in ec2.instances[0]["Tags"]}
    assert {"LeaseExpires", "RunnerSeenAt"} <= tags, tags


def case_the_ceiling_terminate_precedes_any_tag_write_to_that_instance():
    """No CreateTags for an instance may run ahead of its ceiling decision.

    The seen stamp ran before age_policy(). CreateTags can stall for the rest
    of the Lambda's budget (boto3 retries a 60s read timeout), a timeout is
    not catchable, and the sweep is the only thing enforcing the lifetime
    bound, so a write that hangs ahead of the ceiling check defers the
    ceiling. The ceiling terminate has to be the first thing done to an
    instance that is over it.
    """
    result, ec2, _, _ = age_poll(HARD_CEILING_MINUTES + 5, runners=[runner_record()])
    assert result["hard_killed"] == ["i-aged"], result
    kill = ec2.journal.index("ec2:terminate:i-aged")
    writes = [i for i, e in enumerate(ec2.journal) if e.startswith("ec2:create_tags:i-aged:")]
    assert all(kill < w for w in writes), ec2.journal


def case_a_failed_seen_stamp_does_not_block_the_ceiling():
    """A rejected CreateTags must not escape into the sweep and skip the kill."""
    result, ec2, _, out = age_poll(HARD_CEILING_MINUTES + 5, runners=[runner_record()],
                                   create_tags_error=OSError("CreateTags rejected"))
    assert terminated_ids(ec2) == ["i-aged"], terminated_ids(ec2)
    assert result["hard_killed"] == ["i-aged"], result
    assert "HARD-CEILING KILL" in out, out


def case_a_truncated_runner_list_degrades_to_the_instance_count():
    """The webhook Lambda, same defect class: a short page is not four dead runners.

    get_online_runner_names() read one page and treated it as the whole roster,
    so online runners past the page boundary counted as absent, `counted` fell
    below the cap and the launcher added metal to a pool that was already full.
    Unknown health already has a defined answer here - degrade to the instance
    count - and a truncated read is unknown health.
    """
    ec2 = FakeEC2([instance(f"i-live{n}", "c5d.metal", "running", 60) for n in range(4)])
    roster = [{"id": n, "name": f"runner-i-live{n}", "status": "online", "busy": True}
              for n in range(4)]
    github = FakeGitHub(runners=roster[:1], runners_total=4)
    event = {"body": json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "X64"]},
    })}
    result = webhook(ec2, github=github)["handler"](event, None)
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result


def case_a_nameless_runner_record_cannot_stop_a_launch():
    """One malformed record must not take scale-up down with it.

    The set comprehension that read r['name'] sat outside the try in
    get_online_runner_names, so a record without a name raised KeyError out of
    get_capacity and out of the handler. GitHub delivers a workflow_job event
    once and never redelivers it, so the queued job waits for the next poll.
    The record now makes runner health unknown, which degrades the cap to the
    instance count: one instance of four, so the launch still goes ahead.
    """
    ec2 = FakeEC2([instance("i-live", "c5d.metal", "running", 60)])
    github = FakeGitHub(runners=[{"id": 1, "status": "online", "busy": False}])
    event = {"body": json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "X64"]},
    })}
    result = webhook(ec2, github=github)["handler"](event, None)
    assert len(launched_types(ec2)) == 1, launched_types(ec2)
    assert result["body"].startswith("Launched 1 x86_64 runner"), result

def webhook_capacity_poll(roster, live=4, runners_total=None, omit_runners_total=False,
                          runner_payloads=None):
    """One queued x86 event against `live` running instances and this roster."""
    ec2 = FakeEC2([instance(f"i-live{n}", "c5d.metal", "running", 60) for n in range(live)])
    event = {"body": json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "X64"]},
    })}
    github = FakeGitHub(runners=roster, runners_total=runners_total,
                        omit_runners_total=omit_runners_total,
                        runner_payloads=runner_payloads)
    result = webhook(ec2, github=github)["handler"](event, None)
    return result, ec2


def full_x86_roster(first):
    """Four online runners for the four instances webhook_capacity_poll() creates,
    with the first record replaced by `first`."""
    return [first] + [{"id": n, "name": f"runner-i-live{n}", "status": "online", "busy": True}
                      for n in range(1, 4)]


def case_a_nameless_runner_record_degrades_the_webhook_to_the_instance_count():
    """A record with no name is an online runner the cap cannot see.

    Skipping it kept the launcher up (the case above) and quietly took one
    runner out of `counted`: a full pool of four read as three online and the
    launcher added metal to it. Unknown health already has a defined answer
    here, the instance count, and a roster holding a record it cannot place
    is unknown health.
    """
    result, ec2 = webhook_capacity_poll(full_x86_roster({"id": 0, "status": "online", "busy": True}))
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result


def case_a_record_with_no_status_degrades_the_webhook_to_the_instance_count():
    """A record with no status is not offline; it is a field GitHub did not send.

    Read as offline it under-counts the pool by one, which is the expensive
    mistake this Lambda is documented to fail away from.
    """
    result, ec2 = webhook_capacity_poll(full_x86_roster({"id": 0, "name": "runner-i-live0", "busy": True}))
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result


def case_an_unreadable_total_count_degrades_the_webhook_to_the_instance_count():
    """The webhook's completeness check, disabled by a non-integer total_count."""
    roster = full_x86_roster({"id": 0, "name": "runner-i-live0", "status": "online", "busy": True})
    result, ec2 = webhook_capacity_poll(roster[:1], runners_total="4")
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result

def case_a_runner_list_without_total_count_degrades_the_webhook_to_the_instance_count():
    """No total_count is unknown health, for the same reason a non-integer one is."""
    roster = full_x86_roster({"id": 0, "name": "runner-i-live0", "status": "online", "busy": True})
    result, ec2 = webhook_capacity_poll(roster[:1], omit_runners_total=True)
    assert launched_types(ec2) == [], launched_types(ec2)
    assert result["body"] == "Max x86_64 runners (4) reached", result


def poll(github, env=None):
    """Run one cleanup poll and report what it asked the webhook to launch."""
    invoker = FakeLambdaClient()
    cleanup(FakeEC2(), github, invoker, env)["handler"]({}, None)
    return invoker.arch_invokes()


def case_real_work_behind_six_phantom_runs_is_found():
    """The six 2026-08-06 phantoms are newer than the real run and have no jobs.

    The runs endpoint returns newest first, so the phantoms fill the whole
    five-run sample and the real run is never reached. Driven through handler so
    the assertion is about runners launched, not about an internal helper.
    """
    phantoms = [run(31127540655 - i, "queued") for i in range(6)]
    real = run(31000000000, "queued")
    github = FakeGitHub(
        runs=phantoms + [real],
        jobs={real["id"]: [job("queued", "arm64"), job("queued", "arm64")],
              **{p["id"]: [] for p in phantoms}},
    )
    sample = github._payload("https://api.github.com/repos/x/actions/runs?status=queued&per_page=10")
    assert [r["id"] for r in sample["workflow_runs"]][:5] == [p["id"] for p in phantoms[:5]], \
        "the phantoms must occupy the whole old sample for this case to mean anything"
    assert poll(github) == {"arm64": 2}, poll(github)


def case_queued_jobs_in_an_in_progress_run_are_found():
    """Run 31202629167 held arm64 jobs queued for 45m while it was in_progress.

    status=queued never returns such a run, so its queued jobs were invisible.
    """
    live = run(31202629167, "in_progress")
    github = FakeGitHub(runs=[live], jobs={live["id"]: [
        job("in_progress", "arm64"), job("queued", "arm64"), job("queued", "x86_64"),
    ]})
    assert poll(github) == {"arm64": 1, "x86_64": 1}, poll(github)


def case_jobs_beyond_the_first_page_are_found():
    """A run with more jobs than one page must not hide the queued ones."""
    big = run(42, "queued")
    jobs = [job("completed", "arm64") for _ in range(100)] + [job("queued", "x86_64")]
    github = FakeGitHub(runs=[big], jobs={big["id"]: jobs})
    assert poll(github) == {"x86_64": 1}, poll(github)


def case_runs_beyond_the_first_page_are_found():
    """Real work sitting past the first page of runs must still be counted."""
    runs = [run(200000 - i, "queued") for i in range(101)]
    jobs = {r["id"]: [] for r in runs}
    jobs[runs[-1]["id"]] = [job("queued", "x86_64")]
    assert poll(FakeGitHub(runs=runs, jobs=jobs)) == {"x86_64": 1}


def case_queue_deeper_than_the_pool_launches_up_to_the_cap():
    """Seven queued arm64 jobs must fill the pool in one poll, not one per poll.

    And the whole deficit must travel in a SINGLE invocation per architecture:
    a burst of single-launch invocations, even serialized by reserved
    concurrency 1, can each miss the instance the previous one just launched
    (DescribeInstances is eventually consistent) and overshoot MAX_RUNNERS.
    """
    busy = run(7, "queued")
    github = FakeGitHub(runs=[busy], jobs={busy["id"]: [job("queued", "arm64") for _ in range(7)]})
    invoker = FakeLambdaClient()
    cleanup(FakeEC2(), github, invoker)["handler"]({}, None)
    assert invoker.arch_invokes() == {"arm64": 4}, invoker.arch_invokes()
    assert invoker.invocations_per_arch() == {"arm64": 1}, invoker.invocations_per_arch()


def case_stale_describe_cannot_overshoot_the_cap():
    """The launch budget is bounded in-process, immune to describe lag.

    FakeEC2 never adds launched instances to its describe results -- the
    worst-case eventual-consistency window, forever. A launch_count of 7
    against an empty pool of max 4 must launch exactly 4, because the
    handler's own loop is the counter, not a re-read of EC2 state.
    """
    ec2 = FakeEC2([])
    event = {"body": json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "ARM64"]},
        "launch_count": 7,
    })}
    result = webhook(ec2)["handler"](event, None)
    assert "Launched 4 arm64 runner(s)" in result["body"], result
    assert len(launched_types(ec2)) == 4, launched_types(ec2)


def case_github_style_header_casing_verifies():
    """GitHub sends X-Hub-Signature-256; payload format 1.0 preserves that case.

    The lowercase-only lookup 401'd every real delivery regardless of secret --
    the actual root cause of the dead webhook path. The handler must verify
    with the header exactly as GitHub capitalizes it.
    """
    import hmac as hmac_mod
    import hashlib
    secret = "testsecret"
    body = json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "ARM64"]},
    })
    sig = "sha256=" + hmac_mod.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    event = {"body": body, "requestContext": {}, "headers": {"X-Hub-Signature-256": sig}}
    ec2 = FakeEC2([])
    result = webhook(ec2, github=FakeGitHub(), env={"WEBHOOK_SECRET": secret})["handler"](event, None)
    assert "Launched 1 arm64 runner(s)" in result["body"], result


def case_public_webhook_cannot_amplify_launch_count():
    """launch_count is honored only on IAM-authed direct invokes.

    A real GitHub delivery arrives through API Gateway (requestContext
    present). Even correctly signed, its launch_count must be ignored --
    otherwise anyone with the webhook secret could 4x every launch.
    """
    import hmac as hmac_mod
    import hashlib
    secret = "testsecret"
    body = json.dumps({
        "action": "queued",
        "workflow_job": {"labels": ["self-hosted", "Linux", "ARM64"]},
        "launch_count": 4,
    })
    sig = "sha256=" + hmac_mod.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    event = {"body": body, "requestContext": {}, "headers": {"x-hub-signature-256": sig}}
    ec2 = FakeEC2([])
    result = webhook(ec2, env={"WEBHOOK_SECRET": secret})["handler"](event, None)
    assert "Launched 1 arm64 runner(s)" in result["body"], result
    assert len(launched_types(ec2)) == 1, launched_types(ec2)


def case_phantom_only_queue_launches_nothing():
    phantoms = [run(31127540655 - i, "queued") for i in range(6)]
    github = FakeGitHub(runs=phantoms, jobs={p["id"]: [] for p in phantoms})
    assert poll(github) == {}, poll(github)


def case_scan_stops_once_both_architectures_are_saturated():
    """Saturation is the only early exit, and it cannot change the outcome."""
    runs = [run(900 - i, "queued") for i in range(20)]
    jobs = {r["id"]: [job("queued", "arm64")] * 4 + [job("queued", "x86_64")] * 4 for r in runs}
    github = FakeGitHub(runs=runs, jobs=jobs)
    module = cleanup(FakeEC2(), github)
    demand = module["queued_demand"]("ghp_test", 4)
    assert demand == {"arm64": 4, "x86_64": 4}, demand
    job_calls = [u for u in github.requests if "/jobs" in u]
    assert len(job_calls) == 1, f"scanned {len(job_calls)} runs after saturation"


def case_only_self_hosted_jobs_count():
    hosted = run(11, "queued")
    github = FakeGitHub(runs=[hosted], jobs={hosted["id"]: [
        {"name": "Lint", "status": "queued", "labels": ["ubuntu-latest"]},
    ]})
    demand = cleanup(FakeEC2(), github)["queued_demand"]("ghp_test", 4)
    assert demand == {"arm64": 0, "x86_64": 0}, demand


CASES = [v for k, v in sorted(globals().items()) if k.startswith("case_")]


def main():
    global WEBHOOK_SRC, CLEANUP_SRC
    sources = extract_lambda_sources()
    if len(sources) != 2:
        print(f"FAIL: expected 2 Lambda heredocs in {TF_FILE.name}, found {len(sources)}")
        return 1
    WEBHOOK_SRC, CLEANUP_SRC = sources

    failures = 0
    for case in CASES:
        try:
            case()
            print(f"ok   {case.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL {case.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
