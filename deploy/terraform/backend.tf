# Remote state in Cloud Storage (bucket created outside this config: versioned, uniform access,
# public access prevention). Terraform locks the state with a lock object in the bucket.
# The bucket is not set here. Pass it at init time:
#   terraform -chdir=deploy/terraform init -backend-config="bucket=<your-state-bucket>" -backend-config="prefix=cene"
terraform {
  backend "gcs" {}
}
