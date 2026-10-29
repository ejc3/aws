# fcvm-fleet-broadcast.tf
#
# The wall's broadcast (ejc3/fcvm docs/fleet/ARCHITECTURE.md, "Broadcast mode"): past the direct-mode capacity (500
# viewers, ejc3/fcvm fleet/renderer/README.md "Viewers per renderer"), viewers watch one composite of the whole wall that
# the coordinator encodes and pushes to an Amazon IVS low-latency channel. Measured 2026-10-27 (ejc3/fcvm#1384): 1.79 s
# glass to glass at the median, 0.21 s to the first frame.
#
# - One IVS channel in us-east-1 (IVS is not offered in us-east-2), STANDARD (IVS transcodes the 720p input into 720p,
#   480p, 360p and 160p), LOW latency, private: a playback request needs a token signed with the channel's playback key
#   pair. The coordinator holds the private key and signs a token per /live connection; /live is behind Cloudflare
#   Access on staging, so only Access-authenticated viewers get one.
# - The secret fcvm-fleet/broadcast (us-east-2, with the fleet's other secrets): the ingest URL with the stream key, the
#   playback URL, the channel ARN and the playback private key. Only the coordinator's task role reads it.
# - The coordinator's task grows to 2 vCPU and 4 GB for the decode and the 720p encode (1.69 cores measured on a Xeon),
#   and runs with --broadcast-listen, --broadcast-secret and --direct-capacity (fcvm-fleet.tf).
#
# Cost: $2.00 an hour while the channel is live (the coordinator pushes only while the fleet is awake, and the fleet
# sleeps after 10 minutes with no viewer), $0.072 per viewer-hour at 720p and $0.036 at 480p (North America, first
# 10,000 hours a month), and the larger task about $43 a month.
#
# The gate: local.fcvm_fleet_broadcast. Enabling requires admission-enforcing host and tile AMIs to be pinned and
# running (ejc3/aws#722), plus a coordinator image with --broadcast-listen and private ingest errors kept out of logs.

locals {
  fcvm_fleet_broadcast = true
  # The broadcast aggregator's private listener (layout and stats) inside the coordinator's task.
  fcvm_fleet_broadcast_port = 20310
  # Seats for direct streams; the rest watch the broadcast.
  fcvm_fleet_direct_capacity = 500
}

resource "aws_ivs_channel" "fcvm_fleet_broadcast" {
  count        = local.fcvm_fleet_broadcast ? 1 : 0
  provider     = aws.fcvm_fleet_east1
  name         = "fcvm-fleet-stage-broadcast"
  latency_mode = "LOW"
  type         = "STANDARD"
  authorized   = true
  tags         = { Project = "fcvm-fleet", Purpose = "fcvm-fleet-broadcast" }
}

# The channel's default stream key (IVS makes one with the channel).
data "aws_ivs_stream_key" "fcvm_fleet_broadcast" {
  count       = local.fcvm_fleet_broadcast ? 1 : 0
  provider    = aws.fcvm_fleet_east1
  channel_arn = aws_ivs_channel.fcvm_fleet_broadcast[0].arn
}

# The playback key pair: IVS keeps the public half; the private half signs viewers' tokens (ES384).
resource "tls_private_key" "fcvm_fleet_broadcast_playback" {
  count       = local.fcvm_fleet_broadcast ? 1 : 0
  algorithm   = "ECDSA"
  ecdsa_curve = "P384"
}

resource "aws_ivs_playback_key_pair" "fcvm_fleet_broadcast" {
  count      = local.fcvm_fleet_broadcast ? 1 : 0
  provider   = aws.fcvm_fleet_east1
  name       = "fcvm-fleet-stage-broadcast"
  public_key = tls_private_key.fcvm_fleet_broadcast_playback[0].public_key_pem
  tags       = { Project = "fcvm-fleet" }
}

resource "aws_secretsmanager_secret" "fcvm_fleet_broadcast" {
  count                   = local.fcvm_fleet_broadcast ? 1 : 0
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/broadcast"
  description             = "The wall's IVS broadcast: ingest URL with stream key, playback URL, channel ARN, playback private key (JSON). Written by fcvm-fleet-broadcast.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "fcvm-fleet/broadcast", Project = "fcvm-fleet" }
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_broadcast" {
  count     = local.fcvm_fleet_broadcast ? 1 : 0
  provider  = aws.fcvm_fleet
  secret_id = aws_secretsmanager_secret.fcvm_fleet_broadcast[0].id
  # ejc3/fcvm src/fleet/coord/broadcast.rs, BroadcastSecret.
  secret_string = jsonencode({
    ingest_url       = "rtmps://${aws_ivs_channel.fcvm_fleet_broadcast[0].ingest_endpoint}:443/app/${data.aws_ivs_stream_key.fcvm_fleet_broadcast[0].value}"
    playback_url     = aws_ivs_channel.fcvm_fleet_broadcast[0].playback_url
    channel_arn      = aws_ivs_channel.fcvm_fleet_broadcast[0].arn
    playback_key_pem = tls_private_key.fcvm_fleet_broadcast_playback[0].private_key_pem_pkcs8
  })
}

# The coordinator's inline policies already fill its aggregate 10 KiB budget. This
# managed policy grants only this secret read without changing those permissions.
resource "aws_iam_policy" "fcvm_fleet_coordinator_broadcast" {
  count    = local.fcvm_fleet_broadcast ? 1 : 0
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-coordinator-broadcast-secret"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "BroadcastSecret"
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.fcvm_fleet_broadcast[0].arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_coordinator_broadcast" {
  count      = local.fcvm_fleet_broadcast ? 1 : 0
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_coordinator_task.name
  policy_arn = aws_iam_policy.fcvm_fleet_coordinator_broadcast[0].arn
}
