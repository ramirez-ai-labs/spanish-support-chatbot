# Same state bucket as the bootstrap stack, separate prefix. The bucket name contains the project
# ID, so it is passed at init time: terraform init -backend-config="bucket=$TF_STATE_BUCKET"
terraform {
  backend "gcs" {
    prefix = "gcp"
  }
}
