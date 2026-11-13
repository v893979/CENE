# CENE end-to-end platform - Stage 1 on Google Cloud.
# Creates: APIs, Artifact Registry, service accounts, three KMS signing keys (token, oracle, audit),
# BigQuery dataset + control tables, write-once audit bucket, optional Memorystore Redis,
# and three Cloud Run services (control, gateway, tools). Nothing is public: callers need run.invoker.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = ">= 6.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  apis = ["run.googleapis.com", "artifactregistry.googleapis.com", "cloudkms.googleapis.com",
    "bigquery.googleapis.com", "storage.googleapis.com", "iam.googleapis.com",
  "cloudbuild.googleapis.com", "redis.googleapis.com", "vpcaccess.googleapis.com", "compute.googleapis.com",
  "modelarmor.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com"]
  image   = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}/cene:${var.image_tag}"
  key_ver = { for k, v in google_kms_crypto_key.keys : k => "${v.id}/cryptoKeyVersions/1" }
  common_env = {
    CENE_MODE          = "gcp"
    CENE_DATASET       = google_bigquery_dataset.cene.dataset_id
    CENE_CONTRACTS_DIR = "harness/contracts"
    CENE_TOKEN_KEY     = local.key_ver["token"]
    CENE_ORACLE_KEY    = local.key_ver["oracle"]
    CENE_AUDIT_KEY     = local.key_ver["audit"]
    CENE_AUDIT_BUCKET  = google_storage_bucket.audit.name
    CENE_TOKEN_TTL     = "900"
    CENE_REDIS_URL     = var.enable_redis ? "redis://${google_redis_instance.state[0].host}:6379/0" : ""
    CENE_SCREENER             = var.model_armor_template == "" ? "none" : "model_armor"
    CENE_MODEL_ARMOR_TEMPLATE = var.model_armor_template
  }
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- container registry
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "cene"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# ---------------------------------------------------------------- identities
resource "google_service_account" "sa" {
  for_each     = toset(["control", "gateway", "tools", "agent"])
  account_id   = "cene-${each.value}"
  display_name = "CENE ${each.value}"
}

# ---------------------------------------------------------------- signing keys (KMS / HSM)
resource "google_kms_key_ring" "ring" {
  name       = "cene"
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "keys" {
  for_each = toset(["token", "oracle", "audit"])
  name     = each.value
  key_ring = google_kms_key_ring.ring.id
  purpose  = "ASYMMETRIC_SIGN"
  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = var.kms_protection_level # SOFTWARE for development, HSM for Cloud HSM
  }
  lifecycle { prevent_destroy = false }
}

# control plane signs tokens; gateway and control only *read* public keys of the others
resource "google_kms_crypto_key_iam_member" "control_sign_token" {
  crypto_key_id = google_kms_crypto_key.keys["token"].id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.sa["control"].email}"
}
resource "google_kms_crypto_key_iam_member" "gateway_read_token" {
  crypto_key_id = google_kms_crypto_key.keys["token"].id
  role          = "roles/cloudkms.publicKeyViewer"
  member        = "serviceAccount:${google_service_account.sa["gateway"].email}"
}
resource "google_kms_crypto_key_iam_member" "read_oracle" {
  for_each      = toset(["control", "gateway"])
  crypto_key_id = google_kms_crypto_key.keys["oracle"].id
  role          = "roles/cloudkms.publicKeyViewer"
  member        = "serviceAccount:${google_service_account.sa[each.value].email}"
}
resource "google_kms_crypto_key_iam_member" "oracle_signers" {
  for_each      = toset(var.oracle_signers) # people allowed to declare emergency modes, e.g. "user:cro@example.com"
  crypto_key_id = google_kms_crypto_key.keys["oracle"].id
  role          = "roles/cloudkms.signerVerifier"
  member        = each.value
}
resource "google_kms_crypto_key_iam_member" "audit_sign" {
  for_each      = toset(["control", "gateway"])
  crypto_key_id = google_kms_crypto_key.keys["audit"].id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.sa[each.value].email}"
}

# ---------------------------------------------------------------- BigQuery
resource "google_bigquery_dataset" "cene" {
  dataset_id = var.dataset
  location                   = var.bq_location
  delete_contents_on_destroy = true
  depends_on                 = [google_project_service.apis]
}

resource "google_bigquery_table" "load_log" {
  dataset_id          = google_bigquery_dataset.cene.dataset_id
  table_id            = "_cene_load_log"
  deletion_protection = false
  schema = jsonencode([
    { name = "product", type = "STRING", mode = "REQUIRED" },
    { name = "load_commit_ts", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

resource "google_bigquery_table" "lineage" {
  dataset_id          = google_bigquery_dataset.cene.dataset_id
  table_id            = "_cene_lineage_health"
  deletion_protection = false
  schema = jsonencode([
    { name = "node", type = "STRING", mode = "REQUIRED" },
    { name = "health", type = "FLOAT64", mode = "REQUIRED" },
    { name = "updated", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

resource "google_bigquery_dataset_iam_member" "readers" {
  for_each   = toset(["control", "gateway", "tools"])
  dataset_id = google_bigquery_dataset.cene.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.sa[each.value].email}"
}
resource "google_project_iam_member" "bq_jobs" {
  for_each = toset(["control", "gateway", "tools"])
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.sa[each.value].email}"
}

# ---------------------------------------------------------------- write-once audit bucket
resource "google_storage_bucket" "audit" {
  name                        = "${var.project_id}-cene-audit"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = !var.lock_audit_retention
  retention_policy {
    retention_period = var.audit_retention_days * 86400
    is_locked        = var.lock_audit_retention # true is IRREVERSIBLE: objects cannot be deleted early
  }
}
resource "google_storage_bucket_iam_member" "audit_writers" {
  for_each = toset(["control", "gateway"])
  bucket   = google_storage_bucket.audit.name
  role     = "roles/storage.objectCreator"
  member   = "serviceAccount:${google_service_account.sa[each.value].email}"
}

# ---------------------------------------------------------------- optional shared state (Redis)
resource "google_vpc_access_connector" "conn" {
  count         = var.enable_redis ? 1 : 0
  name          = "cene-conn"
  region        = var.region
  network       = "default"
  ip_cidr_range = "10.8.0.0/28"
  depends_on    = [google_project_service.apis]
}
resource "google_redis_instance" "state" {
  count              = var.enable_redis ? 1 : 0
  name               = "cene-state"
  tier               = "BASIC"
  memory_size_gb     = 1
  region             = var.region
  authorized_network = data.google_compute_network.default.id
  depends_on         = [google_project_service.apis]
}

# ---------------------------------------------------------------- Cloud Run services
resource "google_cloud_run_v2_service" "svc" {
  for_each = {
    control = { max = var.enable_redis ? 3 : 1 } # modes are held in memory: keep 1 instance (see LLD 9)
    gateway = { max = var.enable_redis ? 5 : 1 } # escrow + state need Redis/Firestore to scale out
    tools   = { max = 2 }
  }
  name                = "cene-${each.key}"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # authentication still required (no allUsers invoker)
  deletion_protection = false
  template {
    service_account = google_service_account.sa[each.key].email
    scaling {
      min_instance_count = 0
      max_instance_count = each.value.max
    }
    dynamic "vpc_access" {
      for_each = var.enable_redis && each.key != "tools" ? [1] : []
      content {
        connector = google_vpc_access_connector.conn[0].id
        egress    = "PRIVATE_RANGES_ONLY"
      }
    }
    containers {
      image = local.image
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      env {
        name  = "CENE_SERVICE"
        value = each.key
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = each.key == "gateway" ? [1] : []
        content {
          name  = "CENE_TOOLS_URL"
          value = "https://cene-tools-${data.google_project.this.number}.${var.region}.run.app"
        }
      }
    }
  }
  depends_on = [google_project_service.apis]
}

data "google_project" "this" {}

data "google_compute_network" "default" {
  name = "default"
}

# gateway may call Model Armor when a template is configured
resource "google_project_iam_member" "gateway_model_armor" {
  count   = var.model_armor_template == "" ? 0 : 1
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${google_service_account.sa["gateway"].email}"
}

# who may call what
resource "google_cloud_run_v2_service_iam_member" "agent_calls" {
  for_each = toset(["control", "gateway"])
  name     = google_cloud_run_v2_service.svc[each.value].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa["agent"].email}"
}
resource "google_cloud_run_v2_service_iam_member" "gateway_calls_tools" {
  name     = google_cloud_run_v2_service.svc["tools"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa["gateway"].email}"
}
# ---------------------------------------------------------------- test identities
# A second human-like approver for two-person escrow tests (the requester may not approve).
resource "google_service_account" "approver" {
  account_id   = "cene-approver"
  display_name = "CENE test approver"
}
resource "google_cloud_run_v2_service_iam_member" "approver_calls_gateway" {
  name     = google_cloud_run_v2_service.svc["gateway"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.approver.email}"
}
# Testers who may act as the agent (token requester) and the test approver. Empty outside testing.
resource "google_service_account_iam_member" "test_impersonators" {
  for_each           = { for pair in setproduct(["agent", "approver"], var.test_impersonators) : "${pair[0]}-${pair[1]}" => pair }
  service_account_id = each.value[0] == "agent" ? google_service_account.sa["agent"].name : google_service_account.approver.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value[1]
}

resource "google_cloud_run_v2_service_iam_member" "operators" {
  for_each = { for pair in setproduct(["control", "gateway"], var.operators) : "${pair[0]}-${pair[1]}" => pair }
  name     = google_cloud_run_v2_service.svc[each.value[0]].name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.value[1]
}
