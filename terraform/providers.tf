terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    # The test VM is reached over SSH, not SSM. `tls` generates the keypair and
    # `local` writes the private half to disk for the checks to use. See the
    # "HOW THE CHECKS REACH THE TEST VM" note at the top of main.tf for why the
    # lab does not use Systems Manager.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
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
