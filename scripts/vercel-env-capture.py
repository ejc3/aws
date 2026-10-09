#!/usr/bin/env python3
"""Record a Vercel project's environment for one target in AWS Secrets Manager (vercel-env/<site>/<target>).

    scripts/vercel-env-capture.py SITE TARGET          e.g. dolphin-labs production
    scripts/vercel-env-capture.py --list               the sites and targets this knows

Why this exists: Vercel never returns a variable of type `sensitive` once it is saved (not through the API, the CLI or the
dashboard; `vercel env pull` writes the text [SENSITIVE]). A deployment's own code can read them at run time, so for a target
that has sensitive variables this deploys a throwaway route that returns exactly the project's variable names, reads it, and
deletes the deployment. Safeguards, each checked by the tool before it goes on:

  * The deployment is created with --skip-domain (production target) or as a preview, so it never gets a public domain and
    never replaces what a site serves.
  * Vercel Deployment Protection covers it: the tool requests the URL WITHOUT credentials first and stops, deleting the
    deployment, unless Vercel answers 401. The project's own `ssoProtection` setting must also be on.
  * The route answers 404 unless the request carries a one-time token (only its SHA-256 is in the deployed code).
  * The deployment is deleted by its `dpl_` id (never by project name, which would delete every deployment of the project),
    and the tool confirms it is gone.
  * Values travel only through pipes: from the deployment to the box that holds the Vercel login, over ssh to here, into
    `aws secretsmanager put-secret-value`. Nothing is written to a file there (the throwaway project directory lives in
    /dev/shm and is removed), and only variable NAMES are ever printed.

A target with no sensitive variable (and the development target, which cannot have any) is read with `vercel env pull`
instead, with no deployment at all. Vercel's own bookkeeping variables (VERCEL_*, TURBO_*, NX_*) are not recorded.
Run this from an administration box: it needs ssh to nextjs-dev (where the Vercel logins are) and write access to Secrets
Manager. See vercel-env-secrets.tf for the containers.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

# site -> (unix account on nextjs-dev holding the Vercel login, Vercel scope, project name, the project's root directory)
SITES = {
    "colton-games": ("colton", "coltons-projects-7f9a4e8b", "colton-games", "."),
    "dolphin-films": ("ejc3", "dolphin-labs", "dolphin-films", "films/web"),
    "dolphin-labs": ("ejc3", "dolphin-labs", "dolphin-labs", "web"),
    "imagine": ("ejc3", "ejc3-7031s-projects", "imagine", "web"),
    "nest-step": ("colton", "coltons-projects-7f9a4e8b", "v0-immigrant-children-website", "."),
    "remote-claw": ("ejc3", "ejc3-7031s-projects", "remote-claw", "apps/web"),
    "ts-api": ("ejc3", "ejc3-7031s-projects", "ts-api", "."),
}
TARGETS = ("development", "preview", "production")
PROTECTED_MODES = ("all", "prod_deployment_urls_and_all_previews", "all_except_custom_domains")
SKIP_NAME = re.compile(r"^(VERCEL_|TURBO_|NX_)")
PLACEHOLDER = re.compile(r"\[[A-Za-z _]{4,14}\]")
NAME = re.compile(r"[A-Z][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------------------------------------- pure parts
def variable_names(envs, target):
    """Names the project defines for a target: not Vercel's own, not system-type, not branch-specific."""
    out = set()
    for e in envs:
        if target not in (e.get("target") or []) or e.get("gitBranch") or e.get("type") == "system":
            continue
        if SKIP_NAME.match(e["key"]) or not NAME.fullmatch(e["key"]):
            continue
        out.add(e["key"])
    return sorted(out)


def has_sensitive(envs, target):
    return any(target in (e.get("target") or []) and e.get("type") == "sensitive" and not e.get("gitBranch") for e in envs)


def needs_deployment(envs, target):
    """Only a target with a sensitive variable needs the throwaway route; development never has one."""
    return target != "development" and has_sensitive(envs, target)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def render_route(names, digest):
    """The whole of the throwaway route. It carries the names and the HASH of the token, never a value or the token."""
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert names and all(NAME.fullmatch(n) and not SKIP_NAME.match(n) for n in names)
    return """import { createHash } from 'node:crypto';

export const dynamic = 'force-dynamic';

const NAMES = %s;
const DIGEST = %s;

export async function GET(request) {
  const given = request.headers.get('x-dump-token') || '';
  if (createHash('sha256').update(given).digest('hex') !== DIGEST) return new Response('not found', { status: 404 });
  const out = {};
  for (const name of NAMES) if (process.env[name] !== undefined) out[name] = process.env[name];
  return Response.json(out, { headers: { 'cache-control': 'no-store' } });
}
""" % (json.dumps(names), json.dumps(digest))


def render_package():
    return json.dumps({
        "name": "vercel-env-capture", "private": True, "version": "0.0.0",
        "scripts": {"build": "next build"},
        "dependencies": {"next": "^15.5.0", "react": "^19.0.0", "react-dom": "^19.0.0"},
    }, indent=2) + "\n"


def render_vercel_json():
    # Per-deployment settings win over the project's dashboard settings: a site's own install or build command (imagine runs a
    # wasm build, remote-claw is a pnpm workspace) must not run for this throwaway app.
    return json.dumps({"framework": "nextjs", "installCommand": "npm install --no-audit --no-fund", "buildCommand": "npm run build"}) + "\n"


def is_protected(status, location):
    """Deployment Protection answers an anonymous request with a 401 or a redirect to Vercel's own login (vercel.com/sso-api).
    Anything else (a 200, a 404, a redirect elsewhere) is not proof of protection."""
    return status == "401" or (status[:1] == "3" and re.match(r"https://vercel\.com/sso-api[?/]", location or "") is not None)


def parse_dotenv(text):
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value[:1] == '"' and value[-1:] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        values[key] = value
    return values


def validate_record(record, expected_names):
    """What may be stored: every expected name present, a non-empty string, and not a placeholder."""
    missing = [n for n in expected_names if n not in record]
    bad = [n for n, v in record.items() if not isinstance(v, str) or v == "" or PLACEHOLDER.fullmatch(v)]
    extra = [n for n in record if n not in expected_names]
    return missing, bad, extra


# ------------------------------------------------------------------------------------------------ remote part (nextjs-dev)
def vercel(args, cwd=None, check=True, timeout=600):
    r = subprocess.run(["vercel", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise SystemExit("vercel %s failed: %s" % (" ".join(args[:2]), (r.stderr or r.stdout)[-300:]))
    return r


def api(path, scope):
    r = vercel(["api", path, "--scope", scope], timeout=120)
    return json.loads(r.stdout[r.stdout.find("{"):])


def api_call(method, path, scope, body, check=True):
    r = subprocess.run(["vercel", "api", path, "--scope", scope, "-X", method, "--input", "-", "--raw"],
                       input=json.dumps(body), stdin=None, capture_output=True, text=True, timeout=120)
    if check and r.returncode:
        raise SystemExit("vercel api %s %s failed: %s" % (method, path, (r.stderr or r.stdout)[-200:]))
    out = r.stdout
    return json.loads(out[out.find("{"):]) if "{" in out else {}


def log(message):
    print(message, file=sys.stderr, flush=True)


def remote(scope, project, target, root):
    """Runs on nextjs-dev as the owner of the Vercel login. Prints ONE JSON object (name -> value) on stdout."""
    info = api("/v9/projects/%s" % project, scope)
    envs = api("/v9/projects/%s/env" % project, scope).get("envs", [])
    names = variable_names(envs, target)
    if not names:
        raise SystemExit("no variables for %s on %s" % (project, target))
    temp_bypass = None  # a protection-bypass secret this run created because the project had none; always revoked
    previous = None  # (deployment url, aliases) serving production before a staged deploy: --skip-domain keeps the custom
    #                  domains but Vercel still moves the project's default *.vercel.app aliases to the new deployment
    work = tempfile.mkdtemp(dir="/dev/shm", prefix="vercel-env.")
    os.chmod(work, 0o700)
    deployment = None
    try:
        if not needs_deployment(envs, target):
            log("%s %s: %d names, none sensitive: reading with `vercel env pull`" % (project, target, len(names)))
            vercel(["link", "--yes", "--project", project, "--scope", scope], cwd=work)
            vercel(["env", "pull", ".env.pulled", "--environment=" + target, "--yes"], cwd=work)
            values = parse_dotenv(open(os.path.join(work, ".env.pulled")).read())
            json.dump({n: values[n] for n in names if n in values}, sys.stdout)
            return
        # The Deployment Protection setting is checked first; a project without it is never deployed to.
        sso = info.get("ssoProtection")
        # all_except_custom_domains still covers every *.vercel.app URL, which is all the throwaway ever has (a custom domain on
        # it is refused below); "preview" alone would leave a production deployment's own URL open.
        if not sso or sso.get("deploymentType") not in PROTECTED_MODES:
            raise SystemExit("%s: Vercel Authentication does not cover all deployments (%r); not deploying" % (project, sso))
        log("%s %s: %d names, %d sensitive: using a protected throwaway deployment" % (
            project, target, len(names), sum(1 for e in envs if target in e["target"] and e.get("type") == "sensitive")))
        if target == "production":
            prod = (info.get("targets") or {}).get("production") or {}
            previous = (prod.get("url"), list(prod.get("alias") or []))
            if not previous[0]:
                raise SystemExit("cannot tell which deployment serves production now; not deploying")
        token = secrets.token_urlsafe(32)
        app = os.path.join(work, root) if root != "." else work
        os.makedirs(os.path.join(app, "app", "api", "dump"), exist_ok=True)
        open(os.path.join(app, "package.json"), "w").write(render_package())
        open(os.path.join(app, "vercel.json"), "w").write(render_vercel_json())
        open(os.path.join(app, "app", "api", "dump", "route.js"), "w").write(render_route(names, token_hash(token)))
        vercel(["link", "--yes", "--project", project, "--scope", scope], cwd=work)
        args = ["deploy", "--yes", "--scope", scope] + (["--prod", "--skip-domain"] if target == "production" else [])
        out = vercel(args, cwd=work).stdout
        urls = re.findall(r"https://[A-Za-z0-9.-]+\.vercel\.app", out)
        if not urls:
            raise SystemExit("no deployment url in the deploy output")
        url = urls[-1]
        host = url.split("//", 1)[1]
        dep = api("/v13/deployments/%s" % host, scope)
        deployment = dep["id"]
        assert deployment.startswith("dpl_"), deployment
        log("deployed %s (%s, target %s)" % (host, deployment, dep.get("target")))
        custom = [a for a in dep.get("alias") or [] if not a.endswith(".vercel.app")]
        if target == "production" and custom:
            raise SystemExit("the staged production deployment has custom domains %r; refusing to go on" % custom)
        # No credentials: Vercel must refuse. This is the whole point of the protection.
        anon = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", "--max-time", "30", url + "/api/dump"],
                              capture_output=True, text=True).stdout.strip().split(" ", 1) + [""]
        if not is_protected(anon[0], anon[1]):
            raise SystemExit("an unauthenticated request got %s: the deployment is NOT protected; deleting it" % anon[0])
        log("unauthenticated request: %s (protected)" % anon[0])
        if not info.get("protectionBypass"):
            # `vercel curl` gets through protection only with an automation-bypass secret, and this project has none: make one
            # for this run, mark it temporary, and revoke it in the finally below. Existing secrets are never touched.
            made = api_call("PATCH", "/v1/projects/%s/protection-bypass" % info["id"], scope, {"generate": {"note": "temporary: vercel-env-capture, revoked at the end of the run"}})
            keys = list((made.get("protectionBypass") or {}).keys())
            if len(keys) != 1:
                raise SystemExit("could not create a temporary bypass secret")
            temp_bypass = keys[0]
            log("created a temporary protection-bypass secret (revoked at the end)")
        for _ in range(6):
            if temp_bypass:
                # secret and token go to curl on stdin, never argv
                r = subprocess.run(["curl", "-s", "--max-time", "60", "-H", "@-", url + "/api/dump"], capture_output=True, text=True, timeout=180,
                                   input="x-vercel-protection-bypass: %s\nx-dump-token: %s\n" % (temp_bypass, token))
            else:
                r = vercel(["curl", "/api/dump", "--deployment", url, "--scope", scope, "--yes", "--", "-s", "--max-time", "60",
                            "-H", "x-dump-token: " + token], check=False, timeout=180)
            body = r.stdout[r.stdout.find("{"):] if "{" in r.stdout else ""
            try:
                values = json.loads(body)
                break
            except ValueError:
                last = r
                time.sleep(5)
        else:
            # A reply that is not JSON cannot hold the variables, so a short look at it is safe; a reply that starts like JSON is never shown.
            seen = (last.stdout or last.stderr or "").strip()
            hint = "" if seen[:1] == "{" else " (exit %s, %d bytes, begins %r)" % (last.returncode, len(seen), seen[:60])
            raise SystemExit("could not read the route's answer" + hint)
        json.dump(values, sys.stdout)
    finally:
        if temp_bypass:
            api_call("PATCH", "/v1/projects/%s/protection-bypass" % info["id"], scope, {"revoke": {"secret": temp_bypass, "regenerate": False}}, check=False)
            left = (api("/v9/projects/%s" % project, scope).get("protectionBypass") or {})
            log("temporary protection-bypass secret %s" % ("STILL EXISTS: revoke it by hand in the project's Deployment Protection settings" if temp_bypass in left else "revoked"))
        if deployment:
            vercel(["remove", deployment, "--yes", "--scope", scope], check=False)
            gone = subprocess.run(["vercel", "api", "/v13/deployments/%s" % deployment, "--scope", scope], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True).stdout
            log("deployment %s %s" % (deployment, "STILL EXISTS: delete it by hand" if '"id"' in gone and deployment in gone else "deleted"))
            if previous:
                # Deleting the deployment leaves the default aliases dangling: point each back at what served production.
                for alias in previous[1]:
                    if alias.endswith(".vercel.app"):
                        r = vercel(["alias", "set", previous[0], alias, "--scope", scope], check=False)
                        log("alias %s -> %s: %s" % (alias, previous[0], "restored" if r.returncode == 0 else "FAILED, set it by hand"))
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------- local part
def main(argv):
    if argv[:1] == ["--remote"]:
        remote(*argv[1:5])
        return 0
    if argv[:1] == ["--list"]:
        for site, (account, scope, project, root) in sorted(SITES.items()):
            print("%-14s %s/%s (root %s, login %s)" % (site, scope, project, root, account))
        return 0
    if len(argv) != 2 or argv[0] not in SITES or argv[1] not in TARGETS:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    site, target = argv
    account, scope, project, root = SITES[site]
    host = subprocess.run(["aws", "ec2", "describe-instances", "--region", "us-west-1", "--filters", "Name=tag:Name,Values=nextjs-dev",
                           "Name=instance-state-name,Values=running", "--query", "Reservations[0].Instances[0].PublicIpAddress", "--output", "text"],
                          capture_output=True, text=True).stdout.strip()
    if not host or host == "None":
        print("nextjs-dev is not running", file=sys.stderr)
        return 1
    ssh = ["ssh", "-i", os.path.expanduser("~/.ssh/fcvm-ec2"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "ubuntu@" + host]
    me = os.path.abspath(__file__)
    remote_copy = "/tmp/vercel-env-capture.%d.py" % os.getpid()
    subprocess.run(["scp", "-q", "-i", os.path.expanduser("~/.ssh/fcvm-ec2"), "-o", "BatchMode=yes", me, "ubuntu@%s:%s" % (host, remote_copy)], check=True)
    try:
        r = subprocess.run(ssh + ["chmod 644 %s; sudo -n -u %s python3 %s --remote %s %s %s %s" % (remote_copy, account, remote_copy, scope, project, target, root)],
                           capture_output=False, stdout=subprocess.PIPE, text=True, timeout=1500)
    finally:
        subprocess.run(ssh + ["rm -f " + remote_copy])
    if r.returncode:
        print("capture failed (nothing was stored)", file=sys.stderr)
        return 1
    record = json.loads(r.stdout)
    missing, bad, extra = validate_record(record, sorted(record))
    if bad or not record:
        print("refusing to store: empty or placeholder values for %s" % bad, file=sys.stderr)
        return 1
    put = subprocess.run(["aws", "secretsmanager", "put-secret-value", "--region", "us-west-1", "--secret-id", "vercel-env/%s/%s" % (site, target),
                          "--secret-string", "file:///dev/stdin", "--query", "Name", "--output", "text"],
                         input=json.dumps(record, separators=(",", ":"), sort_keys=True), capture_output=True, text=True)
    if put.returncode:
        print("could not store: %s" % put.stderr[:200], file=sys.stderr)
        return 1
    print("stored %s: %d names: %s" % (put.stdout.strip(), len(record), ", ".join(sorted(record))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
