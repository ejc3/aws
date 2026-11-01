#!/bin/bash
# Disposable Ubuntu24 native tile replica. Its immutable30-minute lease precedes work.
set -euo pipefail
umask 077
STATE_DIR=/var/lib/gpu-box-native
SOURCE=/opt/gpu-box-native-source
SOURCE_SHA=66b05370dfce40dfc4bbdab709216eac3d6e6098
ARCHIVE_SHA=d316baaa3d63b780f32feddee9882707627aae0b12645b44edb07071bb9f1e1d
ARCHIVE_BYTES=87113401
SCRIPT=/usr/local/sbin/gpu-box-native-bootstrap
[[ $EUID = 0 ]] || { echo 'native bootstrap requires root' >&2; exit 1; }
[[ ! -L $STATE_DIR ]] || exit 1
install -d -m 0700 "$STATE_DIR"

# Only IMDS identity is read; no role, credentials, secret or signed URL is used.
# Atomic/fsynced state is committed before either reboot.
state() {
  python3 - "$STATE_DIR" "$@" <<'PY'
import datetime,hashlib,json,os,pathlib,re,stat,subprocess,sys,tempfile,time,urllib.request
root=pathlib.Path(sys.argv[1]); action=sys.argv[2]; path=root/'state.json'
def write(dest,value,mode=0o600):
    assert not dest.is_symlink()
    fd,tmp=tempfile.mkstemp(prefix='.native-',dir=dest.parent)
    with os.fdopen(fd,'w') as f:
        os.fchmod(f.fileno(),mode);json.dump(value,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,dest)
    d=os.open(dest.parent,os.O_DIRECTORY);os.fsync(d);os.close(d)
def load():
    s=path.lstat();assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o077
    value=json.loads(path.read_text());assert value['version']==1
    return value
if action=='init':
    if path.exists():
        load();sys.exit(0)
    base='http://169.254.169.254/latest/'
    token=urllib.request.urlopen(urllib.request.Request(base+'api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=5).read().decode()
    request=urllib.request.Request(base+'dynamic/instance-identity/document',headers={'X-aws-ec2-metadata-token':token})
    identity=json.load(urllib.request.urlopen(request,timeout=5))
    assert re.fullmatch(r'i-[0-9a-f]{17}',identity['instanceId']) and identity['instanceType']=='g4dn.xlarge'
    launched=int(datetime.datetime.fromisoformat(identity['pendingTime'].replace('Z','+00:00')).timestamp())
    deadline=launched+1800;assert deadline>time.time()
    boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    write(path,{'version':1,'instance_id':identity['instanceId'],'launch_unix':launched,'deadline_unix':deadline,
        'phase':'fresh','boot_ids':[boot],'source_commit':'66b05370dfce40dfc4bbdab709216eac3d6e6098'})
elif action=='get':
    value=load()[sys.argv[3]]
    if sys.argv[3]=='deadline_calendar':raise AssertionError('unsupported')
    print(value if not isinstance(value,(dict,list)) else json.dumps(value))
elif action=='calendar':
    print(datetime.datetime.fromtimestamp(load()['deadline_unix'],datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
elif action=='phase':
    value=load();value['phase']=sys.argv[3]
    boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if boot not in value['boot_ids']:value['boot_ids'].append(boot)
    write(path,value)
elif action=='newboot':
    value=load();boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert boot not in value['boot_ids'],'expected reboot has not occurred'
elif action=='failed':
    value=load();value['failed_phase']=value['phase'];value['phase']='failed';value['failure_rc']=int(sys.argv[3]);write(path,value)
elif action=='snapshot':
    name=sys.argv[3];assert name in {'before','after'}
    cdi=pathlib.Path('/etc/cdi/nvidia.yaml');assert cdi.is_file() and not cdi.is_symlink()
    raw=cdi.read_bytes();assert len(raw)<1024*1024
    nodes=[];current=None
    for line in raw.decode().splitlines():
        match=re.match(r'\s*- path: (/dev/nvidia[\w-]*)\s*$',line)
        if match:
            current={'path':match[1]};nodes.append(current)
        elif current:
            match=re.match(r'\s*(major|minor): (\d+)\s*$',line)
            if match:current[match[1]]=int(match[2])
    actual={}
    for node in nodes:
        s=os.stat(node['path']);assert stat.S_ISCHR(s.st_mode)
        actual[node['path']]={'major':os.major(s.st_rdev),'minor':os.minor(s.st_rdev)}
    devices={};character=False
    for line in pathlib.Path('/proc/devices').read_text().splitlines():
        if line=='Character devices:':character=True
        elif line=='Block devices:':character=False
        elif character:
            match=re.fullmatch(r'\s*(\d+)\s+(\S+)',line)
            if match:devices[match[2]]=int(match[1])
    def command(argv):
        p=subprocess.run(argv,check=True,capture_output=True,text=True,timeout=15)
        return p.stdout.strip()
    value={'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'kernel':os.uname().release,'cdi_sha256':hashlib.sha256(raw).hexdigest(),
        'cdi_device_nodes':nodes,'actual_device_nodes':actual,'character_devices':devices,
        'driver':command(['nvidia-smi','--query-gpu=driver_version,name','--format=csv,noheader,nounits']),
        'podman':command(['podman','--version']),'toolkit':command(['nvidia-ctk','--version'])}
    write(root/(name+'.json'),value)
elif action=='ready':
    value=load();before=json.loads((root/'before.json').read_text());after=json.loads((root/'after.json').read_text())
    assert len(value['boot_ids'])==3 and len(set(value['boot_ids']))==3
    assert after['boot_id']==value['boot_ids'][-1] and after['boot_id']!=before['boot_id']
    assert before['cdi_sha256']==after['cdi_sha256'],'CDI changed across the prep reboot'
    assert after['driver'].split(',')[0].strip()=='580.196.07'
    assert after['podman']=='podman version 4.9.3'
    assert after['toolkit'].splitlines()[0]=='NVIDIA Container Toolkit CLI version 1.20.1'
    assert time.time()<value['deadline_unix']
    value.update(phase='ready',cdi_unchanged=True,kernel=after['kernel'],driver=after['driver'],podman=after['podman'],toolkit=after['toolkit'])
    write(path,value);write(pathlib.Path('/etc/gpu-box-ready'),dict(value,status='ready'),0o644)
else:raise AssertionError('unsupported state operation')
PY
}

if [[ ${1:-} = --expire ]]; then
  (( $(date +%s) >= $(state get deadline_unix) )) || exit 1
  exec /usr/sbin/shutdown -h now
fi

if [[ ${1:-} = --install ]]; then
  state init
  calendar=$(state calendar)
  cat > /etc/systemd/system/gpu-box-native-expiry.service <<EOF
[Unit]
Description=End this native GPU diagnostic at its original launch deadline
[Service]
Type=oneshot
ExecStart=$SCRIPT --expire
EOF
  cat > /etc/systemd/system/gpu-box-native-expiry.timer <<EOF
[Unit]
Description=Immutable launch plus30-minute native GPU lease
[Timer]
OnCalendar=$calendar
Persistent=true
AccuracySec=1s
RandomizedDelaySec=0
Unit=gpu-box-native-expiry.service
[Install]
WantedBy=timers.target
EOF
  cat > /etc/systemd/system/gpu-box-native-bootstrap.service <<EOF
[Unit]
Description=Build the pinned native tile environment on this disposable fixture
Wants=network-online.target
After=network-online.target cloud-final.service
[Service]
Type=oneshot
ExecStart=$SCRIPT
TimeoutStartSec=infinity
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now gpu-box-native-expiry.timer
  systemctl enable gpu-box-native-bootstrap.service
  systemctl start --no-block gpu-box-native-bootstrap.service
  exit 0
fi
[[ $# = 0 ]] || exit 2
exec >> "$STATE_DIR/bootstrap.log" 2>&1
chmod 0600 "$STATE_DIR/bootstrap.log"
# shellcheck disable=SC2154 # rc is assigned inside the EXIT trap.
trap 'rc=$?; if (( rc )); then state failed "$rc"; echo "native bootstrap failed; inspect /var/lib/gpu-box-native/state.json and bootstrap.log"; fi' EXIT
[[ $(. /etc/os-release; printf '%s:%s' "$ID" "$VERSION_ID") = ubuntu:24.04 ]]
deadline=$(state get deadline_unix)
run() {
  local left=$((deadline - $(date +%s) - 10))
  (( left > 0 )) || { echo 'native launch deadline reached'; return 1; }
  timeout --signal=TERM "$left" "$@"
}
reboot_fixture() {
  sync
  systemctl reboot --no-block
  exit 0
}
phase=$(state get phase)
case "$phase" in
fresh)
  state phase upgrade_running
  [[ ! -e $SOURCE && ! -L $SOURCE ]]
  # The public, hash-pinned tree is extracted only under this fixed owned directory.
  run python3 - "$SOURCE" "$SOURCE_SHA" "$ARCHIVE_SHA" "$ARCHIVE_BYTES" <<'PY'
import hashlib,os,pathlib,sys,tarfile,tempfile,urllib.request
dest=pathlib.Path(sys.argv[1]);sha=sys.argv[2];expected=sys.argv[3];size=int(sys.argv[4])
with tempfile.TemporaryFile() as output:
    request=urllib.request.urlopen('https://codeload.github.com/ejc3/fcvm/tar.gz/'+sha,timeout=30)
    digest=hashlib.sha256();count=0
    while True:
        block=request.read(1024*1024)
        if not block:break
        count+=len(block);assert count<=size;digest.update(block);output.write(block)
    assert count==size and digest.hexdigest()==expected
    output.seek(0)
    with tarfile.open(fileobj=output) as archive:
        members=archive.getmembers();assert len(members)<100000 and sum(m.size for m in members)<2*1024**3
        prefix='fcvm-'+sha+'/'
        assert all(m.name==prefix[:-1] or m.name.startswith(prefix) for m in members)
        dest.mkdir(mode=0o755)
        for member in members:
            if member.name==prefix[:-1]:continue
            member.name=member.name[len(prefix):]
            archive.extract(member,dest,filter='data')
PY
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  systemctl stop apt-daily.timer apt-daily-upgrade.timer
  run apt-get -o DPkg::Lock::Timeout=120 update
  run apt-get -o DPkg::Lock::Timeout=120 -y --with-new-pkgs upgrade
  state phase upgrade_reboot_pending
  reboot_fixture
  ;;
upgrade_reboot_pending)
  state newboot
  state phase prep_running
  run bash "$SOURCE/fleet/ami/tile/build.sh" prep
  # Nothing may request production TLS or start a renderer before artifact loading.
  systemctl disable --now fcvm-fleet-tls.service fcvm-fleet-renderer.service fcvm-fleet-first-frame.service fcvm-fleet-nginx.service
  state snapshot before
  state phase prep_reboot_pending
  reboot_fixture
  ;;
prep_reboot_pending)
  state newboot
  state phase checking
  /usr/local/sbin/cve-2026-98432-mitigation --check
  state snapshot after
  state ready
  echo 'native tile environment ready; static CDI preserved across reboot'
  ;;
ready) exit 0 ;;
*) echo "native bootstrap refuses to replay interrupted phase: $phase"; exit 1 ;;
esac
