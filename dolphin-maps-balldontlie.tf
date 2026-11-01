# Ball Don't Lie (api.balldontlie.io: sports schedules) for dolphin-labs-hq/dolphin-maps' games loop
# (.github/workflows/refresh-games.yml, pipeline/src/fetch-games.ts there): each Bay Area arena's home games for the next 30
# days, so a trip to Chase Center on a game night says "Warriors vs Heat · 7:00 PM". The key is games-worth-watching's
# (the dolphin-labs repository's Actions secret BALLDONTLIE_API_KEY; the news pass's copy in the Dolphin account), shared:
# the loop spends one call per league a day (four), 20 s apart, and reads the x-ratelimit-* headers.
#
#   balldontlie/api-key   JSON: BALLDONTLIE_API_KEY. Terraform owns the container and who may read it; the value is set out
#                         of band (put-secret-value from a 0600 file, copied from games-worth-watching's key) and never
#                         enters git. Readers: the administration set, dev-server-role (the metal boxes: recording
#                         fixtures, a run by hand), which may also put it, as with dolphin-maps/pems, and the loop
#                         Lambdas' role (dolphin-maps-loop.tf: the games pass, {"mode": "games"}).
#
# Once it has a value, set local.dolphin_maps_balldontlie_key_set to true: the key then becomes the BALLDONTLIE_API_KEY of
# dolphin-maps' data-push environment (main only; the loops' environment, which holds DATA_APP_KEY and the PeMS login).
# While false, nothing reads the value, so a plan never fails on a container with no version yet.
locals {
  dolphin_maps_balldontlie_secret  = "balldontlie/api-key"
  dolphin_maps_balldontlie_key_set = false
}

resource "aws_secretsmanager_secret" "balldontlie" {
  name                    = local.dolphin_maps_balldontlie_secret
  description             = "Ball Don't Lie API key, games-worth-watching's, shared with dolphin-maps' games loop (JSON: BALLDONTLIE_API_KEY). Value set out of band; see dolphin-maps-balldontlie.tf."
  recovery_window_in_days = 7
  tags                    = { Name = local.dolphin_maps_balldontlie_secret, Project = "dolphin-maps" }
}

resource "aws_secretsmanager_secret_policy" "balldontlie" {
  secret_arn = aws_secretsmanager_secret.balldontlie.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationTheMetalBoxesAndTheLoopCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.balldontlie.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn, aws_iam_role.dolphin_maps_loop.arn]) } }
    }]
  })
}

data "aws_secretsmanager_secret_version" "balldontlie" {
  count     = local.dolphin_maps_balldontlie_key_set ? 1 : 0
  secret_id = aws_secretsmanager_secret.balldontlie.id
}

resource "github_actions_environment_secret" "dolphin_maps_balldontlie" {
  count           = local.dolphin_maps_balldontlie_key_set ? 1 : 0
  provider        = github.dolphin_labs_ci
  repository      = "dolphin-maps"
  environment     = "data-push"
  secret_name     = "BALLDONTLIE_API_KEY"
  plaintext_value = jsondecode(data.aws_secretsmanager_secret_version.balldontlie[0].secret_string)["BALLDONTLIE_API_KEY"]
}
