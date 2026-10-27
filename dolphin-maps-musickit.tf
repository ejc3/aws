# dolphin-maps-musickit.tf
#
# Apple Music for dolphin-labs-hq/dolphin-maps (its docs/design/music.md, stage 7): the car screen plays the person's
# Apple Music through MusicKit JS, which needs a developer token, a short-lived ES256 JWT signed with a MusicKit private
# key. The site's Worker mints those (GET /api/music/apple/token, for a paired car screen or a signed-in account), so
# the Worker holds the key; nothing else does.
#
#   dolphin-maps/musickit   JSON: MUSICKIT_KEY_ID, MUSICKIT_TEAM_ID, MUSICKIT_PRIVATE_KEY (the .p8's PEM text).
#                           The key "Dolphin Maps MusicKit" of the owner's Apple Developer team (Media Services, for the
#                           Media ID media.dev.dolphinlabs.maps). Apple lets a .p8 be downloaded once; this is its record.
#
# Terraform owns the container and who reaches it, never a value (none is in state). The value is put out of band:
# from a metal box (dev-server-role may put it and describe it, never read it back), or by an administrator. Only
# administration reads it: the loaders scripts/workers-prod-secrets.sh and scripts/workers-stage-secrets.sh, run with
# `dolphin-maps`, read it with the caller's own credentials (this account, not the Dolphin account where the site's other
# containers are) and merge its three names into dolphin-maps-prod and dolphin-maps-stage. While it has no value they load
# without it, and the site keeps Apple Music off on the car screen.
#
# One key for both Workers, by the owner's choice: a developer token identifies the app to Apple, not a person, and
# carries no one's data. Revoking it in the developer portal stops both; a second MusicKit key can be made for a
# rotation (Apple allows two per Media ID), put here, loaded, and the first revoked.
#
# Putting the value (from a 0600 file, never on a command line):
#
#   jq -n --rawfile k AuthKey_<KEY_ID>.p8 --arg id <KEY_ID> --arg team <TEAM_ID> \
#     '{MUSICKIT_KEY_ID: $id, MUSICKIT_TEAM_ID: $team, MUSICKIT_PRIVATE_KEY: $k}' |
#   aws secretsmanager put-secret-value --region us-west-1 --secret-id dolphin-maps/musickit --secret-string file:///dev/stdin
#
# then, after the site's own deploy has finished, scripts/workers-stage-secrets.sh dolphin-maps and
# scripts/workers-prod-secrets.sh dolphin-maps.

resource "aws_secretsmanager_secret" "dolphin_maps_musickit" {
  name                    = "dolphin-maps/musickit"
  description             = "dolphin-maps' MusicKit key for Apple Music developer tokens (JSON: MUSICKIT_KEY_ID, MUSICKIT_TEAM_ID, MUSICKIT_PRIVATE_KEY). Value set out of band; loaded into dolphin-maps-prod and -stage. See dolphin-maps-musickit.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "dolphin-maps/musickit", Project = "dolphin-maps" }
}

resource "aws_secretsmanager_secret_policy" "dolphin_maps_musickit" {
  secret_arn = aws_secretsmanager_secret.dolphin_maps_musickit.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.dolphin_maps_musickit.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

# The metal boxes put it (the key is made on the developer portal from a dev box's session) and see that it has a value,
# and nothing more: PutSecretValue and DescribeSecret on this one secret, never GetSecretValue. One managed policy, not
# an inline one: dev-server-role's inline policies are at IAM's 10,240-character total (#574).
data "aws_iam_policy_document" "dolphin_maps_musickit_dev" {
  statement {
    sid       = "PutTheMusicKitKey"
    actions   = ["secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.dolphin_maps_musickit.arn]
  }
}

resource "aws_iam_policy" "dolphin_maps_musickit_dev" {
  name        = "dev-dolphin-maps-musickit"
  description = "Metal dev boxes: put (never read) dolphin-maps' MusicKit key (dolphin-maps/musickit)"
  policy      = data.aws_iam_policy_document.dolphin_maps_musickit_dev.json
}

resource "aws_iam_role_policy_attachment" "dolphin_maps_musickit_dev" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.dolphin_maps_musickit_dev.arn
}
