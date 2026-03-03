# ConnectK CI/CD Guide

Complete reference for configuring and operating the CI/CD pipelines across the ConnectK
backend and frontend repositories.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Repository Layout](#2-repository-layout)
3. [Pipeline Flow Diagrams](#3-pipeline-flow-diagrams)
4. [GitHub Variables (vars.)](#4-github-variables-vars)
5. [GitHub Secrets](#5-github-secrets)
6. [Scenario Matrix — All Deployment Combinations](#6-scenario-matrix--all-deployment-combinations)
7. [Scenario 1 — GHCR + EKS Self-Hosted (Default)](#7-scenario-1--ghcr--eks-self-hosted-default)
8. [Scenario 2 — ECR + EKS (Self-Hosted or Managed)](#8-scenario-2--ecr--eks-self-hosted-or-managed)
9. [Scenario 3 — ACR + AKS (Self-Hosted or Managed)](#9-scenario-3--acr--aks-self-hosted-or-managed)
10. [Scenario 4 — GAR + GKE (Self-Hosted or Managed)](#10-scenario-4--gar--gke-self-hosted-or-managed)
11. [Scenario 5 — GHCR + Any Cloud](#11-scenario-5--ghcr--any-cloud)
12. [Scenario 6 — Multi-Cloud (Multiple Overlays)](#12-scenario-6--multi-cloud-multiple-overlays)
13. [ArgoCD Setup](#13-argocd-setup)
14. [Image Tagging Strategy](#14-image-tagging-strategy)
15. [Path Filters — What Triggers Each Pipeline](#15-path-filters--what-triggers-each-pipeline)
16. [The Update-Manifests Job — How GitOps Works](#16-the-update-manifests-job--how-gitops-works)
17. [Manual Pipeline Triggers](#17-manual-pipeline-triggers)
18. [Rollback Procedures](#18-rollback-procedures)
19. [Troubleshooting](#19-troubleshooting)
20. [Quick-Start Checklist](#20-quick-start-checklist)

---

## 1. Architecture Overview

```
┌──────────────┐     push     ┌──────────────────────┐     push image     ┌────────────┐
│   Developer  │────────────▶ │  GitHub Actions (CI)  │──────────────────▶ │  Container │
│              │              │                       │                    │  Registry  │
└──────────────┘              │  1. Lint / Type-check │                    │  (GHCR /   │
                              │  2. Build Docker      │                    │   ECR /    │
                              │  3. Push image        │                    │   ACR /    │
                              │  4. Update K8s        │                    │   GAR)     │
                              │     manifests         │                    └────────────┘
                              └───────┬───────────────┘
                                      │ git push (image tag)
                                      ▼
                              ┌──────────────────────┐     sync      ┌────────────────┐
                              │  connectk-backend    │◀──────────────│    ArgoCD       │
                              │  repo (k8s/ folder)  │               │  (watches repo) │
                              └──────────────────────┘               │                 │
                                                                     │  Deploys to K8s │
                                                                     └────────┬────────┘
                                                                              │
                                                                              ▼
                                                                     ┌────────────────┐
                                                                     │  Kubernetes    │
                                                                     │  Cluster       │
                                                                     │  (EKS/AKS/GKE)│
                                                                     └────────────────┘
```

**Key concepts:**

- **CI** is handled by GitHub Actions in each repo (backend and frontend).
- **CD** is handled by ArgoCD watching the `k8s/` folder in the **backend repo**.
- **GitOps bridge**: After building a Docker image, CI updates the image tag in the K8s
  overlay kustomization, commits, and pushes. ArgoCD detects the change and syncs.
- **K8s manifests live in the backend repo** — both backend and frontend manifests.

---

## 2. Repository Layout

### connectk-backend

```
connectk-backend/
├── .github/workflows/
│   └── ci-backend.yaml          ← Backend CI pipeline
├── app/                         ← FastAPI application code
├── alembic/                     ← Database migrations
├── requirements.txt
├── Dockerfile
└── k8s/                         ← ALL K8s manifests (backend + frontend)
    ├── argocd/
    │   ├── project.yaml         ← ArgoCD AppProject
    │   └── applicationset.yaml  ← ArgoCD ApplicationSet
    ├── base/                    ← Shared manifests
    ├── components/              ← Self-hosted / managed DB & Redis
    └── overlays/                ← Cloud-specific overlays
        ├── eks/
        ├── aks/
        └── gke/
```

### connectk-frontend

```
connectk-frontend/
├── .github/workflows/
│   └── ci-frontend.yaml         ← Frontend CI pipeline
├── app/                         ← Next.js pages / API routes
├── components/                  ← React components
├── hooks/                       ← Custom hooks
├── lib/                         ← Shared utilities
├── types/                       ← TypeScript types
├── public/                      ← Static assets
├── package.json
└── Dockerfile
```

---

## 3. Pipeline Flow Diagrams

### Backend Pipeline (`ci-backend.yaml`)

```
Push to main (matching paths)
         │
         ▼
    ┌─────────┐
    │  Lint    │  ← pip install, (ruff check, pytest — when enabled)
    └────┬────┘
         │ pass
         ▼
  ┌──────────────┐
  │ Build & Push │  ← Docker build, push to $REGISTRY
  │    Image     │     Tags: sha-XXXXXXX, main, latest
  └──────┬───────┘
         │ pass
         ▼
  ┌────────────────┐
  │ Update K8s     │  ← kustomize edit set image (same repo)
  │ Manifests      │     Commit with [skip ci]
  └────────────────┘     Only runs if vars.DEPLOY_OVERLAY is set
```

### Frontend Pipeline (`ci-frontend.yaml`)

```
Push to main (matching paths)
         │
         ▼
  ┌────────────────┐
  │ Lint &         │  ← npm ci, npm run lint, npm run type-check
  │ Type Check     │
  └──────┬─────────┘
         │ pass
         ▼
  ┌──────────────┐
  │ Build & Push │  ← Docker build, push to $REGISTRY
  │    Image     │     Tags: sha-XXXXXXX, main, latest
  └──────┬───────┘
         │ pass
         ▼
  ┌────────────────┐
  │ Update K8s     │  ← Clones connectk-backend repo
  │ Manifests      │     kustomize edit set image (cross-repo)
  └────────────────┘     Commit with [skip ci], push to backend repo
                         Only runs if vars.DEPLOY_OVERLAY is set
                         Requires BACKEND_REPO_PAT secret
```

---

## 4. GitHub Variables (vars.)

Variables are set in **GitHub → Repo → Settings → Secrets and variables → Actions → Variables**.

| Variable | Where to Set | Required | Default | Description |
|---|---|---|---|---|
| `REGISTRY` | Both repos | No | `ghcr.io` | Container registry hostname. See scenarios below for exact values. |
| `DEPLOY_OVERLAY` | Both repos | **Yes** (for GitOps) | *(empty — skips manifest update)* | Path to the Kustomize overlay, e.g. `eks/self-hosted`. Determines which overlay gets the updated image tag. |

### `REGISTRY` values by cloud

| Cloud Provider | Registry | `REGISTRY` value |
|---|---|---|
| Default (any) | GitHub Container Registry | `ghcr.io` (or leave unset) |
| AWS | Elastic Container Registry | `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com` |
| Azure | Azure Container Registry | `YOUR_ACR_NAME.azurecr.io` |
| GCP | Google Artifact Registry | `REGION-docker.pkg.dev/PROJECT_ID/REPO_NAME` |

### `DEPLOY_OVERLAY` values

| Target | `DEPLOY_OVERLAY` value |
|---|---|
| EKS + Self-Hosted DB/Redis | `eks/self-hosted` |
| EKS + Managed (RDS + ElastiCache) | `eks/managed` |
| AKS + Self-Hosted DB/Redis | `aks/self-hosted` |
| AKS + Managed (Azure DB + Azure Cache) | `aks/managed` |
| GKE + Self-Hosted DB/Redis | `gke/self-hosted` |
| GKE + Managed (CloudSQL + Memorystore) | `gke/managed` |

> **Important:** `DEPLOY_OVERLAY` must be set to the **same value** in both the backend
> and frontend repos. Both pipelines update the same overlay in the backend repo.

---

## 5. GitHub Secrets

Secrets are set in **GitHub → Repo → Settings → Secrets and variables → Actions → Secrets**.

### Always Required

| Secret | Where to Set | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | **Auto-provided by GitHub Actions.** No setup needed. Used for GHCR login and backend repo push. |

### Registry-Specific Secrets

Only configure the secrets matching your chosen `REGISTRY`:

#### GHCR (Default — no extra secrets needed)

| Secret | Where | Notes |
|---|---|---|
| *(none)* | — | `GITHUB_TOKEN` handles everything |

#### AWS ECR

| Secret | Where to Set | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | Both repos | IAM user access key with `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:PutImage`, etc. |
| `AWS_SECRET_ACCESS_KEY` | Both repos | Corresponding secret key |

#### Azure ACR

| Secret | Where to Set | Description |
|---|---|---|
| `ACR_USERNAME` | Both repos | ACR admin username or service principal appId |
| `ACR_PASSWORD` | Both repos | ACR admin password or service principal password |

#### GCP GAR

| Secret | Where to Set | Description |
|---|---|---|
| `GCP_SA_KEY` | Both repos | Full JSON key of a GCP service account with `roles/artifactregistry.writer` |

### Cross-Repo Secret (Frontend Only)

| Secret | Where to Set | Description |
|---|---|---|
| `BACKEND_REPO_PAT` | **Frontend repo only** | A GitHub fine-grained Personal Access Token scoped to `connectk-backend` with **Contents: Read & Write** permission. Required for the frontend CI to push manifest updates to the backend repo. |

**How to create `BACKEND_REPO_PAT`:**

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**
2. Click **Generate new token**
3. Token name: `connectk-frontend-ci`
4. Expiration: Set appropriate expiry (recommend 90 days, set a calendar reminder to rotate)
5. Resource owner: Your GitHub account/org
6. Repository access: **Only select repositories** → `connectk-backend`
7. Permissions: **Contents → Read and write** (this is the only permission needed)
8. Generate and copy the token
9. Go to `connectk-frontend` → Settings → Secrets → Actions → **New repository secret**
10. Name: `BACKEND_REPO_PAT`, Value: paste the token

---

## 6. Scenario Matrix — All Deployment Combinations

| # | Registry | Cloud | Strategy | `REGISTRY` var | `DEPLOY_OVERLAY` var | Extra Secrets |
|---|---|---|---|---|---|---|
| 1 | GHCR | EKS | self-hosted | *(unset)* | `eks/self-hosted` | `BACKEND_REPO_PAT` (frontend) |
| 2 | GHCR | EKS | managed | *(unset)* | `eks/managed` | `BACKEND_REPO_PAT` (frontend) |
| 3 | ECR | EKS | self-hosted | `ACCT.dkr.ecr.REGION.amazonaws.com` | `eks/self-hosted` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BACKEND_REPO_PAT` |
| 4 | ECR | EKS | managed | `ACCT.dkr.ecr.REGION.amazonaws.com` | `eks/managed` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BACKEND_REPO_PAT` |
| 5 | GHCR | AKS | self-hosted | *(unset)* | `aks/self-hosted` | `BACKEND_REPO_PAT` (frontend) |
| 6 | GHCR | AKS | managed | *(unset)* | `aks/managed` | `BACKEND_REPO_PAT` (frontend) |
| 7 | ACR | AKS | self-hosted | `myacr.azurecr.io` | `aks/self-hosted` | `ACR_USERNAME`, `ACR_PASSWORD`, `BACKEND_REPO_PAT` |
| 8 | ACR | AKS | managed | `myacr.azurecr.io` | `aks/managed` | `ACR_USERNAME`, `ACR_PASSWORD`, `BACKEND_REPO_PAT` |
| 9 | GHCR | GKE | self-hosted | *(unset)* | `gke/self-hosted` | `BACKEND_REPO_PAT` (frontend) |
| 10 | GHCR | GKE | managed | *(unset)* | `gke/managed` | `BACKEND_REPO_PAT` (frontend) |
| 11 | GAR | GKE | self-hosted | `REGION-docker.pkg.dev/PROJ/REPO` | `gke/self-hosted` | `GCP_SA_KEY`, `BACKEND_REPO_PAT` |
| 12 | GAR | GKE | managed | `REGION-docker.pkg.dev/PROJ/REPO` | `gke/managed` | `GCP_SA_KEY`, `BACKEND_REPO_PAT` |

---

## 7. Scenario 1 — GHCR + EKS Self-Hosted (Default)

This is the zero-config default. Images push to GitHub Container Registry, ArgoCD deploys
to EKS with self-hosted PostgreSQL and Redis.

### Setup

**Backend repo (`connectk-backend`) — Variables:**

| Variable | Value |
|---|---|
| `REGISTRY` | *(do not set — defaults to `ghcr.io`)* |
| `DEPLOY_OVERLAY` | `eks/self-hosted` |

**Backend repo — Secrets:**

| Secret | Value |
|---|---|
| `GITHUB_TOKEN` | *(automatic — no setup needed)* |

**Frontend repo (`connectk-frontend`) — Variables:**

| Variable | Value |
|---|---|
| `REGISTRY` | *(do not set — defaults to `ghcr.io`)* |
| `DEPLOY_OVERLAY` | `eks/self-hosted` |

**Frontend repo — Secrets:**

| Secret | Value |
|---|---|
| `GITHUB_TOKEN` | *(automatic — no setup needed)* |
| `BACKEND_REPO_PAT` | Fine-grained PAT with Contents read/write on `connectk-backend` |

### Resulting Image Names

```
ghcr.io/<github-owner>/connectk-backend:sha-abc1234
ghcr.io/<github-owner>/connectk-frontend:sha-abc1234
```

### ArgoCD ApplicationSet

In `k8s/argocd/applicationset.yaml`, ensure this entry is uncommented:

```yaml
generators:
  - list:
      elements:
        - cloud: eks
          strategy: self-hosted
```

### K8s Overlay

Update `k8s/overlays/eks/self-hosted/kustomization.yaml` image references to match GHCR:

```yaml
images:
  - name: BACKEND_IMAGE
    newName: ghcr.io/<github-owner>/connectk-backend
    newTag: latest    # CI will overwrite this with sha-XXXXXXX
  - name: FRONTEND_IMAGE
    newName: ghcr.io/<github-owner>/connectk-frontend
    newTag: latest
```

### Prerequisites

- EKS cluster running
- EBS CSI Driver add-on installed (required for self-hosted PVCs)
- ArgoCD installed in cluster
- `kubectl` access configured

---

## 8. Scenario 2 — ECR + EKS (Self-Hosted or Managed)

Images push to Amazon ECR. Ideal when the EKS cluster is in AWS and you want to keep
images in the same account/region for faster pulls and IAM-based access.

### AWS Prerequisites

```bash
# Create ECR repositories (one-time)
aws ecr create-repository --repository-name connectk-backend --region us-east-1
aws ecr create-repository --repository-name connectk-frontend --region us-east-1

# Create an IAM user for CI (or use OIDC — see Advanced section)
aws iam create-user --user-name connectk-ci
aws iam attach-user-policy --user-name connectk-ci \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser
aws iam create-access-key --user-name connectk-ci
# Save the AccessKeyId and SecretAccessKey
```

### Setup — Self-Hosted

**Both repos — Variables:**

| Variable | Value |
|---|---|
| `REGISTRY` | `123456789012.dkr.ecr.us-east-1.amazonaws.com` |
| `DEPLOY_OVERLAY` | `eks/self-hosted` |

**Both repos — Secrets:**

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM access key from above |
| `AWS_SECRET_ACCESS_KEY` | IAM secret key from above |

**Frontend repo — Additional Secrets:**

| Secret | Value |
|---|---|
| `BACKEND_REPO_PAT` | Fine-grained PAT |

### Setup — Managed (RDS + ElastiCache)

Same as self-hosted but change:

| Variable | Value |
|---|---|
| `DEPLOY_OVERLAY` | `eks/managed` |

And update the managed component placeholders before deploying:

```
k8s/components/postgres-managed/kustomization.yaml  → Replace MANAGED_HOST, MANAGED_USER, MANAGED_PASSWORD
k8s/components/redis-managed/kustomization.yaml     → Replace MANAGED_REDIS_HOST
```

### Resulting Image Names

```
123456789012.dkr.ecr.us-east-1.amazonaws.com/connectk-backend:sha-abc1234
123456789012.dkr.ecr.us-east-1.amazonaws.com/connectk-frontend:sha-abc1234
```

### K8s Overlay

`k8s/overlays/eks/self-hosted/kustomization.yaml` (or `managed/`):

```yaml
images:
  - name: BACKEND_IMAGE
    newName: 123456789012.dkr.ecr.us-east-1.amazonaws.com/connectk-backend
    newTag: latest
  - name: FRONTEND_IMAGE
    newName: 123456789012.dkr.ecr.us-east-1.amazonaws.com/connectk-frontend
    newTag: latest
```

### EKS-Specific Prerequisites

```bash
# Install EBS CSI Driver (required for self-hosted DB/Redis PVCs)
eksctl create addon --name aws-ebs-csi-driver --cluster YOUR_CLUSTER \
  --service-account-role-arn arn:aws:iam::123456789012:role/AmazonEKS_EBS_CSI_DriverRole

# ECR pull access: EKS nodes auto-pull from ECR in the same account via instance profile.
# For cross-account or Fargate, see AWS docs on ECR pull-through cache.
```

---

## 9. Scenario 3 — ACR + AKS (Self-Hosted or Managed)

Images push to Azure Container Registry. Ideal for Azure-native deployments.

### Azure Prerequisites

```bash
# Create ACR (one-time)
az acr create --resource-group connectk-rg --name connectkacr --sku Basic

# Option A: Enable admin credentials for CI
az acr update --name connectkacr --admin-enabled true
az acr credential show --name connectkacr
# Note the username and password

# Option B (recommended): Create a service principal instead
az ad sp create-for-rbac --name connectk-ci \
  --scopes /subscriptions/<sub-id>/resourceGroups/connectk-rg/providers/Microsoft.ContainerRegistry/registries/connectkacr \
  --role acrpush
# Note the appId (=ACR_USERNAME) and password (=ACR_PASSWORD)

# Attach ACR to AKS for image pull access
az aks update --resource-group connectk-rg --name connectk-aks \
  --attach-acr connectkacr
```

### Setup — Self-Hosted

**Both repos — Variables:**

| Variable | Value |
|---|---|
| `REGISTRY` | `connectkacr.azurecr.io` |
| `DEPLOY_OVERLAY` | `aks/self-hosted` |

**Both repos — Secrets:**

| Secret | Value |
|---|---|
| `ACR_USERNAME` | ACR admin username or SP appId |
| `ACR_PASSWORD` | ACR admin password or SP password |

**Frontend repo — Additional Secrets:**

| Secret | Value |
|---|---|
| `BACKEND_REPO_PAT` | Fine-grained PAT |

### Setup — Managed (Azure DB for PostgreSQL + Azure Cache for Redis)

Same as self-hosted but change:

| Variable | Value |
|---|---|
| `DEPLOY_OVERLAY` | `aks/managed` |

And update managed component placeholders with your Azure service endpoints.

### Resulting Image Names

```
connectkacr.azurecr.io/connectk-backend:sha-abc1234
connectkacr.azurecr.io/connectk-frontend:sha-abc1234
```

### K8s Overlay

`k8s/overlays/aks/self-hosted/kustomization.yaml` (or `managed/`):

```yaml
images:
  - name: BACKEND_IMAGE
    newName: connectkacr.azurecr.io/connectk-backend
    newTag: latest
  - name: FRONTEND_IMAGE
    newName: connectkacr.azurecr.io/connectk-frontend
    newTag: latest
```

---

## 10. Scenario 4 — GAR + GKE (Self-Hosted or Managed)

Images push to Google Artifact Registry. Ideal for GCP-native deployments.

### GCP Prerequisites

```bash
# Create GAR repository (one-time)
gcloud artifacts repositories create connectk \
  --repository-format=docker \
  --location=us-central1 \
  --description="ConnectK container images"

# Create service account for CI
gcloud iam service-accounts create connectk-ci \
  --display-name="ConnectK CI"

gcloud artifacts repositories add-iam-policy-binding connectk \
  --location=us-central1 \
  --member="serviceAccount:connectk-ci@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"

# Create and download JSON key
gcloud iam service-accounts keys create key.json \
  --iam-account=connectk-ci@PROJECT_ID.iam.gserviceaccount.com

# The full contents of key.json go into the GCP_SA_KEY secret
cat key.json   # Copy this entire output
```

### Setup — Self-Hosted

**Both repos — Variables:**

| Variable | Value |
|---|---|
| `REGISTRY` | `us-central1-docker.pkg.dev/my-project/connectk` |
| `DEPLOY_OVERLAY` | `gke/self-hosted` |

**Both repos — Secrets:**

| Secret | Value |
|---|---|
| `GCP_SA_KEY` | Full contents of the service account JSON key file |

**Frontend repo — Additional Secrets:**

| Secret | Value |
|---|---|
| `BACKEND_REPO_PAT` | Fine-grained PAT |

### Setup — Managed (CloudSQL + Memorystore)

Same as self-hosted but change:

| Variable | Value |
|---|---|
| `DEPLOY_OVERLAY` | `gke/managed` |

And update managed component placeholders with your CloudSQL and Memorystore endpoints.

### Resulting Image Names

```
us-central1-docker.pkg.dev/my-project/connectk/connectk-backend:sha-abc1234
us-central1-docker.pkg.dev/my-project/connectk/connectk-frontend:sha-abc1234
```

### K8s Overlay

`k8s/overlays/gke/self-hosted/kustomization.yaml` (or `managed/`):

```yaml
images:
  - name: BACKEND_IMAGE
    newName: us-central1-docker.pkg.dev/my-project/connectk/connectk-backend
    newTag: latest
  - name: FRONTEND_IMAGE
    newName: us-central1-docker.pkg.dev/my-project/connectk/connectk-frontend
    newTag: latest
```

---

## 11. Scenario 5 — GHCR + Any Cloud

You can use GHCR as your registry regardless of which cloud you deploy to. This is
useful for keeping a single registry across multiple clouds or when you don't want
cloud-specific registry setup.

### Setup

**Both repos — Variables:**

| Variable | Value |
|---|---|
| `REGISTRY` | *(do not set — defaults to `ghcr.io`)* |
| `DEPLOY_OVERLAY` | Any overlay (e.g. `aks/self-hosted`, `gke/managed`) |

**No extra registry secrets needed** — `GITHUB_TOKEN` handles GHCR auth automatically.

### K8s Pull Secret (Required for non-GitHub clusters)

When using GHCR with EKS/AKS/GKE, the cluster needs credentials to pull private images.
Create an image pull secret:

```bash
kubectl create secret docker-registry ghcr-pull-secret \
  --namespace connectk \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USERNAME \
  --docker-password=YOUR_GITHUB_PAT \
  --docker-email=your@email.com
```

Then add to your deployment specs (or patch via kustomize):

```yaml
spec:
  template:
    spec:
      imagePullSecrets:
        - name: ghcr-pull-secret
```

> **Note:** If your GHCR packages are set to **public**, no pull secret is needed.

---

## 12. Scenario 6 — Multi-Cloud (Multiple Overlays)

For deploying to multiple clouds simultaneously. The current pipeline supports
**one overlay at a time** via `DEPLOY_OVERLAY`.

### Option A: Primary + Manual

Set `DEPLOY_OVERLAY` to your primary cloud. For additional clouds, update their overlays
manually or via a separate workflow dispatch.

### Option B: Matrix Strategy (Advanced)

Modify the `update-manifests` job to loop over multiple overlays:

```yaml
update-manifests:
  strategy:
    matrix:
      overlay: [eks/self-hosted, aks/managed]
  steps:
    - name: Update image tag
      run: |
        cd k8s/overlays/${{ matrix.overlay }}
        kustomize edit set image BACKEND_IMAGE=${{ env.IMAGE_NAME }}:sha-${GITHUB_SHA::7}
```

### ArgoCD ApplicationSet for Multi-Cloud

Uncomment multiple entries in `k8s/argocd/applicationset.yaml`:

```yaml
generators:
  - list:
      elements:
        - cloud: eks
          strategy: self-hosted
        - cloud: aks
          strategy: managed
        - cloud: gke
          strategy: self-hosted
```

This creates one ArgoCD Application per entry, each syncing its own overlay path.

---

## 13. ArgoCD Setup

### Install ArgoCD

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

### Register the Git Repository

```bash
# If the backend repo is public, no credentials needed.
# For private repos:
argocd repo add https://github.com/MahammadRafi06/connectk-backend.git \
  --username git \
  --password YOUR_GITHUB_PAT
```

### Apply ArgoCD Resources

```bash
# Apply the AppProject (defines allowed sources, destinations, resources)
kubectl apply -f k8s/argocd/project.yaml

# Apply the ApplicationSet (creates Applications based on the generator list)
kubectl apply -f k8s/argocd/applicationset.yaml
```

### What Each ArgoCD File Does

**`k8s/argocd/project.yaml`** — AppProject:
- Restricts deployments to the `connectk` namespace only
- Only allows manifests from `connectk-backend.git`
- Whitelists specific K8s resource types (Namespace, StorageClass, Deployments, Services, Ingress, HPAs, etc.)
- Has optional sync windows (uncomment to restrict deploys to business hours)

**`k8s/argocd/applicationset.yaml`** — ApplicationSet:
- Uses a list generator with `cloud`/`strategy` pairs
- Creates one ArgoCD Application per entry (e.g. `connectk-eks-self-hosted`)
- Enables automated sync with `prune: true` and `selfHeal: true`
- Ignores `/spec/replicas` differences (lets HPA manage scaling without sync conflicts)
- Retries failed syncs up to 3 times with exponential backoff (5s → 10s → 20s)

### ArgoCD Application Naming

Applications are named: `connectk-{cloud}-{strategy}`

Examples:
- `connectk-eks-self-hosted`
- `connectk-aks-managed`
- `connectk-gke-self-hosted`

### Sync Flow

```
CI pushes new image tag to k8s/overlays/<cloud>/<strategy>/kustomization.yaml
    │
    ▼
ArgoCD detects diff in connectk-backend repo (polling every 3 min or via webhook)
    │
    ▼
ArgoCD runs: kustomize build k8s/overlays/<cloud>/<strategy>/
    │
    ▼
ArgoCD applies the rendered manifests to the cluster
    │
    ▼
Kubernetes rolls out new Deployment revision (zero-downtime rolling update)
```

### Optional: ArgoCD Webhook for Instant Syncs

By default, ArgoCD polls every 3 minutes. For instant syncs after CI pushes:

1. In ArgoCD, go to **Settings → Repositories** and note the webhook URL
2. In GitHub (`connectk-backend` repo), go to **Settings → Webhooks → Add webhook**
3. Payload URL: `https://<argocd-server>/api/webhook`
4. Content type: `application/json`
5. Secret: *(set in ArgoCD `argocd-secret` → `webhook.github.secret`)*
6. Events: **Just the push event**

---

## 14. Image Tagging Strategy

Each build produces three tags:

| Tag | Format | Example | Purpose |
|---|---|---|---|
| SHA | `sha-XXXXXXX` | `sha-abc1234` | **Immutable** — used for deployments |
| Branch | `main` | `main` | Mutable — tracks latest on branch |
| Latest | `latest` | `latest` | Mutable — convenience tag |

The `update-manifests` job always uses the **SHA tag** (`sha-XXXXXXX`) in the Kustomize
overlay, ensuring immutable, traceable, auditable deployments.

```bash
# The CI uses the first 7 chars of the git commit SHA
kustomize edit set image BACKEND_IMAGE=$IMAGE_NAME:sha-${GITHUB_SHA::7}
```

**Why SHA tags?**
- Every deployment is traceable to an exact git commit
- No ambiguity — `sha-abc1234` always refers to the same image
- Easy rollback — just revert to a previous SHA tag
- `latest` and `main` are still pushed for development convenience

---

## 15. Path Filters — What Triggers Each Pipeline

### Backend CI triggers on push to `main` when these paths change:

| Path | What it covers |
|---|---|
| `app/**` | FastAPI application code |
| `alembic/**` | Database migration scripts |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container build instructions |
| `.github/workflows/ci-backend.yaml` | The pipeline itself |

### Frontend CI triggers on push to `main` when these paths change:

| Path | What it covers |
|---|---|
| `app/**` | Next.js pages and API routes |
| `components/**` | React components |
| `hooks/**` | Custom React hooks |
| `lib/**` | Shared utilities |
| `types/**` | TypeScript type definitions |
| `public/**` | Static assets |
| `package.json` | Node.js dependencies |
| `package-lock.json` | Dependency lockfile |
| `Dockerfile` | Container build instructions |
| `.github/workflows/ci-frontend.yaml` | The pipeline itself |

### Pull Request triggers (lint/type-check only — no build or push):

- **Backend PR**: `app/**`, `alembic/**`, `requirements.txt`
- **Frontend PR**: `app/**`, `components/**`, `lib/**`, `package.json`

### What does NOT trigger CI:

- Changes to `k8s/` manifests in the backend repo (prevents infinite loops from `[skip ci]` commits)
- Changes to `README.md`, `docs/`, or non-application files
- Changes to branches other than `main`
- Commits with `[skip ci]` in the message

---

## 16. The Update-Manifests Job — How GitOps Works

### Backend Repo (same-repo update)

1. CI checks out the repo with `contents: write` permission
2. Navigates to `k8s/overlays/${{ vars.DEPLOY_OVERLAY }}`
3. Runs `kustomize edit set image BACKEND_IMAGE=<registry>/connectk-backend:sha-XXXXXXX`
4. This modifies the `images:` section in the overlay's `kustomization.yaml`
5. Commits: `ci: update backend image to sha-XXXXXXX [skip ci]`
6. Pushes to `main`

### Frontend Repo (cross-repo update)

1. CI checks out the **backend** repo using `BACKEND_REPO_PAT`
2. Navigates to `backend-repo/k8s/overlays/${{ vars.DEPLOY_OVERLAY }}`
3. Runs `kustomize edit set image FRONTEND_IMAGE=<registry>/connectk-frontend:sha-XXXXXXX`
4. Commits: `ci: update frontend image to sha-XXXXXXX [skip ci]`
5. Pushes to `connectk-backend` repo's `main` branch

### Why `[skip ci]`?

The manifest update commit only changes files under `k8s/`. Since `k8s/` is NOT in the
backend CI's path filters, it won't re-trigger CI anyway. `[skip ci]` is an extra safety
net to prevent any possible infinite loop.

### Guard: `if: vars.DEPLOY_OVERLAY != ''`

The `update-manifests` job only runs when `DEPLOY_OVERLAY` is set:

- **Not set (empty)**: CI builds and pushes the Docker image but does NOT update manifests.
  Useful during initial setup, testing, or when managing deployments manually.
- **Set to a value**: Full GitOps flow — image is built, pushed, and the K8s manifest is
  automatically updated, which triggers ArgoCD to sync.

---

## 17. Manual Pipeline Triggers

The current pipelines only trigger on `push` and `pull_request`. To add manual triggers,
add `workflow_dispatch` to the `on:` section of the workflow YAML:

```yaml
on:
  push:
    branches: [main]
    paths: [...]
  pull_request:
    branches: [main]
    paths: [...]
  workflow_dispatch:          # ← Add this block
    inputs:
      deploy_overlay:
        description: 'Override DEPLOY_OVERLAY for this run'
        required: false
        type: string
```

Then trigger manually:

```bash
# Via GitHub CLI — default overlay
gh workflow run "CI — Backend" --repo MahammadRafi06/connectk-backend

# With a custom overlay override
gh workflow run "CI — Backend" --repo MahammadRafi06/connectk-backend \
  -f deploy_overlay=eks/managed

# Via GitHub UI: Actions tab → select workflow → "Run workflow" button
```

---

## 18. Rollback Procedures

### Option A: ArgoCD Rollback (Fastest — for emergencies)

```bash
# List application history
argocd app history connectk-eks-self-hosted

# Rollback to a previous revision
argocd app rollback connectk-eks-self-hosted REVISION_NUMBER
```

> **Warning:** With `selfHeal: true`, ArgoCD will re-sync to the Git state within minutes.
> Disable auto-sync first if you need the rollback to persist:

```bash
argocd app set connectk-eks-self-hosted --sync-policy none
argocd app rollback connectk-eks-self-hosted REVISION_NUMBER
# Re-enable later:
argocd app set connectk-eks-self-hosted --sync-policy automated --self-heal --auto-prune
```

### Option B: Git Revert (Recommended — permanent rollback)

```bash
# In the backend repo, find the manifest update commit to revert
git log --oneline k8s/overlays/eks/self-hosted/kustomization.yaml

# Revert the bad commit
git revert COMMIT_SHA
git push

# ArgoCD auto-syncs to the previous (good) image tag
```

### Option C: Manual Image Tag Override

```bash
# Directly set the overlay to a known-good image tag
cd k8s/overlays/eks/self-hosted
kustomize edit set image BACKEND_IMAGE=ghcr.io/<owner>/connectk-backend:sha-GOOD_SHA
git add . && git commit -m "rollback: backend to sha-GOOD_SHA" && git push
```

### Option D: Kubernetes-Level Rollback (No Git change)

```bash
# Rollback a deployment to the previous revision
kubectl rollout undo deployment/backend -n connectk

# Check rollout history
kubectl rollout history deployment/backend -n connectk
```

> **Note:** With ArgoCD `selfHeal: true`, this will be overridden on the next sync.
> Combine with disabling auto-sync if needed.

---

## 19. Troubleshooting

### CI Issues

#### "Update K8s Manifests" job is skipped

**Cause:** `DEPLOY_OVERLAY` variable is not set.

**Fix:** Go to repo → Settings → Secrets and variables → Actions → Variables →
New repository variable. Name: `DEPLOY_OVERLAY`, Value: e.g. `eks/self-hosted`.

---

#### "Update K8s Manifests" fails with 403/authentication error (frontend)

**Cause:** `BACKEND_REPO_PAT` secret is missing, expired, or lacks the right permissions.

**Fix:**
1. Check if the PAT has expired (fine-grained tokens have expiry dates)
2. Regenerate with **Contents: Read and write** on `connectk-backend`
3. Update the secret in `connectk-frontend` → Settings → Secrets

---

#### "Update K8s Manifests" fails with 403 (backend)

**Cause:** `GITHUB_TOKEN` doesn't have `contents: write` permission.

**Fix:** Verify the workflow YAML has:
```yaml
permissions:
  contents: write
  packages: write
```

---

#### Docker build fails: `"/app/public": not found` (frontend)

**Cause:** The `public/` directory has no tracked files (git doesn't track empty dirs).

**Fix:** `touch public/.gitkeep && git add public/.gitkeep && git commit && git push`

---

#### Lint job fails with ESLint interactive prompt (frontend)

**Cause:** Missing `.eslintrc.json` — Next.js tries to interactively configure ESLint.

**Fix:** Create `.eslintrc.json` in the frontend repo root:
```json
{
  "extends": "next/core-web-vitals"
}
```

---

#### `pip install` fails with "Invalid requirement" (backend)

**Cause:** Smart/curly quotes in `requirements.txt` (often from copy-pasting).

**Fix:** Ensure all quotes are straight ASCII or removed:
```
fastapi[standard]       ← correct (no quotes needed)
"fastapi[standard]"     ← WRONG (curly quotes U+201C/U+201D break pip)
```

---

#### CI triggers but build-push job is skipped

**Cause:** The `build-push` job has `if: github.event_name == 'push' && github.ref == 'refs/heads/main'`.
Pull requests only run the lint job.

**Fix:** This is by design. Build & push only happens on merge to `main`.

---

### ArgoCD Issues

#### Application shows "OutOfSync" but won't sync

**Cause 1:** AppProject doesn't allow the resource type.

**Fix:** Check `k8s/argocd/project.yaml` → `namespaceResourceWhitelist` and
`clusterResourceWhitelist`. Add the missing resource group/kind.

**Cause 2:** Overlay path doesn't match ApplicationSet generator.

**Fix:** Ensure the ApplicationSet has a matching `cloud`/`strategy` entry.

---

#### Application shows "ComparisonError"

**Cause:** Kustomization overlay has invalid references or missing files.

**Fix:** Test locally:
```bash
kustomize build k8s/overlays/eks/self-hosted/
# Fix any errors shown in the output
```

---

#### Application shows "SyncFailed" with namespace error

**Cause:** The namespace doesn't exist and `CreateNamespace=true` isn't set.

**Fix:** Already configured in the ApplicationSet. If overridden, re-add:
```yaml
syncOptions:
  - CreateNamespace=true
```

---

#### Replicas keep resetting (HPA conflict)

**Cause:** ArgoCD and HPA are fighting over `/spec/replicas`.

**Fix:** Already configured. Verify `ignoreDifferences` is present:
```yaml
ignoreDifferences:
  - group: apps
    kind: Deployment
    jsonPointers:
      - /spec/replicas
```

---

### Cluster Issues

#### Image pull fails: `ErrImagePull` / `ImagePullBackOff`

**Cause 1 (GHCR):** Package is private and cluster lacks pull credentials.

**Fix:** Either make the GHCR package public, or create an `imagePullSecret`:
```bash
kubectl create secret docker-registry ghcr-pull-secret \
  --namespace connectk \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USER \
  --docker-password=YOUR_PAT
```

**Cause 2 (ECR):** Node IAM role lacks pull permissions.

**Fix:** `aws iam attach-role-policy --role-name <node-role> --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly`

**Cause 3 (ACR):** AKS not attached to ACR.

**Fix:** `az aks update -g <rg> -n <cluster> --attach-acr <acr-name>`

**Cause 4 (GAR):** GKE node SA lacks `artifactregistry.reader` role.

**Fix:** `gcloud projects add-iam-policy-binding <project> --member=serviceAccount:<node-sa> --role=roles/artifactregistry.reader`

---

#### PVC stuck in "Pending" (self-hosted)

**Cause 1:** StorageClass `connectk-ssd` not created.

**Fix:** Ensure the overlay includes the storageclass resource:
```bash
kubectl get storageclass connectk-ssd
# If missing, apply:
kubectl apply -f k8s/overlays/eks/storageclass.yaml
```

**Cause 2 (EKS):** EBS CSI Driver not installed.

**Fix:**
```bash
eksctl create addon --name aws-ebs-csi-driver --cluster YOUR_CLUSTER \
  --service-account-role-arn arn:aws:iam::ACCOUNT:role/AmazonEKS_EBS_CSI_DriverRole
```

---

## 20. Quick-Start Checklist

### First-Time Setup (do once)

**GitHub Configuration:**
- [ ] **Backend repo:** Set `DEPLOY_OVERLAY` variable (e.g. `eks/self-hosted`)
- [ ] **Frontend repo:** Set `DEPLOY_OVERLAY` variable (**same value** as backend)
- [ ] **Frontend repo:** Create `BACKEND_REPO_PAT` secret
- [ ] If using ECR: Set `REGISTRY` var + `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` secrets (both repos)
- [ ] If using ACR: Set `REGISTRY` var + `ACR_USERNAME` / `ACR_PASSWORD` secrets (both repos)
- [ ] If using GAR: Set `REGISTRY` var + `GCP_SA_KEY` secret (both repos)

**K8s Manifests (in backend repo):**
- [ ] Update overlay `kustomization.yaml` image `newName` to match your registry
- [ ] Update `k8s/base/secrets.yaml` with real values (Azure Entra, session keys, etc.)
- [ ] If self-hosted DB: Set password in `k8s/components/postgres-self-hosted/kustomization.yaml`
- [ ] If managed DB: Set endpoint in `k8s/components/postgres-managed/kustomization.yaml`
- [ ] If managed Redis: Set endpoint in `k8s/components/redis-managed/kustomization.yaml`

**Cluster:**
- [ ] K8s cluster running (EKS/AKS/GKE)
- [ ] If EKS self-hosted: EBS CSI Driver add-on installed
- [ ] ArgoCD installed
- [ ] Apply `kubectl apply -f k8s/argocd/project.yaml`
- [ ] Uncomment your cloud/strategy in `applicationset.yaml`
- [ ] Apply `kubectl apply -f k8s/argocd/applicationset.yaml`

### Verify (after setup)

- [ ] Push a code change to **backend** repo → CI should: lint ✓ → build & push ✓ → update manifests ✓
- [ ] Push a code change to **frontend** repo → CI should: lint & type-check ✓ → build & push ✓ → update manifests in backend repo ✓
- [ ] ArgoCD UI → Application should appear as **Synced** and **Healthy**
- [ ] Access the app via Ingress URL

### Day-to-Day Workflow

```
1. Develop locally
2. Push to main (or merge a PR)
3. CI runs automatically: lint → build → push image → update manifests
4. ArgoCD detects manifest change → syncs to cluster
5. New version is live (zero-downtime rolling update)
```

No manual `kubectl apply` needed after initial setup.
