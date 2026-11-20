# fcvm-fleet-prod-broadcast.tf
#
# The prod wall's broadcast: staging's (fcvm-fleet-broadcast.tf, whose notes apply here) with its own IVS channel in
# us-east-1, its own playback key pair and its own secret, fcvm-fleet/prod/broadcast, which only the prod coordinator's
# task role reads (fcvm-fleet-prod.tf, FleetSecrets). Behind var.fcvm_fleet_prod_enabled and staging's
# local.fcvm_fleet_broadcast. Public viewers past the direct capacity watch it with a playback token the prod coordinator
# signs per /live connection once the connection has passed Turnstile.
#
# Cost while on: $2.00 an hour while the channel is live (only while the prod fleet is awake), and the viewer-hours.

locals {
  fcvm_fleet_prod_broadcast = var.fcvm_fleet_prod_enabled && local.fcvm_fleet_broadcast ? 1 : 0
}

resource "aws_ivs_channel" "fcvm_fleet_prod_broadcast" {
  count        = local.fcvm_fleet_prod_broadcast
  provider     = aws.fcvm_fleet_east1
  name         = "${local.fcvm_fleet_prod_stack.prefix}-broadcast"
  latency_mode = "LOW"
  type         = "STANDARD"
  authorized   = true
  tags         = { Project = "fcvm-fleet", Purpose = "fcvm-fleet-broadcast", Fleet = local.fcvm_fleet_prod_stack.fleet }
}

data "aws_ivs_stream_key" "fcvm_fleet_prod_broadcast" {
  count       = local.fcvm_fleet_prod_broadcast
  provider    = aws.fcvm_fleet_east1
  channel_arn = aws_ivs_channel.fcvm_fleet_prod_broadcast[0].arn
}

resource "tls_private_key" "fcvm_fleet_prod_broadcast_playback" {
  count       = local.fcvm_fleet_prod_broadcast
  algorithm   = "ECDSA"
  ecdsa_curve = "P384"
}

resource "aws_ivs_playback_key_pair" "fcvm_fleet_prod_broadcast" {
  count      = local.fcvm_fleet_prod_broadcast
  provider   = aws.fcvm_fleet_east1
  name       = "${local.fcvm_fleet_prod_stack.prefix}-broadcast"
  public_key = tls_private_key.fcvm_fleet_prod_broadcast_playback[0].public_key_pem
  tags       = { Project = "fcvm-fleet", Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_secretsmanager_secret" "fcvm_fleet_prod_broadcast" {
  count                   = local.fcvm_fleet_prod_broadcast
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/prod/broadcast"
  description             = "The prod wall's IVS broadcast: ingest URL with stream key, playback URL, channel ARN, playback private key (JSON). Written by fcvm-fleet-prod-broadcast.tf."
  recovery_window_in_days = 7
  tags                    = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet/prod/broadcast", Project = "fcvm-fleet" })
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_prod_broadcast" {
  count     = local.fcvm_fleet_prod_broadcast
  provider  = aws.fcvm_fleet
  secret_id = aws_secretsmanager_secret.fcvm_fleet_prod_broadcast[0].id
  # ejc3/fcvm src/fleet/coord/broadcast.rs, BroadcastSecret.
  secret_string = jsonencode({
    ingest_url       = "rtmps://${aws_ivs_channel.fcvm_fleet_prod_broadcast[0].ingest_endpoint}:443/app/${data.aws_ivs_stream_key.fcvm_fleet_prod_broadcast[0].value}"
    playback_url     = aws_ivs_channel.fcvm_fleet_prod_broadcast[0].playback_url
    channel_arn      = aws_ivs_channel.fcvm_fleet_prod_broadcast[0].arn
    playback_key_pem = tls_private_key.fcvm_fleet_prod_broadcast_playback[0].private_key_pem_pkcs8
  })
}
