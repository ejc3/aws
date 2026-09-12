# No recurring service subscription: four regional account defaults, not hosts.
# Existing disk migration, public SSH/ET, Cloudflare and backup resources are unchanged.
# Account defaults are not a substitute for instance-scoped IAM/metadata controls.

module "security_defaults_main_ap_northeast_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_ap_northeast_1 }
}

module "security_defaults_main_ap_northeast_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_ap_northeast_2 }
}

module "security_defaults_main_ap_northeast_3" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_ap_northeast_3 }
}

module "security_defaults_main_ap_south_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_ap_south_1 }
}

module "security_defaults_main_ap_southeast_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_ap_southeast_1 }
}

module "security_defaults_main_ap_southeast_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_ap_southeast_2 }
}

module "security_defaults_main_ca_central_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_ca_central_1 }
}

module "security_defaults_main_eu_central_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_eu_central_1 }
}

module "security_defaults_main_eu_north_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_eu_north_1 }
}

module "security_defaults_main_eu_west_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_eu_west_1 }
}

module "security_defaults_main_eu_west_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_eu_west_2 }
}

module "security_defaults_main_eu_west_3" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_eu_west_3 }
}

module "security_defaults_main_sa_east_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_sa_east_1 }
}

module "security_defaults_main_us_east_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dr }
}

module "security_defaults_main_us_east_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_main_us_east_2 }
}

module "security_defaults_main_us_west_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws }
}

module "security_defaults_main_us_west_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.west2 }
}

module "security_defaults_staging_ap_northeast_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_ap_northeast_1 }
}

module "security_defaults_staging_ap_northeast_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_ap_northeast_2 }
}

module "security_defaults_staging_ap_northeast_3" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_ap_northeast_3 }
}

module "security_defaults_staging_ap_south_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_ap_south_1 }
}

module "security_defaults_staging_ap_southeast_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_ap_southeast_1 }
}

module "security_defaults_staging_ap_southeast_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_ap_southeast_2 }
}

module "security_defaults_staging_ca_central_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_ca_central_1 }
}

module "security_defaults_staging_eu_central_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_eu_central_1 }
}

module "security_defaults_staging_eu_north_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_eu_north_1 }
}

module "security_defaults_staging_eu_west_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_eu_west_1 }
}

module "security_defaults_staging_eu_west_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_eu_west_2 }
}

module "security_defaults_staging_eu_west_3" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_eu_west_3 }
}

module "security_defaults_staging_sa_east_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_sa_east_1 }
}

module "security_defaults_staging_us_east_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.staging_dr }
}

module "security_defaults_staging_us_east_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_us_east_2 }
}

module "security_defaults_staging_us_west_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.staging }
}

module "security_defaults_staging_us_west_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.security_staging_us_west_2 }
}
