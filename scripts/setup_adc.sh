#!/usr/bin/env bash
# Configure local Application Default Credentials for Gemini on Google Cloud.
set -euo pipefail

if ! command -v gcloud >/dev/null 2>&1; then
  echo "Install the Google Cloud CLI and add gcloud to PATH: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi
adc_project="${1:-}"
if [[ -z "$adc_project" ]]; then
  read -r -p 'Google Cloud project ID: ' adc_project
fi
if [[ ! "$adc_project" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  echo "Invalid Google Cloud project ID." >&2
  exit 1
fi

gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project "$adc_project"
echo "ADC configured. Quota project: $adc_project"
echo "Set GOOGLE_CLOUD_PROJECT=$adc_project in .env."
echo "The project needs billing, the Vertex AI API, and appropriate account permissions."
