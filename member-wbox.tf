# member-wbox.tf
#
# EACH MEMBER'S WBOX: one Windows spot box with an NVIDIA GPU per person in
# local.member_box_people (member-boxes.tf), for playtesting Roblox games (Roblox Player and
# Roblox Studio). Name tag, launch template and everything durable are "wbox-<person>". The
# member runs `wbox up` / `wbox down` from their own account (scripts/wbox.sh hands a member's
# commands to scripts/wbox-member.sh); the owner's own wbox (wbox.tf) is a different, stop/start
# box and is unchanged.
#
# THE PARALLEL-BOX SHAPE (parallel-box-launch.tf), not the owner's wbox shape: terraform owns
# the durable half and a dev box launches and terminates the instance itself with a tag-scoped
# grant. It is the same documented exception to AGENTS.md rule 5, not a new one: the instance
# is ephemeral and never in state.
#
#   durable  (terraform): per-person D: data disk (prevent_destroy), launch template, instance
#                         role, Administrator password, fixed private IP; the shared subnet,
#                         security group, IAM grant and watchdog
#   ephemeral (wbox):     the instance, its Windows root disk, and its attachment to D:
#
# WHAT PERSISTS AND WHAT DOES NOT. Each person's 50 GB D: disk outlives every instance. On it:
#   - D:\Roblox: the box's %LOCALAPPDATA%\Roblox (the Player and Studio programs, their
#     versions and caches) and Documents\Roblox (Studio's local files and auto-saves) are
#     junctions to it, made at each logon by a scheduled task, so neither is downloaded again;
#   - D:\wbox\cache: the NVIDIA GRID driver, the DCV server and the two Roblox installers,
#     so a later launch copies them from D: instead of downloading them.
# The Windows root disk (C:) is new at every launch and deleted with the instance: the
# Windows profile, the registry and Windows' credential store go with it. So the Roblox
# sign-ins do not survive (Roblox encrypts its sign-in with keys kept on C:): each session
# signs in again, quickest with Quick Sign-in from a phone. Anything saved outside D:
# (Desktop, other folders) is gone when the box is. At the first logon of a launch the task
# runs the cached Player and Studio installers once, which with the files already on D: only
# re-registers them (their roblox-player: and roblox-studio: links live in the registry on C:);
# either may open a window.
#
# SPOT, AND WHERE. us-west-1a: g4dn.xlarge Windows spot there averaged $0.264/hour over the
# 30 days to 2026-10-18 (peak $0.270), the same as us-west-2, and scored 9/10 for spot
# placement (us-west-1c scored 1-2). us-east-2, where the parallel boxes went for price, is
# $0.42 for this pool. And us-west-1 is the dev fleet's own VPC, so the private path the
# perimeter rule needs already exists with no new peering. The data disks pin the AZ.
# Instance types, tried in order by wbox up (local.member_wbox_types; the grant allows only
# these): g4dn.xlarge (4 vCPU, 16 GB, NVIDIA T4), then g4dn.2xlarge (8 vCPU, 32 GB, the same
# T4 and the same GRID driver, about $0.47/hour). Not g5 or g6: us-west-1 does not offer them.
# Not g4ad: an AMD GPU, which the NVIDIA driver setup below does not cover. A reclaim ends the
# session with two minutes' notice; D: survives and `wbox up` starts another.
#
# COST. Running: about $0.27/hour (spot g4dn.xlarge plus the 60 GB root disk and a public
# IPv4 address for outbound traffic). Down: each person's 50 GB gp3 disk, about $4.80/month,
# plus its password secret ($0.40/month): about $20.80/month for the four. The watchdog
# (below) terminates a box after ONE hour below 15% CPU, checked every 5 minutes, and at a
# hard lifetime of local.member_box_max_hours (6) from launch whatever it is doing, because a
# Roblox game left running keeps the CPU busy and would never look idle; a forgotten session
# therefore costs at most about $1.60. 15%, not the 5% the parallel boxes use: idle Windows
# with its DCV agent measured 6.4% average and 11.7% peak on wbox.
#
# REACHABLE ONLY FROM THE DEV BOXES (the perimeter rule: nothing new listens on the internet).
# The security group admits RDP (3389) and Amazon DCV (8443, TCP and UDP) from the dev fleet's
# subnets only. Each box has a FIXED private address (local.member_wbox_ips, in an explicit
# subnet CIDR reservation EC2 never assigns on its own), so a member's container can allow
# exactly that one. A player reaches the box through the box they log in to: an SSH tunnel
# from their own computer, `ssh -L 8443:<private IP>:8443 <that box>`, then
# https://localhost:8443 in a browser (DCV's web client) or the DCV client app; `wbox connect`
# prints it. The boxes have their own subnet whose route table has the internet and nothing
# else -- no I/O-box peer route -- as wbox's has, so a desktop that runs downloaded games
# cannot reach the fleet's NFS scratch.
#
# FIRST BOOT OF EACH LAUNCH (user_data): first the box's own lifetime backstop (below), then
# the setup, tried twice; if both attempts fail the box shuts itself down, which terminates it
# (instance_initiated_shutdown_behavior), so the next `wbox up` starts clean. It sets
# the Administrator password from Secrets Manager (wbox-<person>/administrator-password,
# generated here), finds the D: disk by its EBS volume id and formats it only if RAW,
# installs the NVIDIA GRID driver and Amazon DCV (each from the D: cache when it holds the
# current one), Media Foundation, refreshes the cached Roblox installers, registers the logon
# task, and reboots. It takes Windows' first boot plus that: expect `wbox up` to wait 15-20
# minutes before DCV answers.
#
# ONE INSTANCE ROLE PER PERSON, so a box can read its own password and no one else's. Each
# password is readable by administration, its own box's role and nextjs-dev-role (narrowed to
# its own person by a container's session policy).
#
# WHO MAY LAUNCH: nextjs-dev-role, where the members work (nextjs-dev, and nextjs-ct's
# per-container sessions of that role). An instance-role grant is box-wide (every account on
# nextjs-dev has sudo); what bounds it is the policy below -- four names, four templates, two
# instance types, spot only, these four roles only -- and the watchdog's idle and lifetime
# limits, backed by alarms on the watchdog and by each box's own deadline task. Every resource
# in it is per person, so a session policy can narrow it to one.

locals {
  # One box per member; each needs nothing else (its volume, template, role, password, address
  # and watchdog entry are keyed on the same map). Person P is Name tag and template "wbox-P".
  member_wbox_slots = { for p in local.member_box_people : p => "wbox-${p}" }
  member_wbox_names = [for k in sort(keys(local.member_wbox_slots)) : local.member_wbox_slots[k]]

  member_wbox_region = var.aws_region # us-west-1, the dev fleet's VPC
  member_wbox_az     = aws_subnet.subnet_a.availability_zone

  # Cheapest first; scripts/wbox-member.sh tries them in this order and the grant allows only these.
  member_wbox_types = ["g4dn.xlarge", "g4dn.2xlarge"]

  member_wbox_data_size    = 50
  member_wbox_root_size    = 60
  member_wbox_idle_minutes = 60
  member_wbox_idle_cpu_pct = 15

  member_wbox_client_cidr = [for s in local.dev_fleet_subnets : s.cidr_block]

  # .8/29 of the subnet: explicit, so EC2 never hands one of them to anything else. Person i
  # (member_box_index) has .10 + i.
  member_wbox_reserved_cidr = cidrsubnet(aws_subnet.member_wbox.cidr_block, 5, 1)
  member_wbox_ips           = { for p, i in local.member_box_index : p => cidrhost(local.member_wbox_reserved_cidr, 2 + i) }

  # The tag keys each launch template writes, per created resource. IAM evaluates aws:TagKeys
  # for each resource of RunInstances separately, so a launch cannot add DevEBS=true or any
  # other key a tag-based policy trusts (gpu-box.tf does the same).
  member_wbox_tag_keys = {
    instance = ["Name", "Purpose"]
    volume   = ["Name", "Role"]
    eni      = ["Name"]
  }

  member_wbox_account       = data.aws_caller_identity.current.account_id
  member_wbox_instance_arn  = "arn:aws:ec2:${local.member_wbox_region}:${local.member_wbox_account}:instance/*"
  member_wbox_volume_arn    = "arn:aws:ec2:${local.member_wbox_region}:${local.member_wbox_account}:volume/*"
  member_wbox_eni_arn       = "arn:aws:ec2:${local.member_wbox_region}:${local.member_wbox_account}:network-interface/*"
  member_wbox_template_arns = [for k in sort(keys(local.member_wbox_slots)) : aws_launch_template.member_wbox[k].arn]
  member_wbox_data_arns     = [for k in sort(keys(local.member_wbox_slots)) : aws_ebs_volume.member_wbox_data[k].arn]
  member_wbox_profile_arns  = [for k in sort(keys(local.member_wbox_slots)) : aws_iam_instance_profile.member_wbox[k].arn]
  member_wbox_role_arns     = [for k in sort(keys(local.member_wbox_slots)) : aws_iam_role.member_wbox[k].arn]
  member_wbox_secret_arns   = [for k in sort(keys(local.member_wbox_slots)) : aws_secretsmanager_secret.member_wbox_admin[k].arn]
}

data "aws_ssm_parameter" "member_wbox_ami" {
  name = "/aws/service/ami-windows-latest/Windows_Server-2025-English-Full-Base"
}

# ============================================================ network
resource "aws_subnet" "member_wbox" {
  vpc_id                  = local.vpc_id
  cidr_block              = cidrsubnet(data.aws_vpc.selected.cidr_block, 8, 69)
  availability_zone       = local.member_wbox_az
  map_public_ip_on_launch = false

  tags = { Name = "member-wbox", Purpose = "windows-playtest" }
}

# Internet and the VPC's implicit local route, nothing else. Never add the I/O box peer route.
resource "aws_route_table" "member_wbox" {
  vpc_id = local.vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "member-wbox-rt", Purpose = "windows-playtest" }
}

resource "aws_route_table_association" "member_wbox" {
  subnet_id      = aws_subnet.member_wbox.id
  route_table_id = aws_route_table.member_wbox.id
}

# The members' fixed addresses: explicit, so EC2 never assigns one of them on its own.
resource "aws_ec2_subnet_cidr_reservation" "member_wbox" {
  subnet_id        = aws_subnet.member_wbox.id
  cidr_block       = local.member_wbox_reserved_cidr
  reservation_type = "explicit"
  description      = "Fixed private addresses of the members' wboxes (member-wbox.tf)"
}

resource "aws_security_group" "member_wbox" {
  name_prefix = "member-wbox-"
  description = "Windows playtest spot boxes: RDP and DCV from the dev fleet subnets only"
  vpc_id      = local.vpc_id

  dynamic "ingress" {
    for_each = { rdp = ["tcp", 3389], dcv-tcp = ["tcp", 8443], dcv-udp = ["udp", 8443] }
    content {
      description = ingress.key
      from_port   = ingress.value[1]
      to_port     = ingress.value[1]
      protocol    = ingress.value[0]
      cidr_blocks = local.member_wbox_client_cidr
    }
  }

  # Roblox, its downloads, Windows Update and online play use many ports; the subnet's route
  # table is what keeps it off the fleet's private peers.
  egress {
    description = "all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "member-wbox" }

  lifecycle {
    create_before_destroy = true
  }
}

# ============================================================ the D: disks
# One per person. Losing one loses only reinstallable programs and caches, plus whatever a kid
# saved in Documents\Roblox, but nothing should delete one by accident.
resource "aws_ebs_volume" "member_wbox_data" {
  for_each          = local.member_wbox_slots
  availability_zone = local.member_wbox_az
  size              = local.member_wbox_data_size
  type              = "gp3"
  encrypted         = true

  tags = { Name = "${each.value}-data", Purpose = "windows-playtest" }

  lifecycle {
    prevent_destroy = true
  }
}

# ============================================================ passwords
resource "random_password" "member_wbox_admin" {
  for_each         = local.member_wbox_slots
  length           = 24
  special          = true
  override_special = "!#%*+-=?@^_"
}

resource "aws_secretsmanager_secret" "member_wbox_admin" {
  for_each                = local.member_wbox_slots
  name                    = "${each.value}/administrator-password"
  description             = "Administrator password of Windows playtest box ${each.value} (member-wbox.tf). Generated by Terraform."
  recovery_window_in_days = 7
  tags                    = { Name = "${each.value}/administrator-password", Purpose = "windows-playtest" }
}

resource "aws_secretsmanager_secret_version" "member_wbox_admin" {
  for_each      = local.member_wbox_slots
  secret_id     = aws_secretsmanager_secret.member_wbox_admin[each.key].id
  secret_string = random_password.member_wbox_admin[each.key].result
}

resource "aws_secretsmanager_secret_policy" "member_wbox_admin" {
  for_each   = local.member_wbox_slots
  secret_arn = aws_secretsmanager_secret.member_wbox_admin[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationItsBoxAndNextjsDevCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.member_wbox_admin[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.member_wbox[each.key].arn, aws_iam_role.nextjs_dev.arn]) } }
    }]
  })
}

# ============================================================ each box's own role
# Systems Manager (a second way in for an admin, and how a first boot is checked), the NVIDIA
# driver and DCV license buckets AWS publishes for EC2, and its own password: one role per
# person, so one person's box cannot read another's password.
resource "aws_iam_role" "member_wbox" {
  for_each = local.member_wbox_slots
  name     = "${each.value}-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" } }]
  })
  tags = { Name = "${each.value}-role" }
}

resource "aws_iam_role_policy_attachment" "member_wbox_ssm" {
  for_each   = local.member_wbox_slots
  role       = aws_iam_role.member_wbox[each.key].name
  policy_arn = aws_iam_policy.ssm_managed_instance.arn
}

resource "aws_iam_role_policy" "member_wbox" {
  for_each = local.member_wbox_slots
  name     = "${each.value}-setup"
  role     = aws_iam_role.member_wbox[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "NvidiaGridDriver"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetObject"]
        Resource = ["arn:aws:s3:::ec2-windows-nvidia-drivers", "arn:aws:s3:::ec2-windows-nvidia-drivers/*"]
      },
      {
        Sid      = "DcvLicense"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::dcv-license.${local.member_wbox_region}/*"
      },
      {
        Sid      = "OwnAdministratorPassword"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.member_wbox_admin[each.key].arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "member_wbox" {
  for_each = local.member_wbox_slots
  name     = "${each.value}-profile"
  role     = aws_iam_role.member_wbox[each.key].name
}

# ============================================================ first boot of each launch
locals {
  # Runs at every boot (persist). C: is new at every launch, so every launch runs it once:
  #
  # 1. THE LIFETIME BACKSTOP, at every boot and before anything that can fail. The first run
  #    of a launch writes C:\wbox\deadline.txt (the first boot's time plus
  #    local.member_box_max_hours, the watchdog's own lifetime) and every run registers the
  #    wbox-deadline task: at startup and every 15 minutes, as SYSTEM, it runs
  #    `shutdown /s /t 0` once the deadline has passed, and the shutdown terminates the
  #    instance. A spot box that the watchdog Lambda stopped watching (an error, a broken
  #    schedule; both alarm below) therefore still ends. Not `shutdown /t 21600`: the setup's
  #    own Restart-Computer would cancel a pending shutdown.
  # 2. THE SETUP, until one run completes every step; only then is the marker written on C:.
  #    A failure (the password or its grant still propagating, D: not attached within ten
  #    minutes, an installer failing) is retried once in the same run. If the retry fails too
  #    the box shuts itself down within a minute: a box that cannot finish its setup only
  #    runs up a bill, and nothing would reboot it to try again.
  member_wbox_user_data = {
    for slot, name in local.member_wbox_slots : slot => <<-PS
<powershell>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Start-Transcript -Path 'C:\wbox-setup.log' -Append

function Set-WboxDeadline {
  New-Item -ItemType Directory -Force 'C:\wbox' | Out-Null
  if (-not (Test-Path 'C:\wbox\deadline.txt')) {
    # The first run of a launch is on its first boot (C: is new), so that boot is the launch.
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()
    Set-Content -Path 'C:\wbox\deadline.txt' -Value $boot.AddHours(${local.member_box_max_hours}).ToString('o')
  }
  # An unreadable deadline ends the box too: this is the backstop for a forgotten bill.
  Set-Content -Path 'C:\wbox\deadline.ps1' -Encoding UTF8 -Value @'
try {
  $deadline = [DateTime]::Parse((Get-Content -Raw 'C:\wbox\deadline.txt').Trim(), [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
} catch { $deadline = [DateTime]::MinValue }
if ([DateTime]::UtcNow -ge $deadline.ToUniversalTime()) { shutdown.exe /s /t 0 /f /c 'wbox lifetime reached' }
'@
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\wbox\deadline.ps1'
  $triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15))
  )
  $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  Register-ScheduledTask -TaskName 'wbox-deadline' -Action $action -Trigger $triggers -Principal $principal -Force | Out-Null
}

function Invoke-WboxSetup {
  $pw = $null
  for ($i = 0; $i -lt 30 -and -not $pw; $i++) {
    try { $pw = (Get-SECSecretValue -SecretId '${aws_secretsmanager_secret.member_wbox_admin[slot].name}' -Region '${local.member_wbox_region}').SecretString }
    catch { Start-Sleep -Seconds 10 }
  }
  if (-not $pw) { throw 'could not read the Administrator password' }
  net user Administrator $pw | Out-Null

  # D: is this box's EBS data disk, found by its serial (the volume id without its hyphen) --
  # never the instance-store NVMe disk, which is RAW too and wiped with the instance. wbox up
  # attaches it after the instance is running: wait for it.
  $serial = '${replace(aws_ebs_volume.member_wbox_data[slot].id, "-", "")}'
  $disk = $null
  for ($i = 0; $i -lt 60 -and -not $disk; $i++) {
    $disk = Get-Disk | Where-Object { $_.SerialNumber -like "$serial*" }
    if (-not $disk) { Start-Sleep -Seconds 10 }
  }
  if (-not $disk) { throw "data disk $serial is not attached" }
  if ($disk.IsOffline) { $disk | Set-Disk -IsOffline $false }
  if ($disk.IsReadOnly) { $disk | Set-Disk -IsReadOnly $false }
  # Free D: if another volume (the instance store, a DVD) took it.
  $other = Get-Partition -DriveLetter D -ErrorAction SilentlyContinue | Where-Object DiskNumber -ne $disk.Number
  if ($other) {
    $free = [char[]]('EFGHIJKLMNOPQRSTUVWXYZ') | Where-Object { -not (Get-PSDrive -Name $_ -ErrorAction SilentlyContinue) } | Select-Object -First 1
    $other | Set-Partition -NewDriveLetter $free
  }
  if ($disk.PartitionStyle -eq 'RAW') {
    $disk | Initialize-Disk -PartitionStyle GPT -PassThru | New-Partition -DriveLetter D -UseMaximumSize |
      Format-Volume -FileSystem NTFS -NewFileSystemLabel 'Wbox' -Confirm:$false | Out-Null
  } else {
    # Already formatted (any launch after the first): never reformat, only make sure it is D:.
    $part = $disk | Get-Partition | Where-Object Type -eq 'Basic' | Select-Object -First 1
    if ($part -and $part.DriveLetter -ne 'D') { $part | Set-Partition -NewDriveLetter D }
  }

  $cache = 'D:\wbox\cache'
  New-Item -ItemType Directory -Force $cache, 'D:\Roblox' | Out-Null

  # NVIDIA GRID driver: the newest one in AWS's bucket, downloaded only when D: does not
  # already hold that file.
  $nv = Join-Path $cache 'nvidia'
  New-Item -ItemType Directory -Force $nv | Out-Null
  $obj = Get-S3Object -BucketName 'ec2-windows-nvidia-drivers' -KeyPrefix 'latest/' -Region 'us-east-1' |
    Where-Object Key -like '*.exe' | Select-Object -First 1
  if (-not $obj) { throw 'no NVIDIA driver listed' }
  $exe = Join-Path $nv (Split-Path $obj.Key -Leaf)
  if (-not ((Test-Path $exe) -and (Get-Item $exe).Length -eq $obj.Size)) {
    Get-ChildItem $nv | Remove-Item -Force
    Read-S3Object -BucketName $obj.BucketName -Key $obj.Key -File "$exe.part" -Region 'us-east-1' | Out-Null
    Move-Item -Force "$exe.part" $exe
  }
  $p = Start-Process $exe -ArgumentList '-s', '-noreboot', '-clean' -Wait -PassThru
  Write-Output "NVIDIA installer exit code $($p.ExitCode)"
  # NVIDIA documents 0 (success) and 1 (success, reboot required: the setup reboots at its
  # end); anything else is a failure, and a GPU box without its driver is not done.
  if ($p.ExitCode -notin 0, 1) { throw "NVIDIA driver install failed: exit code $($p.ExitCode)" }
  New-Item -Path 'HKLM:\SOFTWARE\NVIDIA Corporation\Global\GridLicensing' -Force | Out-Null
  New-ItemProperty -Path 'HKLM:\SOFTWARE\NVIDIA Corporation\Global\GridLicensing' -Name 'NvCplDisableManageLicensePage' -PropertyType DWord -Value 1 -Force | Out-Null

  # Amazon DCV server: the cached installer, fetched again when it is over 30 days old.
  $dcv = Join-Path $cache 'nice-dcv-server-x64-Release.msi'
  if (-not (Test-Path $dcv) -or ((Get-Date) - (Get-Item $dcv).LastWriteTime).TotalDays -gt 30) {
    Invoke-WebRequest 'https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-server-x64-Release.msi' -OutFile "$dcv.part" -UseBasicParsing
    Move-Item -Force "$dcv.part" $dcv
  }
  $p = Start-Process msiexec -ArgumentList '/i', $dcv, 'ADDLOCAL=ALL', 'AUTOMATIC_SESSION_OWNER=Administrator', '/quiet', '/norestart' -Wait -PassThru
  if ($p.ExitCode -notin 0, 3010) { throw "DCV install failed: $($p.ExitCode)" }

  Install-WindowsFeature Server-Media-Foundation | Out-Null

  # The Roblox installers, refreshed on D: at every launch. Not fatal: a failed download keeps
  # the cached copy, and Roblox can always be installed from roblox.com in the session.
  foreach ($r in @(@('RobloxPlayerInstaller.exe', 'https://www.roblox.com/download/client'),
                   @('RobloxStudioInstaller.exe', 'https://www.roblox.com/download/studio'))) {
    $f = Join-Path $cache $r[0]
    try {
      Invoke-WebRequest $r[1] -OutFile "$f.part" -UseBasicParsing
      Move-Item -Force "$f.part" $f
    } catch { Write-Output "could not refresh $($r[0]), keeping any cached copy: $_" }
  }

  # At each logon of Administrator, in that session: point Roblox's per-user folders at D:,
  # then run the cached installers once per launch (the registry they write is on C:).
  New-Item -ItemType Directory -Force 'C:\wbox' | Out-Null
  Set-Content -Path 'C:\wbox\logon.ps1' -Encoding UTF8 -Value @'
$ErrorActionPreference = 'Continue'
Start-Transcript -Path 'C:\wbox\logon.log' -Append | Out-Null
$keep = @{
  (Join-Path $env:LOCALAPPDATA 'Roblox') = 'D:\Roblox\LocalAppData'
  (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Roblox') = 'D:\Roblox\Documents'
}
foreach ($link in $keep.Keys) {
  New-Item -ItemType Directory -Force $keep[$link] | Out-Null
  $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
  if ($item -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    # Something wrote a real folder there first: keep it beside the junction, never delete it.
    Rename-Item -LiteralPath $link -NewName ('Roblox.local-' + (Get-Date -Format yyyyMMddHHmmss))
    $item = $null
  }
  if (-not $item) { New-Item -ItemType Junction -Path $link -Target $keep[$link] | Out-Null }
}
foreach ($app in @(@('roblox-player', 'RobloxPlayerInstaller.exe'), @('roblox-studio', 'RobloxStudioInstaller.exe'))) {
  if (Test-Path ('HKCU:\Software\Classes\' + $app[0])) { continue }
  $exe = Join-Path 'D:\wbox\cache' $app[1]
  if (-not (Test-Path $exe)) { Write-Output "no cached $($app[1]); install it from roblox.com"; continue }
  Start-Process $exe -PassThru | Wait-Process -Timeout 900 -ErrorAction SilentlyContinue
}
Stop-Transcript | Out-Null
'@
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\wbox\logon.ps1'
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User 'Administrator'
  $principal = New-ScheduledTaskPrincipal -UserId 'Administrator' -LogonType Interactive
  Register-ScheduledTask -TaskName 'wbox-roblox' -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
}

try {
  Set-WboxDeadline
} catch {
  Write-Output "could not set the lifetime backstop: $_ -- shutting down rather than run without it"
  Stop-Transcript
  shutdown.exe /s /t 60 /c 'wbox lifetime backstop failed'
  exit 1
}
if (Test-Path 'C:\wbox-setup.done') { Stop-Transcript; exit 0 }
$done = $false
for ($attempt = 1; $attempt -le 2 -and -not $done; $attempt++) {
  try {
    Invoke-WboxSetup
    $done = $true
  } catch {
    Write-Output "wbox setup attempt $attempt failed: $_"
  }
}
if ($done) {
  New-Item 'C:\wbox-setup.done' -ItemType File | Out-Null
  Stop-Transcript
  Restart-Computer -Force
} else {
  # Nothing reboots a box to try again, and a box that cannot finish only runs up a bill:
  # shut down, which terminates the instance; the next wbox up starts clean.
  Write-Output 'wbox setup failed twice: shutting down (the instance terminates)'
  Stop-Transcript
  shutdown.exe /s /t 60 /c 'wbox setup failed'
}
</powershell>
<persist>true</persist>
PS
  }
}

# ============================================================ launch templates
# The whole launch configuration except the instance type, which wbox up picks by capacity
# from local.member_wbox_types (the grant pins it to that list and to these templates). One
# template per person, identical but for the Name it writes and the D: disk its boot script
# looks for. image_id follows the newest Windows Server 2025 AMI at each apply; a running
# box is never touched by that, the next launch simply boots the newer image.
resource "aws_launch_template" "member_wbox" {
  for_each               = local.member_wbox_slots
  name                   = each.value
  image_id               = nonsensitive(data.aws_ssm_parameter.member_wbox_ami.value)
  update_default_version = true

  # An OS shutdown from inside Windows ends the instance rather than leaving one behind
  # (a one-time spot instance cannot be started again anyway).
  instance_initiated_shutdown_behavior = "terminate"

  iam_instance_profile {
    arn = aws_iam_instance_profile.member_wbox[each.key].arn
  }

  # One-time spot that terminates on reclaim: nothing on the instance outlives it but D:,
  # which is a separate volume, detached rather than deleted.
  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  # C: is disposable and recreated at every launch.
  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = local.member_wbox_root_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # The subnet pins the AZ, which the D: disk pins: EBS is AZ-locked. The person's fixed
  # private address, from the explicit reservation. A public IPv4 address for outbound traffic
  # only; the security group admits the dev fleet alone.
  network_interfaces {
    subnet_id                   = aws_subnet.member_wbox.id
    private_ip_address          = local.member_wbox_ips[each.key]
    security_groups             = [aws_security_group.member_wbox.id]
    associate_public_ip_address = true
    delete_on_termination       = true
  }

  # Tags at launch on all three created resources: the grant tests aws:RequestTag/Name on the
  # instance, its root volume AND its ENI (the untagged-ENI trap in AGENTS.md), and the
  # watchdog finds the box by the same tag. The root volume carries the box's name, Role=root.
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = each.value, Purpose = "windows-playtest" }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = each.value, Role = "root" }
  }

  tag_specifications {
    resource_type = "network-interface"
    tags          = { Name = each.value }
  }

  tags = { Name = each.value }

  user_data = base64encode(local.member_wbox_user_data[each.key])

  # The boot script reads the password with this role: both must exist before Windows starts.
  depends_on = [
    aws_secretsmanager_secret_version.member_wbox_admin,
    aws_secretsmanager_secret_policy.member_wbox_admin,
    aws_iam_role_policy.member_wbox,
    aws_iam_role_policy_attachment.member_wbox_ssm,
    aws_ec2_subnet_cidr_reservation.member_wbox,
  ]
}

# ============================================================ what a dev box may do
# Launch the members' tagged boxes from their templates as one of two types, attach each
# one's own D: disk, read the passwords, and terminate them. Every resource is per person (a
# session policy narrows it to one: output.member_boxes). The statement split follows
# parallel-box-launch.tf and gpu-box.tf: a condition holds only for the resources that carry
# its key, so the instance, its root volume, its ENI and the referenced resources each get a
# statement. EVERY RunInstances statement requires one of these templates: the role also holds
# the parallel-box and GPU-box grants, and IAM checks each resource of a call against the union, so
# without the pin a call could mix this policy's AMI or PassRole with another's tags.
data "aws_iam_policy_document" "member_wbox_control" {
  statement {
    sid       = "RunMemberWboxInstanceOnly"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_wbox_instance_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_wbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_wbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_wbox_tag_keys.instance
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceType"
      values   = local.member_wbox_types
    }

    # NotEquals, as gpu-box.tf: holds whether or not EC2 fills ec2:Tenancy in, and still
    # refuses a dedicated or host override.
    condition {
      test     = "StringNotEquals"
      variable = "ec2:Tenancy"
      values   = ["dedicated", "host"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:MetadataHttpTokens"
      values   = ["required"]
    }

    condition {
      test     = "ArnEquals"
      variable = "ec2:InstanceProfile"
      values   = local.member_wbox_profile_arns
    }

    # Spot only: the template asks for spot, and this refuses a launch that overrides the
    # market (any other costs several times as much). ec2:InstanceMarketType is a RunInstances
    # condition key on the instance resource (Service Authorization Reference).
    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceMarketType"
      values   = ["spot"]
    }
  }

  # The root volume: the template's shape, so a launch-time block-device override cannot add
  # a huge disk.
  statement {
    sid       = "RunMemberWboxRootVolume"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_wbox_volume_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_wbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_wbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_wbox_tag_keys.volume
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:VolumeType"
      values   = ["gp3"]
    }

    condition {
      test     = "NumericLessThanEquals"
      variable = "ec2:VolumeSize"
      values   = [tostring(local.member_wbox_root_size)]
    }

    condition {
      test     = "Bool"
      variable = "ec2:Encrypted"
      values   = ["true"]
    }
  }

  statement {
    sid       = "RunMemberWboxEni"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_wbox_eni_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_wbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_wbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_wbox_tag_keys.eni
    }

    # The ENI must be the template's own: a --network-interfaces override (another address, the
    # fixed one dropped) makes this false. AWS's example policies use exactly this key on
    # network-interface/* to forbid overriding the template's network interfaces.
    condition {
      test     = "Bool"
      variable = "ec2:IsLaunchTemplateResource"
      values   = ["true"]
    }
  }

  # What the call references, pinned to exact ARNs: this AMI, this subnet, this security
  # group, these templates. No key pair: the password comes from Secrets Manager.
  statement {
    sid     = "RunMemberWboxReferencedResources"
    actions = ["ec2:RunInstances"]
    resources = concat(
      [
        "arn:aws:ec2:${local.member_wbox_region}::image/${nonsensitive(data.aws_ssm_parameter.member_wbox_ami.value)}",
        "arn:aws:ec2:${local.member_wbox_region}:${local.member_wbox_account}:subnet/${aws_subnet.member_wbox.id}",
        "arn:aws:ec2:${local.member_wbox_region}:${local.member_wbox_account}:security-group/${aws_security_group.member_wbox.id}",
      ],
      local.member_wbox_template_arns,
    )

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_wbox_template_arns
    }
  }

  # Tagging at launch only; ec2:CreateAction keeps it from retagging anything that exists.
  statement {
    sid       = "TagMemberWboxAtLaunchOnly"
    actions   = ["ec2:CreateTags"]
    resources = [local.member_wbox_instance_arn, local.member_wbox_volume_arn, local.member_wbox_eni_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  # Down. Terminate only: a one-time spot instance cannot be stopped, and C: is disposable.
  statement {
    sid       = "TerminateTaggedMemberWboxOnly"
    actions   = ["ec2:TerminateInstances"]
    resources = [local.member_wbox_instance_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = local.member_wbox_names
    }
  }

  # Attaching a member's D: disk: the volumes by identity (never by a tag, which a
  # DevEBS-style delete grant could also match) to an instance tagged as a member wbox. Terminating
  # the instance detaches it, so there is no DetachVolume.
  statement {
    sid       = "AttachMemberWboxDataVolume"
    actions   = ["ec2:AttachVolume"]
    resources = local.member_wbox_data_arns
  }

  statement {
    sid       = "AttachToTaggedMemberWboxOnly"
    actions   = ["ec2:AttachVolume"]
    resources = [local.member_wbox_instance_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = local.member_wbox_names
    }
  }

  # PassRole is the escalation: one role, only to EC2.
  statement {
    sid       = "PassMemberWboxRole"
    actions   = ["iam:PassRole"]
    resources = local.member_wbox_role_arns

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  statement {
    sid       = "ReadMemberWboxPasswords"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.member_wbox_secret_arns
  }

  # Read-only lookups wbox makes for a member (find their box and disk, show the spot price). Not
  # scopable to resources; pinned to the one region.
  statement {
    sid       = "DescribeForMemberWbox"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSpotPriceHistory"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.member_wbox_region]
    }
  }
}

# A MANAGED policy, not inline: a role's inline policies share one 10,240-character total
# (parallel-box-launch.tf), and a managed policy has its own 6,144.
resource "aws_iam_policy" "member_wbox_control" {
  name        = "member-wbox-control"
  description = "nextjs-dev: launch, attach the D: disk of, and terminate the tagged Windows playtest spot boxes (member-wbox.tf), and read their passwords"
  policy      = data.aws_iam_policy_document.member_wbox_control.json
}

# nextjs-dev-role only: the members work on nextjs-dev and nextjs-ct. The owner's metal boxes
# have no business launching a member's box.
resource "aws_iam_role_policy_attachment" "member_wbox_control" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.member_wbox_control.arn
}

# ============================================================ idle watchdog
# The parallel boxes' watchdog function (parallel-box-watchdog.tf), here in us-west-1 next to
# the boxes (CloudWatch metrics and TerminateInstances are regional), with Windows settings:
# one hour below 15% CPU, and the hard lifetime. Its own role, which can terminate the
# members' wboxes and nothing else.
resource "aws_iam_role" "member_wbox_watchdog" {
  name = "member-wbox-watchdog"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "member-wbox-watchdog" }
}

resource "aws_iam_role_policy_attachment" "member_wbox_watchdog_basic" {
  role       = aws_iam_role.member_wbox_watchdog.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "member_wbox_watchdog" {
  name = "member-wbox-watchdog"
  role = aws_iam_role.member_wbox_watchdog.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "cloudwatch:GetMetricStatistics"]
        Resource = "*"
      },
      {
        # The members' box names exactly, so nothing else that shares the prefix can match.
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = local.member_wbox_instance_arn
        Condition = { StringEquals = { "ec2:ResourceTag/Name" = local.member_wbox_names } }
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
    ]
  })
}

resource "aws_lambda_function" "member_wbox_watchdog" {
  function_name    = "member-wbox-watchdog"
  role             = aws_iam_role.member_wbox_watchdog.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  filename         = data.archive_file.parallel_watchdog.output_path
  source_code_hash = data.archive_file.parallel_watchdog.output_base64sha256

  environment {
    variables = {
      IDLE_MINUTES    = tostring(local.member_wbox_idle_minutes)
      IDLE_CPU_PCT    = tostring(local.member_wbox_idle_cpu_pct)
      TAG_NAMES       = join(",", local.member_wbox_names)
      MAX_AGE_MINUTES = jsonencode({ for n in local.member_wbox_names : n => local.member_box_max_hours * 60 })
      SINGLE_NAMES    = join(",", local.member_wbox_names) # one instance per member wbox
      SNS_REGION      = var.aws_region
      SNS_TOPIC_ARN   = aws_sns_topic.cost_alerts.arn
    }
  }

  tags = { Name = "member-wbox-watchdog" }
}

resource "aws_cloudwatch_event_rule" "member_wbox_watchdog" {
  name                = "member-wbox-watchdog"
  description         = "Terminate a Windows playtest spot box after an idle hour or at its lifetime"
  schedule_expression = "rate(5 minutes)"
  tags                = { Name = "member-wbox-watchdog" }
}

resource "aws_cloudwatch_event_target" "member_wbox_watchdog" {
  rule      = aws_cloudwatch_event_rule.member_wbox_watchdog.name
  target_id = "member-wbox-watchdog"
  arn       = aws_lambda_function.member_wbox_watchdog.arn
}

resource "aws_lambda_permission" "member_wbox_watchdog" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.member_wbox_watchdog.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.member_wbox_watchdog.arn
}

# The watchdog must not be the thing that fails silently: a spot box it stops watching runs
# until the box's own deadline task ends it. It errors, or it stops being invoked at all (a
# missing datapoint is breaching here, on purpose), as auto-reboot.tf's alarms.
resource "aws_cloudwatch_metric_alarm" "member_wbox_watchdog_errors" {
  alarm_name          = "member-wbox-watchdog-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The member wbox watchdog Lambda raised an error: an idle or expired Windows playtest spot box may not be terminated (member-wbox.tf)."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.member_wbox_watchdog.function_name }
}

resource "aws_cloudwatch_metric_alarm" "member_wbox_watchdog_not_running" {
  alarm_name          = "member-wbox-watchdog-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The member wbox watchdog Lambda has not run for 15 minutes (it runs every 5): nothing ends an idle Windows playtest spot box but its own ${local.member_box_max_hours}-hour deadline (member-wbox.tf)."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "breaching"
  dimensions          = { FunctionName = aws_lambda_function.member_wbox_watchdog.function_name }
}

output "member_wbox_launch_templates" {
  description = "Launch template per member wbox (terraform owns the config; wbox up picks the instance type)"
  value       = { for k, lt in aws_launch_template.member_wbox : k => lt.name }
}

output "member_wbox_data_volumes" {
  description = "Persistent D: disk per member wbox (survives every instance), in us-west-1a"
  value       = { for k, v in aws_ebs_volume.member_wbox_data : k => v.id }
}
