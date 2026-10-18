# dolphin-vercel-retired.tf
#
# The Dolphin sites are off Vercel (owner, 2026-10-18): gamesworthwatching.com and yourfantasymovie.com are served by the
# Workers dolphin-labs-prod and dolphin-films-prod (gamesworthwatching-zone.tf, yourfantasymovie.tf), the three projects of
# the dolphin-labs Vercel team (dolphin-labs, dolphin-films, dolphin-maps) are disconnected from GitHub, and the team is
# being DELETED. Nothing in this repository writes to that team any more: vercel-env.tf no longer manages the dolphin-labs
# project and dolphin-films-vercel.tf is gone. This file makes Terraform FORGET what they managed in Vercel, without
# calling Vercel, and DESTROY the AWS secret that held the team's token (below), as any AWS change goes through Terraform.
#
# WHY FORGET AND NOT DESTROY (the Vercel objects). Deleting the team deletes its projects and every variable in them, so a Terraform destroy
# would only do early, one variable at a time, what the deletion does anyway, and it needs the team's token to work: once
# the team is gone that token is dead and a destroy plan fails on every variable. A forget makes no API call, so this plan
# is the same before and after the deletion. Checked with this repository's pins (Terraform 1.10.3, vercel/vercel
# 5.17.1) against a state holding a vercel_project_environment_variable under an aliased provider whose token Vercel
# refuses: a `removed` block with `destroy = false` plans the forget and never refreshes the object.
#
# WHY THE PROVIDER BLOCK STAYS. Terraform refuses to plan any object in state whose provider configuration has been
# removed ("Provider configuration not present"), a forget included (checked the same way). So the alias stays until the
# apply has forgotten everything, with a constant placeholder token: nothing reads the old token's secret any more, and
# the provider only sends a token when it calls the API, which a forget never does. The Vercel provider refuses to
# configure without a token or with one not shaped like a Vercel token, hence a placeholder of that shape.
#
# AFTER THE APPLY (an empty follow-up plan shows nothing left to forget or destroy), delete this whole file in a follow-up
# PR: with nothing in state under vercel.dolphin_labs or at the removed addresses, none of these blocks does anything.

provider "vercel" {
  alias     = "dolphin_labs"
  api_token = "unsetunsetunsetunsetunse"
}

# The dolphin-labs project's variables (team dolphin-labs), formerly in vercel-env.tf.
removed {
  from = vercel_project_environment_variable.vercel_env_dolphin_labs

  lifecycle {
    destroy = false
  }
}

# The dolphin-films project's Production and Preview variables, formerly in dolphin-films-vercel.tf.
removed {
  from = vercel_project_environment_variable.dolphin_films

  lifecycle {
    destroy = false
  }
}

# The container of the dolphin-labs team's Vercel API token (`vercel-api-token-dolphin-labs`) and its policy are AWS
# resources, so Terraform DESTROYS them rather than leaving a hand deletion. Nothing reads the secret any more (the
# provider above holds a placeholder). The secret is deleted with the recovery window it was created with (30 days,
# recorded in state), so it can be restored in that window; the token inside dies with the team anyway. The removed
# blocks drop the old prevent_destroy guard together with the resource blocks. Terraform removes the policy first, then
# schedules the secret's deletion, so the secret is without its deny statement only for the seconds between the two calls.
removed {
  from = aws_secretsmanager_secret_policy.dolphin_films_vercel_api_token

  lifecycle {
    destroy = true
  }
}

removed {
  from = aws_secretsmanager_secret.dolphin_films_vercel_api_token

  lifecycle {
    destroy = true
  }
}
