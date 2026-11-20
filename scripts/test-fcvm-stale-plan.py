#!/usr/bin/env python3
"""Real Terraform/provider proof against local synthetic SSM, without AWS credentials.

Extracts the deployed guard and its task dependency from this repository. An
unguarded saved plan first demonstrates the race; the actual guard must reject
the same version change before a local terraform_data resource is created.
"""
import http.server
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARAMETER_ARN = "arn:aws:ssm:us-west-1:123456789012:parameter/mock/image"


def block(text, header):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


def test_counted_region(directory, provider, locked, state, run):
    prod = (ROOT / "fcvm-fleet-prod.tf").read_text()
    module = block(prod, 'module "fcvm_fleet_prod_east1"')
    region = (ROOT / "modules/fcvm-fleet-region/main.tf").read_text()
    guard = block(prod, 'ephemeral "aws_ssm_parameter" "fcvm_fleet_prod_ami_release_guard"')
    # The read-only fixture supplies the planned SSM version and ARN for the actual guard.
    guard = guard.replace("aws.fcvm_fleet_east1", "aws.fcvm_fleet").replace(
        "aws_ssm_parameter.fcvm_fleet_prod_ami_release[0].", "data.aws_ssm_parameter.fcvm_coordinator_release.")
    case = directory / "counted"
    child = case / "region"
    child.mkdir(parents=True)
    (case / ".terraform.lock.hcl").write_text(locked + "\n")
    wiring = [re.search(rf"(?m)^  {name}\s*=.*$", module)[0] for name in ("count", "ami_release_parameter_version")]
    (case / "main.tf").write_text(provider + guard + """
variable "enabled" { type = bool }
locals { fcvm_fleet_prod = var.enabled ? 1 : 0 }
resource "terraform_data" "independent" {}
module "region" {
  source = "./region"
""" + "\n".join(wiring) + "\n}\n")
    templates = []
    for name in ("host", "tile", "tile_on_demand", "tile_pool"):
        launch = block(region, f'resource "aws_launch_template" "{name}"')
        dependency = re.search(r"(?m)^  depends_on\s*=.*$", launch)[0]
        templates.append(f'resource "terraform_data" "{name}" {{\n{dependency}\n}}')
    (child / "main.tf").write_text(block(region, 'variable "ami_release_parameter_version"') + "\n" + "\n".join(templates))

    def succeeds(*args):
        rc, output = run(case, *args)
        assert rc == 0, output

    def created():
        return {r["name"] for r in json.loads((case / "terraform.tfstate").read_text())["resources"]
                if r["type"] == "terraform_data" and r.get("instances")}

    state["version"] = 1
    succeeds("init", "-backend=false", "-input=false", "-lockfile=readonly")
    succeeds("plan", "-var=enabled=false", "-out=off.tfplan")
    succeeds("apply", "-input=false", "off.tfplan")
    assert created() == {"independent"}
    print("disabled region: saved apply created the independent resource", flush=True)
    succeeds("plan", "-var=enabled=true", "-target=module.region[0].terraform_data.host", "-out=targeted.tfplan")
    state["version"] = 2
    rc, output = run(case, "apply", "-input=false", "targeted.tfplan")
    assert rc != 0 and "Fleet AMI release changed after planning" in output, output
    assert created() == {"independent"}
    print("stale targeted region plan: apply rejected before host creation", flush=True)
    succeeds("plan", "-var=enabled=true", "-out=enabled.tfplan")
    succeeds("apply", "-input=false", "enabled.tfplan")
    assert created() == {"independent", "host", "tile", "tile_on_demand", "tile_pool"}
    print("fresh enabled region: saved apply created all four template probes", flush=True)


def main():
    locked = block((ROOT / ".terraform.lock.hcl").read_text(), 'provider "registry.terraform.io/hashicorp/aws"')
    version = re.search(r'version\s*=\s*"([^"]+)"', locked)[1]
    constraints = re.search(r'constraints\s*=\s*"([^"]+)"', locked)[1]
    source = (ROOT / "fcvm-fleet-ci.tf").read_text()
    guard = block(source, 'ephemeral "aws_ssm_parameter" "fcvm_coordinator_release_guard"')
    guard, replaced = re.subn(r"aws_ssm_parameter\.fcvm_coordinator_release\.arn", json.dumps(PARAMETER_ARN), guard)
    assert replaced == 1
    tasks = (ROOT / "fcvm-fleet.tf").read_text()
    dependency = "ephemeral.aws_ssm_parameter.fcvm_coordinator_release_guard"
    for kind in ("aws_ecs_task_definition", "aws_ecs_service"):
        resource = block(tasks, f'resource "{kind}" "fcvm_fleet_coordinator"')
        deps = re.search(r"depends_on\s*=\s*(\[[^\]]*\])", resource)
        assert deps and dependency in deps[1], f"{kind} does not depend on the apply-time guard"
    dependency_expression = re.search(r"depends_on\s*=\s*(\[[^\]]*\])", block(
        tasks, 'resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"'))[1]
    packages = ROOT / ".terraform/providers/registry.terraform.io/hashicorp/aws" / version
    binaries = list(packages.glob("*/terraform-provider-aws*"))
    assert len(binaries) == 1, "run the locked backend-free init before this test"
    state = {"version": 1}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.dumps({"Parameter": {"Name": "/mock/image", "ARN": PARAMETER_ARN,
                "Type": "String", "Value": "synthetic-image", "Version": state["version"], "DataType": "text"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-amz-json-1.1")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="fcvm-stale-plan-") as tmp:
            directory = Path(tmp)
            mirror = directory / "providers"
            target = mirror / "registry.terraform.io/hashicorp/aws" / version / binaries[0].parent.name
            target.parent.mkdir(parents=True)
            # Keep the whole signed package, including LICENSE, for lockfile hash verification.
            target.symlink_to(binaries[0].parent.resolve(), target_is_directory=True)
            cli = directory / "terraform.rc"
            cli.write_text('provider_installation {\n filesystem_mirror {\n path = '
                + json.dumps(str(mirror)) + '\n include = ["registry.terraform.io/hashicorp/aws"]\n }\n'
                + ' direct { exclude = ["registry.terraform.io/hashicorp/aws"] }\n}\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "TF_CLI_ARGS", "TF_LOG"))
                   and k not in ("TF_DATA_DIR", "TF_PLUGIN_CACHE_DIR", "TF_WORKSPACE")}
            env.update(TF_CLI_CONFIG_FILE=str(cli), AWS_EC2_METADATA_DISABLED="true", TF_IN_AUTOMATION="1")
            provider = '''terraform {
  required_version = "= 1.10.3"
  required_providers { aws = { source = "hashicorp/aws", version = "PROVIDER_CONSTRAINTS" } }
}
provider "aws" {
  alias = "fcvm_fleet"
  region = "us-west-1"
  access_key = "synthetic-access-key"
  secret_key = "synthetic-secret-key"
  skip_credentials_validation = true
  skip_requesting_account_id = true
  skip_metadata_api_check = true
  skip_region_validation = true
  endpoints { ssm = "http://127.0.0.1:PORT" }
}
data "aws_ssm_parameter" "fcvm_coordinator_release" {
  provider = aws.fcvm_fleet
  name = "/mock/image"
  with_decryption = false
}
'''.replace("PORT", str(server.server_port)).replace("PROVIDER_CONSTRAINTS", constraints)

            def run(cwd, *args):
                result = subprocess.run(["terraform", args[0], "-no-color", *args[1:]], cwd=cwd, env=env,
                                        capture_output=True, text=True, timeout=120)
                return result.returncode, result.stdout + result.stderr

            for guarded in (False, True):
                state["version"] = 1
                case = directory / ("guarded" if guarded else "unguarded")
                case.mkdir()
                (case / ".terraform.lock.hcl").write_text(locked + "\n")
                config = provider + (guard if guarded else "") + '\nresource "terraform_data" "probe" {\n'
                config += ' input = data.aws_ssm_parameter.fcvm_coordinator_release.value\n'
                if guarded:
                    config += " depends_on = " + dependency_expression + "\n"
                (case / "main.tf").write_text(config + "}\n")
                rc, output = run(case, "init", "-backend=false", "-input=false", "-lockfile=readonly")
                assert rc == 0, output
                rc, output = run(case, "plan", "-input=false", "-out=saved.tfplan")
                assert rc == 0, output
                state["version"] = 2
                rc, output = run(case, "apply", "-input=false", "-auto-approve", "saved.tfplan")
                if not guarded:
                    assert rc == 0, "the fixture did not reproduce the unguarded race: " + output
                    print("unguarded saved plan: stale apply reproduced", flush=True)
                    continue
                assert rc != 0 and "release changed after this plan" in output, output
                # A failure must happen before any resource action, not after the damage.
                local_state = case / "terraform.tfstate"
                resources = json.loads(local_state.read_text()).get("resources", []) if local_state.exists() else []
                assert not any(r.get("type") == "terraform_data" and r.get("name") == "probe" for r in resources)
                print("deployed guard: stale apply rejected before resource creation", flush=True)
                rc, output = run(case, "plan", "-input=false", "-out=fresh.tfplan")
                assert rc == 0, output
                rc, output = run(case, "apply", "-input=false", "-auto-approve", "fresh.tfplan")
                assert rc == 0, output
                rc, output = run(case, "plan", "-input=false", "-detailed-exitcode")
                assert rc == 0, output
                print("fresh saved plan: apply succeeds, follow-up plan empty", flush=True)
            test_counted_region(directory, provider, locked, state, run)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
