# GitHub settings as code (#21): what protects `main` and the `prod` deploy gate. Applied locally
# with your own GitHub login, like the rest of this stack, so CI never holds a repo-admin token.
# Existing settings are imported as they are; the only intended change is closing the admin bypass
# on `prod` (see below).

locals {
  repository = element(split("/", var.github_repository), 1)

  # Checks a PR must pass, each pinned to the GitHub Actions app (15368), so a status with the same
  # name posted by any other app or token doesn't count.
  required_checks = [
    "checks", "image", "pr-title", "terraform",
    "tf-plan", "tf-apply-gate", "tf-scan",
    "codeql (python)", "codeql (actions)",
  ]
  github_actions_app_id = 15368
}

# Whoever applies this stack (the owner, with `gh auth token`) is the `prod` approver.
data "github_user" "approver" {
  username = ""
}

import {
  to = github_repository.this
  id = local.repository
}

resource "github_repository" "this" {
  name        = local.repository
  description = local.repository
  visibility  = "public" # portfolio repo; private would also switch off free CodeQL and secret scanning

  has_issues      = true
  has_projects    = true
  has_wiki        = true
  has_discussions = false

  # Squash merges only, titled by the PR (the `pr-title` check enforces conventional commits).
  allow_merge_commit          = false
  allow_rebase_merge          = false
  allow_squash_merge          = true
  allow_auto_merge            = false
  allow_update_branch         = false
  squash_merge_commit_title   = "PR_TITLE"
  squash_merge_commit_message = "PR_BODY"
  delete_branch_on_merge      = true
  web_commit_signoff_required = false

  security_and_analysis {
    secret_scanning {
      status = "enabled"
    }
    secret_scanning_push_protection {
      status = "enabled"
    }
  }

  archive_on_destroy = true # never delete the repository from Terraform

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = github_repository_vulnerability_alerts.this
  id = local.repository
}

resource "github_repository_vulnerability_alerts" "this" {
  repository = github_repository.this.name
  enabled    = true
}

import {
  to = github_repository_dependabot_security_updates.this
  id = local.repository
}

resource "github_repository_dependabot_security_updates" "this" {
  repository = github_repository.this.name
  enabled    = true
}

# Protection for `main` as a ruleset: PRs only, squash only, linear history, no force pushes or
# deletion, and the required checks pinned to GitHub Actions. No bypass actors, so it binds admins
# too. The classic branch protection stays in place for now (both apply); it's removed once this
# ruleset has guarded a few PRs.
resource "github_repository_ruleset" "main" {
  name        = "main"
  repository  = github_repository.this.name
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  rules {
    deletion                = true
    non_fast_forward        = true
    required_linear_history = true

    pull_request {
      allowed_merge_methods             = ["squash"]
      required_approving_review_count   = 0 # one maintainer: CI and the prod approval are the gates
      dismiss_stale_reviews_on_push     = true
      required_review_thread_resolution = true
    }

    required_status_checks {
      strict_required_status_checks_policy = true

      dynamic "required_check" {
        for_each = local.required_checks
        content {
          context        = required_check.value
          integration_id = local.github_actions_app_id
        }
      }
    }
  }
}

import {
  to = github_repository_environment.prod
  id = "${local.repository}:prod"
}

# The deploy gate. Google trusts tf-apply only for jobs in this environment on main, and GitHub only
# runs such a job after this approval.
resource "github_repository_environment" "prod" {
  repository  = github_repository.this.name
  environment = "prod"

  # The one intended change: GitHub's default lets admins skip the review. The only reviewer is the
  # only admin, so with it on, a prod deploy could run without anyone approving it.
  can_admins_bypass   = false
  prevent_self_review = false # a single maintainer approves their own merges

  reviewers {
    users = [data.github_user.approver.id]
  }

  deployment_branch_policy {
    protected_branches     = true
    custom_branch_policies = false
  }
}
