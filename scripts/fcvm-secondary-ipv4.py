#!/usr/bin/env python3
"""Persist the metal host's Terraform-assigned secondary address without a network restart."""
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys


CONFIG = Path('/etc/netplan/61-fcvm-secondary-ipv4.yaml')


def configure(address, config=CONFIG, run=subprocess.run):
    address = str(ipaddress.IPv4Address(address))
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'default'],
                            check=True, capture_output=True, text=True).stdout)
    if not routes:
        raise RuntimeError('no primary IPv4 route')
    interface = min(routes, key=lambda route: route.get('metric', 0))['dev']
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', interface):
        raise ValueError('invalid primary interface')
    # /32 adds the host address without replacing its DHCP subnet or default route.
    wanted = (f'network:\n  version: 2\n  ethernets:\n    {interface}:\n'
              f'      addresses:\n        - {address}/32\n')
    previous = config.read_text() if config.exists() else None
    if previous != wanted:
        config.parent.mkdir(parents=True, exist_ok=True)
        temporary = config.with_suffix('.yaml.tmp')
        temporary.write_text(wanted)
        temporary.chmod(0o600)
        temporary.replace(config)
    try:
        # Generate boot configuration; applying netplan live would disrupt SSH and VMs.
        run(['netplan', 'generate'], check=True)
    except subprocess.CalledProcessError:
        if previous is None:
            config.unlink(missing_ok=True)
        else:
            config.write_text(previous)
            config.chmod(0o600)
        run(['netplan', 'generate'], check=True)
        raise
    run(['ip', '-4', 'address', 'replace', f'{address}/32', 'dev', interface], check=True)


if __name__ == '__main__':
    configure(sys.argv[1])
