# This Mac mini (agents read this first)

A shared build and test Mac. Agents reach it with `ssh mini` from a dev box. Installed and kept
current by ejc3/aws `scripts/mac-setup.sh mini`; the sections marked "found" below are written
by `mac-agents-md` from what is on the machine now (hourly, and at every setup). Do not edit this
file by hand: change `scripts/mac/mini-AGENTS.md` in ejc3/aws.

## Rules

- **A workspace of your own per task:** `(umask 077; mktemp -d ~/agent-work/task.XXXXXXXX)`. Copy
  the exact revision in; copy results out; remove only what you made.
- **Heavy builds take a slot:** `mac-build --status` shows the two. `swift`, `xcodebuild` and
  `xcrun` in `~/.local/bin` take one automatically.
- **No keys or tokens are copied here.** An App Store Connect key stays on the box that holds it:
  make provisioning profiles there (through the API) and send only the profile.
- Other agents' directories under `~/agent-work` are theirs: read them for how something was done,
  never change or delete them.

## Building an iOS app on the host

The CI runners are VMs on this machine; the host has its own toolchain for a build a device
needs. In a build shell: `source ~/agent-tools/env.zsh` (Node, pnpm, XcodeGen, Ruby 3.3 and
CocoaPods; the system Ruby is too old for CocoaPods). Then as CI does: `pnpm install
--frozen-lockfile --ignore-scripts`, `xcodegen generate`, `pod install`, `xcodebuild`.

**Signing for a device.** The login keychain is locked to every session but the owner's own, so
`codesign` over SSH (or in a LaunchAgent) fails with `errSecInternalComponent`. Sign with a
development keychain made for agents (found below): unlock it for the build with the password
file beside it (read on this machine, never printed or copied off), and pass
`OTHER_CODE_SIGN_FLAGS="--keychain <path>"`. Point only the app's and its test target's signing
at their profiles, in your copy of the project, so the Pods targets keep theirs; a wildcard App
ID's profile covers a UI-test runner.

## Testing on the attached iPhones

The phones below are on USB with Developer Mode on, signed in to the owner's account, with
TestFlight. A check that needs a real device (a universal link tapped, a sign-in sheet, push, the
camera) is yours to run: never hand the owner a step these phones can do.

Limits on those phones: work only in the app under test, TestFlight and the system prompts it raises. No
purchases, no payment or Apple Account sheets, no settings outside the app's own, and nothing in the
owner's other apps.

- `xcrun devicectl device info apps` does not list App Store or TestFlight apps, not even with
  `--include-all-apps`. An app missing from it proves nothing.
- Use the build testers run first: the project's TestFlight build, installed through TestFlight
  on the phone. Build and sign here only when no TestFlight build has the change.
- **A locked phone refuses every launch** ("Unable to launch … because the device is locked"). A
  phone asleep with no passcode asked for (`devicectl device info lockState`) wakes in the test with
  `XCUIDevice.shared.press(.home)` and a swipe up on the springboard; only a passcode needs the owner.
- Open a link in the app with a UI test, `XCUIApplication(bundleIdentifier:).open(url)`, as iOS hands
  it over once it has resolved a universal link. `XCUIDevice.shared.system.open(url)` opens an `https`
  URL in the browser, never through universal links. Whether iOS routes the domain rests on the signed
  entitlement and Apple's CDN copy of the association file; a real tap needs a link in another app.
  System prompts belong to `XCUIApplication(bundleIdentifier: "com.apple.springboard")`, a sign-in
  sheet's page to `com.apple.SafariViewService`; keep screenshots with `XCTAttachment(screenshot:)`.
  Pass values to the runner as `TEST_RUNNER_<NAME>` in `xcodebuild`'s environment (not as build
  settings); it sees `<NAME>`.
