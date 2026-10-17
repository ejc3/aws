# The MacBook: remote access and its scratch drive

How agents on fcvm-arm reach EJ's MacBook Pro over SSH, build on it, and use its external
scratch drive without macOS stopping each program at a privacy prompt. The fcvm-arm side
is in [`mac-reverse-tunnels.tf`](mac-reverse-tunnels.tf); the Mac side is local to the
Mac and described here, so it can be checked and rebuilt.

## The route

```
fcvm-arm                                   MacBook
ssh macbook ─> 127.0.0.1:2223 ═══ reverse tunnel ═══> 127.0.0.1:22  macOS Remote Login
                                (the Mac dials out)                  (sshd, keys only)
```

- **The Mac dials out.** The `com.ejcampbell.fcvm-macbook.tunnel` LaunchAgent runs
  `ssh -NT -R 127.0.0.1:2223:127.0.0.1:22 fcvm` at login and reconnects after failures.
  No inbound port is open on the Mac or in AWS.
- **The tunnel ends at macOS Remote Login**, the system `sshd`, on the Mac's localhost.
  Until 2026-10-17 it ended at a private `sshd` on `127.0.0.1:22222`, started by the
  `com.ejcampbell.fcvm-macbook.sshd` LaunchAgent. That agent stays installed as a fallback
  (see [Recovery](#recovery)).
- **fcvm-arm's side**: `ssh macbook` is `127.0.0.1:2223` as `ejcampbell` with the
  `~/.ssh/to-mac` key, `HostKeyAlias ejs-macbook-pro-via-fcvm`, checked against
  `~/.ssh/known_hosts.mac-tunnels`. That file pins both Macs' host keys: Remote Login's and
  the private `sshd`'s, so either end of the tunnel verifies.

## On the Mac

| What | Where | State |
|---|---|---|
| Remote Login | System Settings > General > Sharing > Remote Login | on, Administrators only |
| Full disk access for remote users | the same pane, (i) | on |
| Keys only | `/etc/ssh/sshd_config.d/050-keys-only.conf` | `PasswordAuthentication no`, `KbdInteractiveAuthentication no` |
| fcvm-arm's key | `~/.ssh/authorized_keys` | the `to-mac` public key |
| The tunnel | `~/Library/LaunchAgents/com.ejcampbell.fcvm-macbook.tunnel.plist` | `-R 127.0.0.1:2223:127.0.0.1:22` |
| Fallback `sshd` | `~/Library/LaunchAgents/com.ejcampbell.fcvm-macbook.sshd.plist`, `~/.ssh/fcvm-macbook/sshd_config` | `127.0.0.1:22222`, keys only, unused |
| Scratch drive | `/Volumes/Scratch` (external APFS, 1.8 TB) | see below |

Remote Login listens on every interface, as macOS has it; with the drop-in it accepts
only public keys, and only from administrators.

## The scratch drive

The internal disk runs nearly full with the owner's own data, so builds go to the
external drive, one folder per project: `/Volumes/Scratch/<project>/`. For example
`dolphin-maps/` holds its checkout, `build/` (DerivedData, archives, result bundles:
`BUILD_DIR` in its scripts), `tours/` (screenshots) and `scratch/` (logs, helpers).
Simulator devices still live on the internal disk (CoreSimulator), so delete a tour's
simulator after use. Never write a project's files into `~/src` loose.

## Why Remote Login, not the private sshd

macOS treats an external drive as a *removable volume*, a privacy-protected location. The
first time a program touches one, macOS asks on the Mac's screen, "… would like to access
files on a removable volume", and the program waits, blocked in the system call, until
someone answers.

Who is asked about is the *responsible process*. A child normally inherits its parent's;
a process can also be started as responsible for itself
([disclaim](https://github.com/torarnv/disclaim),
[HackTricks: TCC](https://hacktricks.wiki/en/macos-hardening/macos-security-and-privilege-escalation/macos-security-protections/macos-tcc/index.html)).

- **Under the private `sshd`**, every program a session ran (node, xcodegen, CocoaPods,
  a freshly compiled test binary) was its own responsible process: each got its own
  prompt and hung until it was answered. A Full Disk Access grant on `/usr/sbin/sshd`
  did not change that.
- **Under Remote Login**, sessions are attributed to `/usr/libexec/sshd-keygen-wrapper`,
  and "Allow full disk access for remote users" is a Full Disk Access grant on that
  wrapper ([Der Flounder](https://derflounder.wordpress.com/2021/09/29/enabling-full-disk-access-for-ssh-on-macos-big-sur-using-a-management-profile/),
  [Eclectic Light](https://eclecticlight.co/2018/11/12/who-put-that-in-my-full-disk-access-list-ssh-and-mojaves-privacy-protection/)).
  Everything run over it reaches the drive with no prompt.

The cost is that any Remote Login session has full disk access. It is reachable only with
a key, and only by an administrator.

## Checks

```bash
# From fcvm-arm. The session should come from Remote Login: sshd-session under launchd,
# not the 22222 sshd.
ssh macbook 'p=$$; while [ "$p" -gt 1 ]; do ps -o comm= -p $p; p=$(ps -o ppid= -p $p | tr -d " "); done'

# A program macOS has never seen reads the drive with no prompt ("read ok", not a hang).
ssh macbook 'd=$(mktemp -d); printf "#include <stdio.h>\nint main(int c,char**v){return fopen(v[1],\"r\")?puts(\"read ok\"),0:2;}\n" > $d/p.c &&
  clang -o $d/probe-$RANDOM $d/p.c && ($d/probe-* /Volumes/Scratch/.DS_Store & K=$!;
  for i in 1 2 3 4 5 6; do kill -0 $K 2>/dev/null || exit 0; sleep 1; done; echo BLOCKED; kill $K); rm -rf $d'

# Passwords are refused: expect "Permission denied (publickey)".
ssh macbook 'ssh -o BatchMode=yes -o PubkeyAuthentication=no -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -p 22 ejcampbell@127.0.0.1 true'
```

**Symptom of the old problem:** a build step that sits at 0% CPU for minutes.
`sample <pid> 2` on the Mac shows the main thread in `getcwd` or `open` on
`/Volumes/Scratch`. Check the session's ancestry (above) before anything else.

## Recovery

- **The tunnel is down** (`ssh macbook` refused): it reconnects on its own within
  seconds while the Mac is awake and online; at the Mac,
  `launchctl kickstart -k gui/$(id -u)/com.ejcampbell.fcvm-macbook.tunnel`.
- **Remote Login is off or broken**: point the tunnel back at the fallback `sshd` (change
  `:22` to `:22222` at the end of the plist's `-R` argument, then `launchctl bootout` and
  `launchctl bootstrap` the agent). `ssh macbook` verifies it with its pinned key. The
  prompts come back until Remote Login is restored.
- **Reloading the tunnel ends every session through it**, including the programs those
  sessions started. Start long jobs on the Mac detached (`nohup … &`, logging to a file
  on the scratch drive).

## Rebuilding from nothing

1. On the Mac: turn on Remote Login (Administrators) and "Allow full disk access for
   remote users"; install the keys-only drop-in:
   `printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo tee /etc/ssh/sshd_config.d/050-keys-only.conf`.
2. Put the `to-mac` public key in `~/.ssh/authorized_keys` (mode 600).
3. Install the tunnel LaunchAgent with `-R 127.0.0.1:2223:127.0.0.1:22 fcvm`, `KeepAlive`
   and `RunAtLoad`, and bootstrap it.
4. On fcvm-arm, `mac-reverse-tunnels.tf`'s installer writes the alias and the pinned
   host keys. If the Mac's Remote Login host key changed (a reinstall), pin the new one there.
5. Run the checks above.
