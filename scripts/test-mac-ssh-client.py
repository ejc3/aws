#!/usr/bin/env python3
"""Exercise the real `ssh mac` / `ssh colton-mac` installer and OpenSSH's config parser without a live connection,
the Cloudflare and secrets wiring of both Macs, and the Mac-side connector setup script against stubbed ssh and aws."""

from pathlib import Path
import grp
import os
import plistlib
import pwd
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "mac-ssh-client.tf").read_text()
TUNNEL = (ROOT / "macbook-ssh.tf").read_text()
COLTON = (ROOT / "colton-mac-ssh.tf").read_text()
CONNECTOR = ROOT / "scripts" / "mac-ssh-connector-setup.sh"
REMOTE_LOGIN_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAID1DlX7dlpsRWSQuPOHoiDxtiyZd7/W4I1toKoLi7zfL"
COLTON_HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHwu+CCNChdYQcpAVyCXMIkwoSP75e159hJk1a/tpOkO"


def mac_map():
    """local.mac_ssh_macs, read from the HCL."""
    body = re.search(r"\n  mac_ssh_macs = \{\n(.*?)\n  \}\n", SOURCE, re.S).group(1)
    macs = {}
    for alias, fields in re.findall(r"^    ([\w-]+) = \{\n(.*?)\n    \}", body, re.S | re.M):
        macs[alias] = {k: (v.strip('"') if v.startswith('"') else v == "true")
                       for k, v in re.findall(r"^\s+(\w+)\s*=\s*(\"[^\"]*\"|true|false)\s*$", fields, re.M)}
    return macs


def table():
    """mac_ssh_client_table, rendered the way the join in mac-ssh-client.tf renders it."""
    return "".join(f"{a} {m['hostname']} {m['user']} {m['host_key_alias']} {'yes' if m['make_key'] else 'no'} {m['host_key']}\n"
                   for a, m in mac_map().items())


SETUP = re.search(r"mac_ssh_client_setup = <<-MACSETUP\n(.*?)\nMACSETUP", SOURCE, re.S).group(1)
SETUP = SETUP.replace("${local.mac_ssh_client_table}", table()).replace("$${", "${")


class MacSshClient(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mac-ssh-client-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir(mode=0o700)
        self.config = self.ssh / "config"
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.group = grp.getgrgid(os.getgid()).gr_name
        # ssh expands ~ from the password database, not $HOME, so point the config's ~ at the temp home.
        self.setup = SETUP.replace("~/.ssh", str(self.ssh))
        self.chown = 'chown "$MAC_USER:$MAC_USER"', f'chown "{self.user}:{self.group}"'
        self.setup = self.setup.replace(*self.chown).replace(
            '-o "$MAC_USER" -g "$MAC_USER"', f'-o "{self.user}" -g "{self.group}"')
        # The test runs as the account already; root uses runuser to become it.
        self.setup = self.setup.replace('runuser -u "$MAC_USER" -- ', "")

    def install(self, secret="macbook-ssh-access", alias=None, check=True):
        env = dict(os.environ, MAC_USER=self.user, MAC_HOME=str(self.home), MAC_SECRET=secret)
        env.pop("MAC_ALIAS", None)
        script = "MAC_USER=$MAC_USER\nMAC_SECRET=$MAC_SECRET\n"
        if alias:
            script += f"MAC_ALIAS={alias}\n"
        result = subprocess.run(["bash", "-eu", "-c", script + self.setup], capture_output=True, text=True,
                                timeout=20, env=env)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def effective(self, alias):
        result = subprocess.run(["ssh", "-G", "-F", str(self.config), alias],
                                check=True, capture_output=True, text=True, timeout=5)
        return dict(line.split(" ", 1) for line in result.stdout.splitlines())

    def files(self):
        return {p.name: p.read_bytes() for p in self.ssh.iterdir()}

    def check_mac(self, secret="macbook-ssh-access"):
        mac = self.effective("mac")
        self.assertEqual((mac["hostname"], mac["port"]), ("macbook-ssh.cc-games.dev", "22"))
        self.assertIn("mac-access-proxy", mac["proxycommand"])
        self.assertEqual(mac["user"], "ejcampbell")
        self.assertEqual(mac["identityfile"], str(self.ssh / "to-mac"))
        self.assertEqual(mac["identitiesonly"], "yes")
        self.assertEqual(mac["hostkeyalias"], "ejs-macbook-pro-via-fcvm")
        self.assertEqual(mac["stricthostkeychecking"], "true")
        self.assertEqual(mac["userknownhostsfile"], str(self.ssh / "known_hosts.mac"))
        proxy = self.ssh / "mac-access-proxy"
        self.assertEqual(proxy.stat().st_mode & 0o777, 0o700)
        text = proxy.read_text()
        self.assertIn(f"--secret-id {secret} ", text, "each account reads its own service token")
        self.assertNotIn("@MAC_SECRET@", text)
        run = [l for l in text.splitlines() if "cloudflared access ssh" in l]
        self.assertEqual(len(run), 1)
        for flag in ("--secret", "--id", "--service-token"):
            self.assertNotIn(flag, run[0], "the token reaches cloudflared through its environment, never argv")
        # The old names are gone: ssh treats them as plain host names again.
        for old in ("macbook", "macbook-desktop"):
            config = self.effective(old)
            self.assertEqual(config["hostname"], old)
            self.assertNotIn("proxycommand", config)
            self.assertNotIn("proxyjump", config)
        known = (self.ssh / "known_hosts.mac").read_text().splitlines()
        self.assertEqual(known, [f"ejs-macbook-pro-via-fcvm ssh-ed25519 {REMOTE_LOGIN_KEY}"],
                         "only Remote Login's key is pinned")
        self.assertFalse((self.ssh / "to-mac").exists(), "EJ's key is never made by anything")

    def check_colton(self, secret="colton-mac-ssh-access-fcvm"):
        mac = self.effective("colton-mac")
        self.assertEqual((mac["hostname"], mac["port"], mac["user"]), ("colton-mac-ssh.cc-games.dev", "22", "colton"))
        self.assertIn(f"{self.ssh}/colton-mac-access-proxy", mac["proxycommand"])
        self.assertEqual(mac["identityfile"], str(self.ssh / "to-colton-mac"))
        self.assertEqual((mac["identitiesonly"], mac["hostkeyalias"], mac["stricthostkeychecking"]), ("yes", "colton-mac", "true"))
        self.assertEqual(mac["userknownhostsfile"], str(self.ssh / "known_hosts.colton-mac"))
        self.assertEqual((self.ssh / "known_hosts.colton-mac").read_text(), f"colton-mac {COLTON_HOST_KEY}\n")
        self.assertIn(f"--secret-id {secret} ", (self.ssh / "colton-mac-access-proxy").read_text())
        for name in ("config", "config.d-colton-mac", "known_hosts.colton-mac"):
            self.assertEqual((self.ssh / name).stat().st_mode & 0o777, 0o600, name)
        self.assertEqual((self.ssh / "colton-mac-access-proxy").stat().st_mode & 0o777, 0o700)

    def test_fresh_install_writes_one_alias(self):
        self.install()
        self.check_mac()
        for name in ("config", "config.d-mac", "known_hosts.mac"):
            self.assertEqual((self.ssh / name).stat().st_mode & 0o777, 0o600)
        managed = (self.ssh / "config.d-mac").read_text()
        self.assertEqual(re.findall(r"(?im)^\s*host\s+(.*)$", managed), ["mac"])
        # The installer's ~/.ssh is the temp home here (see setUp).
        self.assertEqual(self.config.read_text().splitlines()[0], f"Include {self.ssh}/config.d-mac")

    def test_an_unset_alias_is_mac_as_the_containers_snippet_relies_on(self):
        self.install(alias=None)
        self.check_mac()
        self.assertFalse((self.ssh / "config.d-colton-mac").exists())

    def test_each_account_gets_its_own_secret(self):
        self.install(secret="macbook-ssh-access-nextjs-ejc3")
        self.check_mac(secret="macbook-ssh-access-nextjs-ejc3")

    def test_migrates_fcvm_arms_old_files_and_stanzas(self):
        other = self.ssh / "config.d-devhop"
        other.write_text("Host fcvm-arm\n    HostName 184.72.40.255\n    User ubuntu\n")
        for old in ("config.d-mac-tunnels", "known_hosts.mac-tunnels", "macbook-access-proxy"):
            (self.ssh / old).write_text("old\n")
        original = (f"Include {self.ssh}/config.d-mac-tunnels\n\n"
                    f"Include {other}\n"
                    "Host *\n    User wrong-user\n"
                    "Host mac\n    Port 1111\n"
                    "Host macbook # old manual configuration\n    Port 9999\n"
                    "# GitHub runners\n"
                    "Host runner-host\n    HostName 192.0.2.1\n    Port 2200\n"
                    "\n"
                    "# The MacBook's desktop session, written by hand.\n"
                    "# Second line of its comment.\n"
                    "Host macbook-desktop\n    HostName 127.0.0.1\n    Port 22222\n    ProxyJump macbook\n"
                    "\n"
                    "# Keep me: the comment of the host after a retired one.\n"
                    "Host after-host\n    Port 2201\n")
        self.config.write_text(original)
        self.install()
        self.check_mac()
        for old in ("config.d-mac-tunnels", "known_hosts.mac-tunnels", "macbook-access-proxy"):
            self.assertFalse((self.ssh / old).exists(), old)
        self.assertEqual(self.effective("fcvm-arm")["hostname"], "184.72.40.255")
        self.assertEqual(self.effective("fcvm-arm")["user"], "ubuntu")
        self.assertEqual(self.effective("runner-host")["port"], "2200")
        self.assertEqual(self.effective("after-host")["port"], "2201")
        text = self.config.read_text()
        for gone in ("config.d-mac-tunnels", "Port 9999", "Port 1111", "Port 22222", "desktop session", "Second line"):
            self.assertNotIn(gone, text)
        for kept in ("# GitHub runners\nHost runner-host",
                     "# Keep me: the comment of the host after a retired one.\nHost after-host"):
            self.assertIn(kept, text)
        backups = [p for p in self.ssh.iterdir() if p.name.startswith("config.before-mac.")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        before = self.files()
        self.install()
        self.assertEqual(self.files(), before, "a rerun changes nothing")

    def test_migrates_nextjs_ejc3s_hand_installed_aliases(self):
        (self.ssh / "config.d-macbook").write_text("Host macbook\n    Port 2222\n")
        self.config.write_text("Include config.d-mac\n\nHost mac\n    HostName 127.0.0.1\n    Port 2222\n")
        self.install(secret="macbook-ssh-access-nextjs-ejc3")
        self.check_mac(secret="macbook-ssh-access-nextjs-ejc3")
        self.assertFalse((self.ssh / "config.d-macbook").exists())
        self.assertNotIn("2222", self.config.read_text())
        self.assertNotIn("Include config.d-mac\n", self.config.read_text())

    def test_an_include_below_a_host_is_moved_to_the_top(self):
        self.config.write_text(f"Host *\n    User wrong-user\nInclude {self.ssh}/config.d-mac\n")
        self.install()
        self.assertEqual(self.config.read_text(), f"Include {self.ssh}/config.d-mac\n\nHost *\n    User wrong-user\n")
        self.check_mac()

    def test_colton_mac_makes_the_accounts_own_key_and_prints_only_its_public_half(self):
        result = self.install(secret="colton-mac-ssh-access-fcvm", alias="colton-mac")
        self.check_colton()
        key = self.ssh / "to-colton-mac"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        public = (self.ssh / "to-colton-mac.pub").read_text().split()
        self.assertEqual(public[-3:], [self.user, "->", "colton-mac"])
        self.assertIn(" ".join(public[:2]), result.stdout)
        private = key.read_text()
        self.assertNotIn("PRIVATE KEY", result.stdout + result.stderr)
        self.assertNotIn(private.splitlines()[1], result.stdout + result.stderr)
        # An existing key is kept and not announced again.
        again = self.install(secret="colton-mac-ssh-access-fcvm", alias="colton-mac")
        self.assertEqual(key.read_text(), private)
        self.assertNotIn("made", again.stdout)

    def test_both_macs_on_one_account_settle_without_rewriting_each_other(self):
        (self.ssh / "config.d-devhop").write_text("Host fcvm-x86\n    HostName 192.0.2.10\n")
        self.config.write_text(f"Include {self.ssh}/config.d-devhop\n\n# runners\nHost runner-*\n    User ubuntu\n")
        self.install()
        self.install(secret="colton-mac-ssh-access-fcvm", alias="colton-mac")
        self.install()
        settled = self.files()
        self.install(secret="colton-mac-ssh-access-fcvm", alias="colton-mac")
        self.install()
        self.assertEqual(self.files(), settled, "neither installer rewrites the other's work")
        self.check_mac()
        self.check_colton()
        self.assertEqual(self.effective("fcvm-x86")["hostname"], "192.0.2.10")
        self.assertEqual(self.effective("runner-x")["user"], "ubuntu")
        self.assertEqual(len([p for p in self.ssh.iterdir() if p.name.startswith("config.before-mac.")]), 2)

    def test_colton_mac_takes_over_fcvm_arms_hand_installed_alias(self):
        # As set up by hand on 2026-10-19 for the temporary reverse tunnel.
        (self.ssh / "config.d-colton-mac").write_text("Host colton-mac\n    HostName 127.0.0.1\n    Port 2224\n")
        (self.ssh / "known_hosts.colton-mac").write_text(f"colton-mac {COLTON_HOST_KEY}\n")
        (self.ssh / "to-colton-mac").write_text("existing private key\n")
        original = (f"Include {self.ssh}/config.d-colton-mac\nInclude {self.ssh}/config.d-mac\n\n"
                    "Host runner-*\n    User ubuntu\n")
        self.config.write_text(original)
        self.install()
        self.install(secret="colton-mac-ssh-access-fcvm", alias="colton-mac")
        self.assertEqual(self.config.read_text(), original, "both Includes were already in place")
        self.check_colton()
        self.check_mac()
        self.assertEqual((self.ssh / "to-colton-mac").read_text(), "existing private key\n")

    def test_an_unknown_alias_fails_and_writes_nothing(self):
        result = self.install(alias="nobody-mac", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no Mac named 'nobody-mac'", result.stderr)
        self.assertEqual(self.files(), {})


class Wiring(unittest.TestCase):
    def test_the_map_pins_both_macs(self):
        macs = mac_map()
        self.assertEqual(sorted(macs), ["colton-mac", "mac"])
        self.assertEqual(macs["mac"], {"hostname": "macbook-ssh.cc-games.dev", "user": "ejcampbell",
                                       "host_key_alias": "ejs-macbook-pro-via-fcvm",
                                       "host_key": f"ssh-ed25519 {REMOTE_LOGIN_KEY}", "make_key": False})
        self.assertEqual(macs["colton-mac"], {"hostname": "colton-mac-ssh.cc-games.dev", "user": "colton",
                                              "host_key_alias": "colton-mac", "host_key": COLTON_HOST_KEY, "make_key": True})
        self.assertIn('macbook_ssh_hostname = "macbook-ssh.cc-games.dev"', TUNNEL)
        self.assertIn('colton_mac_ssh_hostname = "colton-mac-ssh.cc-games.dev"', COLTON)
        self.assertIn('mac_ssh_client_table = join("", [for alias, m in local.mac_ssh_macs : "${alias} ${m.hostname} '
                      '${m.user} ${m.host_key_alias} ${m.make_key ? "yes" : "no"} ${m.host_key}\\n"])', SOURCE)

    def test_fcvm_arm_gets_both_macs_as_ubuntu_with_its_tokens_and_x86_does_not(self):
        source = (ROOT / "dev-user-data.tf").read_text()
        arm, x86 = source.split("  arm_user_data = <<-SCRIPT", 1)[1].split("  x86_user_data = <<-SCRIPT", 1)
        self.assertIn("\n${local.fcvm_mac_ssh_clients}\n", arm)
        self.assertNotIn("mac_ssh_client", x86)
        snippet = re.search(r"fcvm_mac_ssh_clients = <<-FCVMMACS\n(.*?)\nFCVMMACS", SOURCE, re.S).group(1)
        self.assertEqual(snippet, "MAC_USER=ubuntu\n${local.mac_cloudflared_install}\n"
                                  "MAC_ALIAS=mac\nMAC_SECRET=macbook-ssh-access\n${local.mac_ssh_client_setup}\n"
                                  "MAC_ALIAS=colton-mac\nMAC_SECRET=colton-mac-ssh-access-fcvm\n${local.mac_ssh_client_setup}")

    def test_a_daily_association_converges_the_running_fcvm_arm(self):
        assoc = SOURCE[SOURCE.index('resource "aws_ssm_association" "fcvm_mac_ssh_clients"'):]
        self.assertIn('schedule_expression = "rate(1 day)"', assoc)
        self.assertIn("values = [aws_instance.firecracker_dev[0].id]", assoc)
        commands = re.search(r'commands\s+= "(.*)"\n', assoc).group(1)
        self.assertIn("${local.fcvm_mac_ssh_clients}FCVMMACSSH", commands)
        self.assertIn('bash \\"$f\\" </dev/null', commands, "run by bash from a file, not by sh or from stdin")
        # Rendered with a stand-in snippet, the command runs under sh and reports the snippet's status.
        script = commands.encode().decode("unicode_escape").replace("${local.fcvm_mac_ssh_clients}", "echo ran; exit 3\n")
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout), (3, "ran\n"))

    def test_nextjs_dev_installs_mac_for_ejc3_only_and_colton_mac_for_colton_and_ejc3(self):
        source = (ROOT / "nextjs-user-data.tf").read_text()
        self.assertEqual(source.count("${local.mac_ssh_client_setup}"), 2)
        self.assertIn("if id ejc3 >/dev/null 2>&1; then\nMAC_USER=ejc3\nMAC_ALIAS=mac\nMAC_SECRET=macbook-ssh-access-nextjs-ejc3\n"
                      "${local.mac_cloudflared_install}\n${local.mac_ssh_client_setup}\nfi\n", source)
        self.assertIn('for MAC_USER in colton ejc3; do\nif id "$MAC_USER" >/dev/null 2>&1; then\nMAC_ALIAS=colton-mac\n'
                      'MAC_SECRET="colton-mac-ssh-access-nextjs-$MAC_USER"\nMAC_HOME="/home/$MAC_USER"\n'
                      "${local.mac_cloudflared_install}\n${local.mac_ssh_client_setup}\nfi\ndone\n", source)

    def test_the_containers_snippet_still_sets_only_user_and_secret(self):
        # nextjs-ct-user-data.tf is not changed here: its snippet relies on MAC_ALIAS defaulting to mac.
        body = (ROOT / "nextjs-ct-user-data.tf").read_text().split("nextjs_ct_macbook_ssh = <<-MACBOOK\n", 1)[1].split("\nMACBOOK\n", 1)[0]
        self.assertNotIn("MAC_ALIAS", body)
        self.assertIn('MAC_ALIAS="$${MAC_ALIAS:-mac}"', SOURCE)

    def test_the_pinned_cloudflared_is_checked_before_and_after_download(self):
        cfd = re.search(r"mac_cloudflared_install = <<-MACCFD\n(.*?)\nMACCFD", SOURCE, re.S).group(1)
        self.assertIn("${local.cloudflared_version}/cloudflared-linux-arm64", cfd)
        self.assertEqual(cfd.count("${local.cloudflared_sha256_linux_arm}"), 2)
        self.assertIn('"/home/$MAC_USER/.local/bin/cloudflared"', cfd)
        self.assertIn('PATH="$HOME/.local/bin:$PATH" exec cloudflared', SETUP)


class Cloudflare(unittest.TestCase):
    def test_the_route_ends_at_remote_login(self):
        services = re.findall(r'service\s*=\s*"(ssh://[^"]+)"', TUNNEL)
        self.assertEqual(services, ["ssh://127.0.0.1:22"])

    def test_access_admits_exactly_the_two_service_tokens_and_no_person(self):
        policy = re.search(r'resource "cloudflare_zero_trust_access_policy" "macbook_ssh_fcvm" \{(.*?)\n\}', TUNNEL, re.S).group(1)
        self.assertIn('decision         = "non_identity"', policy)
        tokens = re.findall(r"service_token = \{ token_id = cloudflare_zero_trust_access_service_token\.(\w+)\.id \}", policy)
        self.assertEqual(tokens, ["macbook_ssh_fcvm", "macbook_ssh_nextjs_ejc3"])
        self.assertNotRegex(policy, r"email|group|everyone|ip\s*=")

    def test_the_nextjs_token_is_readable_by_administration_and_nextjs_dev_only(self):
        readers = re.search(r"macbook_ssh_nextjs_readers = \[(.*?)\n  \]", TUNNEL, re.S).group(1)
        self.assertIn("aws_iam_role.nextjs_dev.arn", readers)
        self.assertNotIn("dev_server", readers)
        grant = re.search(r'resource "aws_iam_role_policy" "nextjs_dev_macbook_ssh" \{(.*?)\n\}', TUNNEL, re.S).group(1)
        self.assertIn("aws_secretsmanager_secret.macbook_ssh_access_nextjs_ejc3.arn", grant)
        self.assertNotIn("macbook_ssh_access.arn", grant)
        self.assertNotIn("macbook_ssh_tunnel", grant)


class ColtonCloudflare(unittest.TestCase):
    def resource(self, kind, name):
        return re.search(rf'resource "{kind}" "{name}" \{{(.*?)\n\}}', COLTON, re.S).group(1)

    def clients(self):
        body = re.search(r"colton_mac_ssh_clients = \{\n(.*?)\n  \}", COLTON, re.S).group(1)
        return {k: dict(re.findall(r'(\w+) = ("[^"]*"|[\w.]+)', v))
                for k, v in re.findall(r"^    ([\w-]+)\s+= \{ (.*) \}$", body, re.M)}

    def test_three_clients_each_with_its_own_token_and_secret(self):
        clients = self.clients()
        self.assertEqual(sorted(clients), ["fcvm", "nextjs-colton", "nextjs-ejc3"])
        self.assertEqual({k: (c["secret"], c["reader"]) for k, c in clients.items()}, {
            "fcvm": ('"colton-mac-ssh-access-fcvm"', "aws_iam_role.dev_server.arn"),
            "nextjs-colton": ('"colton-mac-ssh-access-nextjs-colton"', "aws_iam_role.nextjs_dev.arn"),
            "nextjs-ejc3": ('"colton-mac-ssh-access-nextjs-ejc3"', "aws_iam_role.nextjs_dev.arn"),
        })
        self.assertIn("for_each   = local.colton_mac_ssh_clients", self.resource("cloudflare_zero_trust_access_service_token", "colton_mac_ssh"))

    def test_the_route_ends_at_remote_login_and_the_connector_checks_access(self):
        config = self.resource("cloudflare_zero_trust_tunnel_cloudflared_config", "colton_mac_ssh")
        self.assertEqual(re.findall(r'service\s*=\s*"([^"]+)"', config), ["ssh://127.0.0.1:22", "http_status:404"])
        self.assertIn("required  = true", config)
        self.assertIn("aud_tag   = [cloudflare_zero_trust_access_application.colton_mac_ssh.aud]", config)

    def test_access_admits_the_clients_tokens_and_the_owner(self):
        policy = self.resource("cloudflare_zero_trust_access_policy", "colton_mac_ssh_clients")
        self.assertIn('decision         = "non_identity"', policy)
        self.assertIn("for k in sort(keys(local.colton_mac_ssh_clients)) : { service_token = { token_id = "
                      "cloudflare_zero_trust_access_service_token.colton_mac_ssh[k].id } }", policy)
        self.assertNotRegex(policy, r"email|group|everyone|ip\s*=")
        app = self.resource("cloudflare_zero_trust_access_application", "colton_mac_ssh")
        self.assertEqual(re.findall(r"id\s+= cloudflare_zero_trust_access_policy\.(\w+)\.id\n\s+precedence = (\d)", app),
                         [("browser_manager_owner", "1"), ("colton_mac_ssh_clients", "2")])
        self.assertIn("domain           = local.colton_mac_ssh_hostname", app)

    def test_the_connector_token_is_administration_only(self):
        admins = re.search(r"colton_mac_ssh_admins = \[(.*?)\n  \]", COLTON, re.S).group(1)
        self.assertNotIn("dev_server", admins)
        self.assertNotIn("nextjs_dev", admins)
        policy = self.resource("aws_secretsmanager_secret_policy", "colton_mac_ssh_tunnel")
        self.assertIn('"aws:PrincipalArn" = local.colton_mac_ssh_admins', policy)
        for grant in ("dev_server_colton_mac_ssh", "nextjs_dev_colton_mac_ssh"):
            self.assertNotIn("colton_mac_ssh_tunnel", self.resource("aws_iam_role_policy", grant))

    def test_each_access_secret_admits_administration_and_its_one_client(self):
        policy = self.resource("aws_secretsmanager_secret_policy", "colton_mac_ssh_access")
        self.assertIn('"aws:PrincipalArn" = concat(local.colton_mac_ssh_admins, [each.value.reader])', policy)
        fcvm = self.resource("aws_iam_role_policy", "dev_server_colton_mac_ssh")
        self.assertIn('Resource = aws_secretsmanager_secret.colton_mac_ssh_access["fcvm"].arn', fcvm)
        self.assertIn('"ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn', fcvm)
        nextjs = self.resource("aws_iam_role_policy", "nextjs_dev_colton_mac_ssh")
        self.assertEqual(re.findall(r'colton_mac_ssh_access\["([\w-]+)"\]', nextjs), ["nextjs-colton", "nextjs-ejc3"])

    def test_the_dns_record_overrides_the_wildcard_for_the_tunnel(self):
        record = self.resource("cloudflare_dns_record", "colton_mac_ssh")
        self.assertIn("name    = local.colton_mac_ssh_hostname", record)
        self.assertIn('content = "${cloudflare_zero_trust_tunnel_cloudflared.colton_mac_ssh.id}.cfargotunnel.com"', record)


class ConnectorScript(unittest.TestCase):
    """scripts/mac-ssh-connector-setup.sh end to end, with ssh, aws and the macOS tools stubbed."""

    TOKEN = "eyJhIjoiZXhhbXBsZS10b2tlbiJ9"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mac-connector-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"  # the Mac's and fcvm-arm's home alike: ssh runs commands locally here
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".local/bin").mkdir(parents=True)
        self.stubs = root / "stubs"
        self.stubs.mkdir()
        self.argv_log = root / "argv.log"
        self.keys = {}
        for name in ("fcvm", "colton", "ejc3"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(root / name)], check=True)
            self.keys[name] = " ".join((root / f"{name}.pub").read_text().split()[:2])
        (self.home / ".ssh/to-colton-mac.pub").write_text(self.keys["fcvm"] + " fcvm-arm ubuntu -> colton-mac\n")
        self.nextjs = root / "nextjs"  # each nextjs-dev account's ~/.ssh/to-colton-mac.pub
        self.nextjs.mkdir()
        (self.nextjs / "colton.pub").write_text(self.keys["colton"] + " nextjs-dev colton -> colton-mac\n")
        (self.nextjs / "ejc3.pub").write_text(self.keys["ejc3"] + " x\n")
        self.offered = root / "offered"
        self.offered.write_text("publickey\n")
        self.dropin = root / "050-keys-only.conf"
        self.nosudo = root / "nosudo"
        bin_sum = re.search(r"^CFD_SHA256_DARWIN_ARM64_BIN=(\S+)$", CONNECTOR.read_text(), re.M).group(1)
        self.stub("ssh", f"""#!/usr/bin/env python3
import os, subprocess, sys
with open({str(self.argv_log)!r}, "a") as log:
    log.write(repr(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "PreferredAuthentications=password,keyboard-interactive" in args:
    # The Remote Login check, run on the Mac: what sshd offers a client with no key.
    sys.stderr.write("debug1: Authentications that can continue: " + open({str(self.offered)!r}).read().strip() + "\\n")
    sys.exit(255)
i = 0
while args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-i", "-p") else 1
data = sys.stdin.buffer.read()
data = data.replace(b"export PATH=/usr/bin:/bin:/usr/sbin:/sbin", b"export PATH={self.stubs}:/usr/bin:/bin")
sys.exit(subprocess.run(["bash", "-c", " ".join(args[i + 1:])], input=data).returncode)
""")
        self.stub("aws", f"""#!/bin/sh
echo "$*" >> {self.argv_log}
case "$1 $2" in
  "ec2 describe-instances") echo i-0123456789abcdef0 ;;
  "ssm send-command") echo "cmd-$(echo "$*" | sed 's|.*cat /home/||; s|/.*||')" ;;
  "ssm wait") ;;
  "ssm get-command-invocation") u=$(echo "$*" | sed 's|.*--command-id cmd-||; s| .*||'); cat "{self.nextjs}/$u.pub" ;;
  "secretsmanager get-secret-value") echo "{self.TOKEN}" ;;
  *) echo "unexpected aws $*" >&2; exit 1 ;;
esac
""")
        self.stub("uname", 'echo "Darwin arm64"')
        self.stub("shasum", f'echo "{bin_sum}  $3"')
        self.stub("plutil", "exit 0")
        self.stub("sudo", f'[ "$1 $2 $3" = "-n /usr/bin/tee /etc/ssh/sshd_config.d/050-keys-only.conf" ] && [ ! -e {self.nosudo} ] '
                          f'|| exit 1; cat > {self.dropin} && echo publickey > {self.offered}')
        self.stub("sleep", "exit 0")
        self.stub("launchctl", f'echo "launchctl $*" >> {root}/launchctl.log')
        cfd = self.home / ".local/bin/cloudflared"
        cfd.write_text("#!/bin/sh\necho 'cloudflared version 2026.9.3'\n")
        cfd.chmod(0o755)
        self.launchctl = root / "launchctl.log"

    def stub(self, name, body):
        path = self.stubs / name
        path.write_text(body if body.startswith("#!") else f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def run_setup(self, *args):
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin", "FCVM_KEY": "/nonexistent"}
        return subprocess.run(["bash", str(CONNECTOR), *args], capture_output=True, text=True, timeout=60,
                              env=env, stdin=subprocess.DEVNULL)

    def test_first_run_over_the_bootstrap_tunnel_installs_everything(self):
        authorized = self.home / ".ssh/authorized_keys"
        bootstrap_line = self.keys["fcvm"] + " fcvm-arm ubuntu -> colton-mac"
        authorized.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIColtonsOwnKey0000000000000000000000000000 colton's own\n"
                              + bootstrap_line + "\n")
        result = self.run_setup("colton-mac", "--bootstrap-port", "2224")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        token = self.home / ".cloudflared/colton-mac-ssh.token"
        self.assertEqual(token.read_text().strip(), self.TOKEN)
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.TOKEN, self.argv_log.read_text(), "the token travels on stdin, never in argv")
        self.assertNotIn(self.TOKEN, result.stdout + result.stderr)
        self.assertIn("'-o', 'ProxyCommand=none', '-o', 'HostName=127.0.0.1', '-o', 'Port=2224', 'colton-mac'",
                      self.argv_log.read_text())
        plist = plistlib.loads((self.home / "Library/LaunchAgents/com.ccgames.colton-mac-ssh.cloudflared.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], [f"{self.home}/.local/bin/cloudflared", "tunnel", "--no-autoupdate",
                                                     "run", "--token-file", f"{self.home}/.cloudflared/colton-mac-ssh.token"])
        self.assertEqual((plist["Label"], plist["RunAtLoad"], plist["KeepAlive"]),
                         ("com.ccgames.colton-mac-ssh.cloudflared", True, True))
        self.assertIn("launchctl bootstrap gui/", self.launchctl.read_text())
        lines = authorized.read_text().splitlines()
        self.assertEqual(lines[0].split()[-1], "own", "the Mac's other keys are kept")
        block = lines[lines.index("# >>> colton-mac clients (managed by aws scripts/mac-ssh-connector-setup.sh) >>>") + 1:
                      lines.index("# <<< colton-mac clients <<<")]
        self.assertEqual(block, [bootstrap_line, self.keys["colton"] + " nextjs-dev colton -> colton-mac",
                                 self.keys["ejc3"] + " nextjs-dev ejc3 -> colton-mac"])
        self.assertEqual(sum(self.keys["fcvm"].split()[1] in line for line in lines), 1, "the bootstrap line is folded in")
        before = authorized.read_text()
        again = self.run_setup("colton-mac")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(authorized.read_text(), before, "a re-run changes nothing")

    def test_a_missing_client_key_stops_before_anything_is_installed(self):
        (self.home / ".ssh/to-colton-mac.pub").unlink()
        result = self.run_setup("colton-mac")
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no single valid ~/.ssh/to-colton-mac.pub", result.stderr)
        self.assertFalse((self.home / ".cloudflared").exists())

    def test_remote_login_that_takes_passwords_gets_the_keys_only_drop_in(self):
        self.offered.write_text("publickey,password,keyboard-interactive\n")
        result = self.run_setup("colton-mac")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.dropin.read_text(), "PasswordAuthentication no\nKbdInteractiveAuthentication no\n")
        self.assertIn("Remote Login: publickey only", result.stdout)

    def test_a_password_for_sudo_stops_before_the_token_with_the_command_to_type(self):
        self.offered.write_text("publickey,password\n")
        self.nosudo.write_text("")
        result = self.run_setup("colton-mac")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo tee /etc/ssh/sshd_config.d/050-keys-only.conf", result.stderr)
        self.assertFalse((self.home / ".cloudflared").exists())
        self.assertFalse((self.home / ".ssh/authorized_keys").exists())

    def test_a_key_file_with_more_than_one_line_is_refused(self):
        # Security review of #510: a .pub with an extra line could add a key, end the heredoc, or print escapes.
        bad = [self.keys["fcvm"] + "\n" + self.keys["colton"] + "\n",
               self.keys["fcvm"] + " a comment\n" + self.keys["colton"] + " second key\n",
               self.keys["fcvm"] + " c\nKEYS\nrm -rf ~\n",
               self.keys["fcvm"] + "\nKEYS\nrm -rf ~\n",
               self.keys["fcvm"] + "\r\n",
               "restrict " + self.keys["fcvm"] + "\n",
               self.keys["fcvm"] + "AA\n"]
        for text in bad:
            with self.subTest(text=text):
                (self.home / ".ssh/authorized_keys").unlink(missing_ok=True)
                (self.home / ".ssh/to-colton-mac.pub").write_text(text)
                result = self.run_setup("colton-mac")
                self.assertEqual(result.returncode, 1)
                self.assertIn("fcvm-arm ubuntu has no single valid", result.stderr)
                self.assertFalse((self.home / ".ssh/authorized_keys").exists())

    def test_one_nextjs_account_cannot_stand_in_for_another(self):
        # colton's file naming a second key "for ejc3" is colton's file with two lines: refused, whatever it says.
        (self.nextjs / "colton.pub").write_text(self.keys["colton"] + " colton\nejc3 " + self.keys["fcvm"] + "\n")
        result = self.run_setup("colton-mac")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nextjs-dev colton has no single valid", result.stderr)
        log = self.argv_log.read_text()
        self.assertIn("cat /home/colton/.ssh/to-colton-mac.pub", log)
        self.assertEqual(log.count("ssm send-command"), 1, "each account is read by its own command; colton's failed first")

    def test_bad_arguments(self):
        for args in [(), ("mac",), ("colton-mac", "--bootstrap-port"), ("colton-mac", "--bootstrap-port", "x"), ("colton-mac", "extra")]:
            with self.subTest(args=args):
                self.assertEqual(self.run_setup(*args).returncode, 2)

    def test_pins_match_the_other_mac_script(self):
        other = (ROOT / "scripts/claw-master-mac-setup.sh").read_text()
        for name in ("CFD_VERSION", "CFD_SHA256_DARWIN_ARM64_TGZ", "CFD_SHA256_DARWIN_ARM64_BIN"):
            pin = re.compile(rf"^{name}=(\S+)$", re.M)
            self.assertEqual(pin.search(CONNECTOR.read_text()).group(1), pin.search(other).group(1), name)

    def test_its_clients_are_the_ones_terraform_issues_tokens_to(self):
        text = CONNECTOR.read_text()
        self.assertIn('CLIENTS="fcvm:ubuntu nextjs:colton nextjs:ejc3"', text)
        self.assertIn("TUNNEL_SECRET=colton-mac-ssh-tunnel-token", text)
        self.assertIn('name                    = "colton-mac-ssh-tunnel-token"', COLTON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
