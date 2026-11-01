# State lives in the bucket this stack creates. The bucket name contains the project ID, so it is
# passed at init time instead of committed:
#   terraform init -backend-config="bucket=$TF_STATE_BUCKET"
# First run only: a gitignored backend_override.tf switches to local state until the bucket
# exists (see infra/BOOTSTRAP.md, section 5).
terraform {
  backend "gcs" {
    prefix = "bootstrap"
  }
}
