terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Credentials come from the standard chain. track_scripts/setup-shell writes the
# Instruqt sandbox keys to /root/.aws/credentials before running terraform, so
# nothing is passed in here and no key ever appears in a .tf file or in state.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "instruqt-lab"            = var.track_slug
      "instruqt-participant-id" = var.participant_id
      "ManagedBy"               = "terraform"
    }
  }
}
