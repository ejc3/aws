#!/usr/bin/env python3
"""Install the MacBook's Scratch cleaner for its existing Remote Login route."""

import os
from pathlib import Path
import plistlib
import runpy
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError


LABEL = "com.ccgames.mac-artifact-clean"


def install(path, contents, mode, create_once=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_once and os.path.lexists(path):
        return False
    if path.is_file() and path.read_bytes() == contents and (path.stat().st_mode & 0o777) == mode:
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        tmp = Path(output.name)
        try:
            output.write(contents)
            output.flush()
            os.fchmod(output.fileno(), mode)
            if create_once:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    return False
            else:
                os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    return True


def loaded(service):
    return (
        subprocess.run(
            ["launchctl", "print", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def enroll(pin, volume, cleaner):
    try:
        if not volume.is_mount() or volume.resolve(strict=True) != volume:
            raise ValueError(f"{volume} is not a real mount point")
        observed = runpy.run_path(str(cleaner))["volume_id"](volume)
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError, subprocess.SubprocessError) as error:
        print(
            f"Scratch cleaner held. {error}. "
            "Connect the Scratch disk and rerun scripts/mac-setup.sh mac --artifact-cleaner-only."
        )
        return False
    # A concurrent setup can win enrollment. Compare its complete pin before reporting success.
    install(pin, (observed + "\n").encode("ascii"), 0o600, create_once=True)
    if pin.is_symlink() or not pin.is_file() or pin.read_text().strip().lower() != observed:
        print(f"Scratch cleaner held. {volume} does not match the UUID enrolled in {pin}.")
        return False
    return True


def main(source, volume=Path("/Volumes/Scratch")):
    home = Path.home()
    bin_dir = home / ".local/bin"
    install(bin_dir / "mac-build", (source / "bin/mac-build").read_bytes(), 0o755)
    cleaner = bin_dir / "mac-artifact-clean"
    install(cleaner, (source / "bin/mac-artifact-clean").read_bytes(), 0o755)
    # The GUI agent lacks macOS removable-volume access. Its replacement runs the
    # cleaner through the same Remote Login account that already builds on Scratch.
    plist = home / "Library/LaunchAgents" / (LABEL + ".plist")
    if plist.exists():
        service = f"gui/{os.getuid()}/{LABEL}"
        if loaded(service):
            subprocess.run(["launchctl", "bootout", service], check=True)
        plist.unlink(missing_ok=True)
    if enroll(home / ".config/mac-setup/scratch-volume-uuid", volume, cleaner):
        print("Scratch cleaner installed for Remote Login.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
