# ConnectK Kubernetes Deployment Guide

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Directory Structure](#directory-structure)
- [Prerequisites](#prerequisites)
- [Deployment Matrix](#deployment-matrix)
- [Step 1: Container Images](#step-1-container-images)
- [Step 2: Configure Secrets](#step-2-configure-secrets)
- [Step 3: Configure Environment](#step-3-configure-environment)
- [Step 4: Cloud-Specific Setup](#step-4-cloud-specific-setup)
  - [GKE (Google Kubernetes Engine)](#gke-google-kubernetes-engine)
  - [AKS (Azure Kubernetes Service)](#aks-azure-kubernetes-service)
  - [EKS (Elastic Kubernetes Service)](#eks-elastic-kubernetes-service)
- [Step 5: Choose Database & Redis Strategy](#step-5-choose-database--redis-strategy)
  - [Self-Hosted](#self-hosted)
  - [Managed Services](#managed-services)
- [Step 6: Deploy](#step-6-deploy)
- [Step 7: Post-Deployment Verification](#step-7-post-deployment-verification)
- [Storage Details](#storage-details)
- [Scaling](#scaling)
- [Database Migrations](#database-migrations)
- [Troubleshooting](#troubleshooting)
- [CI/CD Pipeline](#cicd-pipeline)
  - [Overview](#overview)
  - [GitHub Actions — CI](#github-actions--ci)
  - [ArgoCD — CD](#argocd--cd)
  - [Switching Container Registries](#switching-container-registries)
  - [End-to-End Flow](#end-to-end-flow)

---

## Architecture Overview

```
                        ┌─────────────────────────────────┐
                        │           Ingress (NGINX)        │
                        │  connectk.yourdomain.com         │
                        └──────┬───────────────┬──────────┘
                               │               │
                          /api/*            /*
                               │               │
                   ┌───────────▼──┐   ┌────────▼───────┐
                   │   Backend    │   │    Frontend     │
                   │  (FastAPI)   │   │   (Next.js)     │
                   │  Port 8000   │   │   Port 3000     │
                   │  2-10 pods   │   │   2-5 pods      │
                   └──┬───────┬──┘   └────────────────┘
                      │       │
              ┌───────▼──┐ ┌──▼───────┐
              │ PostgreSQL│ │  Redis   │
              │ Port 5432 │ │ Port 6379│
              └──────────┘ └──────────┘
         (self-hosted OR managed)  (self-hosted OR managed)
```

Key properties:
- Backend is **stateless** — all state lives in PostgreSQL and Redis
- Sessions, rate limiting, and SSE pub/sub use Redis (works across replicas)
- Database migrations run in a Kubernetes **init container**, not the app entrypoint
- Frontend connects to backend via cluster-internal DNS (`backend.connectk.svc.cluster.local`)
- External traffic enters through a single Ingress that routes by path

---

## Directory Structure

```
.github/workflows/
├── ci-backend.yaml                        # Build, lint, push backend image
└── ci-frontend.yaml                       # Build, lint, push frontend image

k8s/
├── base/                                  # Common manifests (all environments)
│   ├── kustomization.yaml
│   ├── namespace.yaml                     # connectk namespace
│   ├── secrets.yaml                       # Backend secrets (placeholders)
│   ├── ingress.yaml                       # NGINX Ingress routing
│   ├── backend/
│   │   ├── deployment.yaml                # FastAPI + migration init container
│   │   ├── service.yaml                   # ClusterIP :8000
│   │   ├── configmap.yaml                 # Non-secret environment config
│   │   └── hpa.yaml                       # Autoscaler (2–10 replicas)
│   └── frontend/
│       ├── deployment.yaml                # Next.js standalone
│       ├── service.yaml                   # ClusterIP :3000
│       ├── configmap.yaml                 # NEXT_PUBLIC_API_URL
│       └── hpa.yaml                       # Autoscaler (2–5 replicas)
├── components/                            # Kustomize Components (mix-and-match)
│   ├── postgres-self-hosted/              # StatefulSet + PVC + headless Service
│   │   ├── kustomization.yaml
│   │   ├── statefulset.yaml
│   │   ├── service.yaml
│   │   └── secret.yaml                   # Postgres credentials
│   ├── redis-self-hosted/                 # StatefulSet + PVC + headless Service
│   │   ├── kustomization.yaml
│   │   ├── statefulset.yaml
│   │   └── service.yaml
│   ├── postgres-managed/                  # Patches DATABASE_URL to external endpoint
│   │   └── kustomization.yaml
│   └── redis-managed/                     # Patches REDIS_URL to external endpoint
│       └── kustomization.yaml
├── argocd/                                # ArgoCD application definitions
│   ├── project.yaml                       # AppProject with RBAC boundaries
│   └── applicationset.yaml                # ApplicationSet (one per cloud/strategy)
└── overlays/                              # Cloud + strategy combinations
    ├── gke/
    │   ├── storageclass.yaml              # pd.csi.storage.gke.io / pd-ssd
    │   ├── self-hosted/kustomization.yaml
    │   └── managed/kustomization.yaml
    ├── aks/
    │   ├── storageclass.yaml              # disk.csi.azure.com / StandardSSD_LRS
    │   ├── self-hosted/kustomization.yaml
    │   └── managed/kustomization.yaml
    └── eks/
        ├── storageclass.yaml              # ebs.csi.aws.com / gp3 (encrypted)
        ├── ebs-csi-driver-sa.yaml         # IRSA ServiceAccount (manual CSI only)
        ├── self-hosted/kustomization.yaml
        └── managed/kustomization.yaml
```

---

## Prerequisites

| Requirement | Details |
|-------------|---------|
| `kubectl` | v1.25+ configured with cluster access |
| `kustomize` | v5.0+ (or use `kubectl apply -k` which bundles it) |
| NGINX Ingress Controller | Installed on the cluster ([install guide](https://kubernetes.github.io/ingress-nginx/deploy/)) |
| Container registry | ECR (AWS), ACR (Azure), or GCR/Artifact Registry (GCP) |
| TLS certificate | Provision via cert-manager or upload manually as a K8s Secret |
| DNS record | `connectk.yourdomain.com` → Ingress load balancer IP |

---

## Deployment Matrix

Pick **one cloud** and **one database strategy**:

| Cloud | Self-Hosted DB & Redis | Managed DB & Redis |
|-------|------------------------|--------------------|
| GKE   | `kubectl apply -k k8s/overlays/gke/self-hosted/` | `kubectl apply -k k8s/overlays/gke/managed/` |
| AKS   | `kubectl apply -k k8s/overlays/aks/self-hosted/` | `kubectl apply -k k8s/overlays/aks/managed/` |
| EKS   | `kubectl apply -k k8s/overlays/eks/self-hosted/` | `kubectl apply -k k8s/overlays/eks/managed/` |

---

## Step 1: Container Images

Build and push both images to your container registry.

### GKE (Artifact Registry)

```bash
# Authenticate
gcloud auth configure-docker REGION-docker.pkg.dev

# Build and push
docker build -t REGION-docker.pkg.dev/PROJECT_ID/connectk/backend:v1.0.0 ./backend
docker build -t REGION-docker.pkg.dev/PROJECT_ID/connectk/frontend:v1.0.0 ./frontend
docker push REGION-docker.pkg.dev/PROJECT_ID/connectk/backend:v1.0.0
docker push REGION-docker.pkg.dev/PROJECT_ID/connectk/frontend:v1.0.0
```

### AKS (Azure Container Registry)

```bash
# Authenticate
az acr login --name YOUR_ACR

# Build and push
docker build -t YOUR_ACR.azurecr.io/connectk-backend:v1.0.0 ./backend
docker build -t YOUR_ACR.azurecr.io/connectk-frontend:v1.0.0 ./frontend
docker push YOUR_ACR.azurecr.io/connectk-backend:v1.0.0
docker push YOUR_ACR.azurecr.io/connectk-frontend:v1.0.0
```

### EKS (Elastic Container Registry)

```bash
# Authenticate
aws ecr get-login-password --region REGION | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com

# Create repositories (first time only)
aws ecr create-repository --repository-name connectk-backend
aws ecr create-repository --repository-name connectk-frontend

# Build and push
docker build -t ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/connectk-backend:v1.0.0 ./backend
docker build -t ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/connectk-frontend:v1.0.0 ./frontend
docker push ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/connectk-backend:v1.0.0
docker push ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/connectk-frontend:v1.0.0
```

Then update the `images` section in your chosen overlay's `kustomization.yaml`:

```yaml
images:
  - name: BACKEND_IMAGE
    newName: YOUR_REGISTRY/connectk-backend
    newTag: v1.0.0
  - name: FRONTEND_IMAGE
    newName: YOUR_REGISTRY/connectk-frontend
    newTag: v1.0.0
```

---

## Step 2: Configure Secrets

Edit `base/secrets.yaml` and fill in all `REPLACE_ME` values:

| Secret | How to generate / where to find |
|--------|---------------------------------|
| `AZURE_TENANT_ID` | Azure Portal → Entra ID → Overview |
| `AZURE_CLIENT_ID` | Azure Portal → App Registrations → your app |
| `AZURE_CLIENT_SECRET` | Azure Portal → App Registrations → Certificates & secrets |
| `INITIAL_ADMIN_ENTRA_GROUP_ID` | Azure Portal → Entra ID → Groups → your admin group Object ID |
| `ADMIN_GROUP_IDS` | Comma-separated group IDs |
| `SESSION_SECRET_KEY` | `openssl rand -hex 32` |
| `CSRF_SECRET_KEY` | `openssl rand -hex 32` |
| `GIT_SSH_PRIVATE_KEY` | Base64-encoded SSH private key for GitOps repos |
| `ARGOCD_SERVER_URL` | Your ArgoCD server URL |

> **Production recommendation**: Use [External Secrets Operator](https://external-secrets.io/) with AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager instead of plain Kubernetes Secrets.

---

## Step 3: Configure Environment

### Backend ConfigMap (`base/backend/configmap.yaml`)

Update these fields to match your environment:

```yaml
ALLOWED_ORIGINS: "https://connectk.yourdomain.com"
OIDC_REDIRECT_URI: "https://connectk.yourdomain.com/api/auth/callback"
```

### Frontend ConfigMap (`base/frontend/configmap.yaml`)

The default points to the backend's cluster-internal DNS. No change needed unless your setup differs:

```yaml
NEXT_PUBLIC_API_URL: "http://backend.connectk.svc.cluster.local:8000"
```

### Ingress (`base/ingress.yaml`)

Replace the hostname:

```yaml
spec:
  tls:
    - hosts:
        - connectk.yourdomain.com   # ← your domain
      secretName: connectk-tls       # ← your TLS secret name
  rules:
    - host: connectk.yourdomain.com  # ← your domain
```

---

## Step 4: Cloud-Specific Setup

### GKE (Google Kubernetes Engine)

**CSI Driver**: GCE PD CSI driver is **pre-installed** on GKE 1.18+. No action needed.

**StorageClass** (`overlays/gke/storageclass.yaml`):
- Provisioner: `pd.csi.storage.gke.io`
- Disk type: `pd-ssd`
- Binding: `WaitForFirstConsumer` (provisions in same AZ as pod)

**NGINX Ingress**:
```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.0/deploy/static/provider/cloud/deploy.yaml
```

### AKS (Azure Kubernetes Service)

**CSI Driver**: Azure Disk CSI driver is **pre-installed** on AKS 1.21+. No action needed.

**StorageClass** (`overlays/aks/storageclass.yaml`):
- Provisioner: `disk.csi.azure.com`
- SKU: `StandardSSD_LRS` (change to `Premium_LRS` for production workloads needing higher IOPS)
- Binding: `WaitForFirstConsumer`

**NGINX Ingress**:
```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.0/deploy/static/provider/cloud/deploy.yaml
```

### EKS (Elastic Kubernetes Service)

**CSI Driver**: The EBS CSI driver is **NOT pre-installed** on EKS. You must install it:

```bash
# Option A: EKS managed add-on (recommended)
# 1. Create IAM role for the driver
eksctl create iamserviceaccount \
  --name ebs-csi-controller-sa \
  --namespace kube-system \
  --cluster YOUR_CLUSTER \
  --role-name AmazonEKS_EBS_CSI_DriverRole \
  --role-only \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve

# 2. Install the add-on
eksctl create addon \
  --name aws-ebs-csi-driver \
  --cluster YOUR_CLUSTER \
  --service-account-role-arn arn:aws:iam::ACCOUNT_ID:role/AmazonEKS_EBS_CSI_DriverRole \
  --force

# Option B: Helm (manual)
helm repo add aws-ebs-csi-driver https://kubernetes-sigs.github.io/aws-ebs-csi-driver
helm install aws-ebs-csi-driver aws-ebs-csi-driver/aws-ebs-csi-driver \
  --namespace kube-system \
  --set controller.serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="arn:aws:iam::ACCOUNT_ID:role/AmazonEKS_EBS_CSI_DriverRole"
```

**StorageClass** (`overlays/eks/storageclass.yaml`):
- Provisioner: `ebs.csi.aws.com`
- Type: `gp3` (encrypted by default)
- Binding: `WaitForFirstConsumer`

**NGINX Ingress**:
```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.0/deploy/static/provider/aws/deploy.yaml
```

---

## Step 5: Choose Database & Redis Strategy

### Self-Hosted

Deploys PostgreSQL 16 and Redis 7 as StatefulSets inside your cluster with persistent volumes.

**When to use**: Development, staging, cost-sensitive environments, or when you want full control.

**Configuration**:

1. Set a strong Postgres password in `components/postgres-self-hosted/secret.yaml`:
   ```yaml
   stringData:
     POSTGRES_USER: "connectk"
     POSTGRES_PASSWORD: "your-strong-password-here"
   ```

2. Update the same password in `components/postgres-self-hosted/kustomization.yaml` (the `DATABASE_URL` patch):
   ```yaml
   value: "postgresql+asyncpg://connectk:your-strong-password-here@postgres:5432/connectk"
   ```

**Storage defaults**:
- PostgreSQL: 20Gi SSD
- Redis: 5Gi SSD (AOF persistence enabled)

### Managed Services

Uses cloud-provider managed databases. No StatefulSets or PVCs are deployed.

**When to use**: Production environments where you want automated backups, failover, patching, and monitoring.

| Cloud | PostgreSQL Service | Redis Service |
|-------|--------------------|---------------|
| GKE   | CloudSQL for PostgreSQL | Memorystore for Redis |
| AKS   | Azure Database for PostgreSQL Flexible Server | Azure Cache for Redis |
| EKS   | Amazon RDS for PostgreSQL | Amazon ElastiCache for Redis |

**Configuration**:

1. Provision the managed services via your cloud console or IaC (Terraform, Pulumi, etc.)

2. Update `components/postgres-managed/kustomization.yaml` with your managed endpoint:
   ```yaml
   value: "postgresql+asyncpg://USER:PASSWORD@your-managed-host.cloud:5432/connectk"
   ```

3. Update `components/redis-managed/kustomization.yaml` with your managed endpoint:
   ```yaml
   value: "redis://your-managed-redis.cloud:6379/0"
   ```

4. Ensure the managed services are reachable from the K8s cluster (same VPC, security groups, firewall rules)

---

## Step 6: Deploy

```bash
# Preview what will be applied (dry-run)
kubectl apply -k k8s/overlays/<cloud>/<strategy>/ --dry-run=client -o yaml

# Apply
kubectl apply -k k8s/overlays/<cloud>/<strategy>/

# Examples:
kubectl apply -k k8s/overlays/eks/self-hosted/
kubectl apply -k k8s/overlays/aks/managed/
kubectl apply -k k8s/overlays/gke/self-hosted/
```

---

## Step 7: Post-Deployment Verification

```bash
# Check all resources in the connectk namespace
kubectl get all -n connectk

# Verify pods are running
kubectl get pods -n connectk -w

# Check the init container (migration) logs
kubectl logs -n connectk deployment/backend -c run-migrations

# Check backend logs
kubectl logs -n connectk deployment/backend -c backend

# Check frontend logs
kubectl logs -n connectk deployment/frontend

# Test health endpoints
kubectl exec -n connectk deployment/backend -- curl -s http://localhost:8000/api/health
kubectl exec -n connectk deployment/frontend -- wget -qO- http://localhost:3000/api/health

# Verify ingress
kubectl get ingress -n connectk
kubectl describe ingress connectk-ingress -n connectk

# If self-hosted, check database and redis
kubectl get statefulset -n connectk
kubectl get pvc -n connectk
```

**Expected healthy state**:
```
NAME                        READY   STATUS    RESTARTS
backend-6d4f5b7c8-xxxxx    1/1     Running   0
backend-6d4f5b7c8-yyyyy    1/1     Running   0
frontend-7a8b9c0d1-xxxxx   1/1     Running   0
frontend-7a8b9c0d1-yyyyy   1/1     Running   0
postgres-0                  1/1     Running   0          # self-hosted only
redis-0                     1/1     Running   0          # self-hosted only
```

---

## Storage Details

All self-hosted overlays use a StorageClass named `connectk-ssd`. The provisioner differs per cloud but the name is consistent, so StatefulSets work without cloud-specific changes.

| Property | GKE | AKS | EKS |
|----------|-----|-----|-----|
| Provisioner | `pd.csi.storage.gke.io` | `disk.csi.azure.com` | `ebs.csi.aws.com` |
| CSI driver pre-installed | Yes (1.18+) | Yes (1.21+) | **No** (manual install required) |
| Disk type | `pd-ssd` | `StandardSSD_LRS` | `gp3` |
| Encryption | Google-managed by default | Azure-managed by default | `encrypted: "true"` (EBS) |
| Volume binding | `WaitForFirstConsumer` | `WaitForFirstConsumer` | `WaitForFirstConsumer` |
| Volume expansion | Enabled | Enabled | Enabled |
| Reclaim policy | `Retain` | `Retain` | `Retain` |

**`WaitForFirstConsumer`** ensures the PV is provisioned in the same availability zone as the pod, preventing cross-AZ mount failures.

**`Retain`** means PersistentVolumes survive StatefulSet deletion — your data is preserved. To reclaim storage after intentional deletion, manually delete the PV.

### Expanding a volume

```bash
# Edit the PVC to increase storage (no downtime)
kubectl patch pvc data-postgres-0 -n connectk -p '{"spec":{"resources":{"requests":{"storage":"50Gi"}}}}'
```

---

## Scaling

### Horizontal (replicas)

The HPA automatically scales backend (2–10) and frontend (2–5) based on CPU utilization (target: 70%).

Manual override:
```bash
kubectl scale deployment/backend -n connectk --replicas=5
kubectl scale deployment/frontend -n connectk --replicas=3
```

### Vertical (resources)

Edit the resource requests/limits via a Kustomize patch in your overlay:

```yaml
# In your overlay kustomization.yaml, add:
patches:
  - target:
      kind: Deployment
      name: backend
    patch: |-
      - op: replace
        path: /spec/template/spec/containers/0/resources/requests/memory
        value: "512Mi"
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/memory
        value: "1Gi"
```

---

## Database Migrations

Migrations are handled by a Kubernetes **init container** on the backend Deployment. This means:

1. On every deployment, the init container runs `alembic upgrade head` **before** the main app starts
2. Alembic is idempotent — if the schema is already at head, it completes instantly
3. The main container only starts after migrations succeed
4. If migrations fail, the pod stays in `Init:Error` and never receives traffic

### Running migrations manually

```bash
kubectl exec -n connectk deployment/backend -c backend -- alembic upgrade head
```

### Checking migration status

```bash
kubectl exec -n connectk deployment/backend -c backend -- alembic current
kubectl exec -n connectk deployment/backend -c backend -- alembic history
```

### Rolling back a migration

```bash
kubectl exec -n connectk deployment/backend -c backend -- alembic downgrade -1
```

---

## Troubleshooting

### Pod stuck in `Init:CrashLoopBackOff`

The migration init container is failing. Check its logs:
```bash
kubectl logs -n connectk <pod-name> -c run-migrations
```

Common causes:
- Database is unreachable (wrong `DATABASE_URL`, network/firewall issue)
- Database does not exist (create the `connectk` database first)
- Migration conflict (check `alembic history` for issues)

### Pod stuck in `Pending`

```bash
kubectl describe pod -n connectk <pod-name>
```

Common causes:
- **No StorageClass**: The `connectk-ssd` StorageClass was not created. Verify with `kubectl get sc`.
- **EBS CSI driver missing** (EKS only): Install the EBS CSI driver add-on. See [EKS setup](#eks-elastic-kubernetes-service).
- **Insufficient resources**: Node doesn't have enough CPU/memory. Check with `kubectl describe node`.

### PVC stuck in `Pending`

```bash
kubectl describe pvc -n connectk <pvc-name>
```

Common causes:
- StorageClass `connectk-ssd` doesn't exist → apply the storageclass.yaml for your cloud
- CSI driver not running → check `kubectl get pods -n kube-system | grep csi`
- Quota exceeded → check cloud provider disk quotas

### Backend returns 502

The backend pods are not ready. Check:
```bash
kubectl get pods -n connectk -l app.kubernetes.io/name=backend
kubectl logs -n connectk deployment/backend -c backend
```

Common causes:
- Redis unreachable (check `REDIS_URL`)
- Backend crashed on startup (check logs for Python tracebacks)

### Frontend shows blank page

```bash
kubectl logs -n connectk deployment/frontend
```

Common causes:
- `NEXT_PUBLIC_API_URL` is wrong — should point to `http://backend.connectk.svc.cluster.local:8000`
- Backend is not reachable from frontend pods — verify with:
  ```bash
  kubectl exec -n connectk deployment/frontend -- wget -qO- http://backend:8000/api/health
  ```

### Ingress has no ADDRESS

```bash
kubectl get ingress -n connectk
kubectl describe ingress connectk-ingress -n connectk
```

Common causes:
- NGINX Ingress Controller is not installed
- Cloud load balancer is still provisioning (wait 2–3 minutes)
- Incorrect `ingressClassName` — verify with `kubectl get ingressclass`

---

## CI/CD Pipeline

### Overview

```
  Developer pushes code
          │
          ▼
  ┌───────────────────────────────────────────────────┐
  │              GitHub Actions (CI)                   │
  │                                                    │
  │  ┌──────────────────┐   ┌───────────────────────┐ │
  │  │  ci-backend.yaml │   │  ci-frontend.yaml     │ │
  │  │                  │   │                        │ │
  │  │  1. Lint         │   │  1. Lint + type-check  │ │
  │  │  2. Build image  │   │  2. Build image        │ │
  │  │  3. Push to GHCR │   │  3. Push to GHCR       │ │
  │  │  4. Update tag   │   │  4. Update tag         │ │
  │  └──────────────────┘   └───────────────────────┘ │
  │           │                        │               │
  │           └───── commit new ───────┘               │
  │                  image tags                        │
  │                  to k8s/overlays/                  │
  └───────────────────────┬───────────────────────────┘
                          │
                   Git push [skip ci]
                          │
                          ▼
  ┌───────────────────────────────────────────────────┐
  │                ArgoCD (CD)                         │
  │                                                    │
  │  1. Detects kustomization.yaml changed             │
  │  2. Runs kustomize build on the overlay            │
  │  3. Diffs desired state vs live cluster state      │
  │  4. Applies only the changed resources             │
  │  5. Waits for rollout to complete                  │
  └───────────────────────────────────────────────────┘
```

Key properties:
- **Independent pipelines** — backend and frontend have separate CI workflows triggered by path filters. Changing `backend/` only rebuilds the backend.
- **GitOps** — the Git repo is the single source of truth. ArgoCD never applies anything that isn't committed.
- **Registry-agnostic** — defaults to GHCR but switchable to ECR/ACR/GAR via one GitHub variable.
- **Auto-sync with safety** — ArgoCD auto-syncs, prunes deleted resources, and self-heals manual cluster changes.

### GitHub Actions — CI

Two independent workflows live in `.github/workflows/`:

| Workflow | Triggers on | What it does |
|----------|-------------|--------------|
| `ci-backend.yaml` | Push to `main` changing `backend/**` | Lint → Build Docker image → Push to registry → Update K8s manifests |
| `ci-frontend.yaml` | Push to `main` changing `frontend/**` | Lint + type-check → Build Docker image → Push to registry → Update K8s manifests |

Both workflows also run lint/type-check on **pull requests** (without building or pushing images).

#### Image tagging strategy

Every push to `main` produces three tags:

| Tag | Example | Purpose |
|-----|---------|---------|
| `sha-<short>` | `sha-a1b2c3d` | Immutable, traceable to exact commit |
| `main` | `main` | Latest from main branch |
| `latest` | `latest` | Convenience alias |

The `sha-*` tag is what gets written into the kustomization overlay — this guarantees reproducible deployments.

#### GitHub repository setup

**Required secrets** (set in GitHub → Settings → Secrets and variables → Actions):

For GHCR (default), no extra secrets needed — `GITHUB_TOKEN` is automatic.

For other registries:

| Registry | Secrets needed |
|----------|---------------|
| ECR | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| ACR | `ACR_USERNAME`, `ACR_PASSWORD` |
| GAR | `GCP_SA_KEY` (service account JSON) |

**Required variables** (set in GitHub → Settings → Secrets and variables → Actions → Variables):

| Variable | Example | Required? |
|----------|---------|-----------|
| `REGISTRY` | `ghcr.io` (default), `ACCOUNT.dkr.ecr.REGION.amazonaws.com`, `YOUR_ACR.azurecr.io` | No (defaults to GHCR) |
| `DEPLOY_OVERLAY` | `eks/self-hosted`, `aks/managed`, `gke/self-hosted` | No (skip manifest update if empty) |

> **Note**: If `DEPLOY_OVERLAY` is not set, the CI will build and push images but will NOT auto-update the K8s manifests. This is useful if you prefer to update image tags manually or via a separate process.

### ArgoCD — CD

#### Prerequisites

ArgoCD must be installed on your target cluster:

```bash
# Install ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for it to be ready
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=120s

# Get the initial admin password
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d

# Port-forward to access the UI
kubectl port-forward svc/argocd-server -n argocd 8080:443
# Open https://localhost:8080, login as admin with the password above
```

#### Connect your Git repository

```bash
# Install the ArgoCD CLI
# macOS: brew install argocd
# Linux: curl -sSL -o argocd https://github.com/argoproj/argo-cd/releases/latest/download/argocd-linux-amd64 && chmod +x argocd && sudo mv argocd /usr/local/bin/

# Login
argocd login localhost:8080

# Add your repo (HTTPS with token)
argocd repo add https://github.com/MahammadRafi06/connectk.git \
  --username git \
  --password <GITHUB_PAT>

# Or add with SSH
argocd repo add git@github.com:MahammadRafi06/connectk.git \
  --ssh-private-key-path ~/.ssh/id_ed25519
```

#### Deploy the ArgoCD resources

```bash
# 1. Create the AppProject (sets RBAC boundaries)
kubectl apply -f k8s/argocd/project.yaml

# 2. Create the ApplicationSet (generates one ArgoCD Application per cloud/strategy)
kubectl apply -f k8s/argocd/applicationset.yaml
```

#### Configure which environments to deploy

Edit `k8s/argocd/applicationset.yaml` and uncomment the environments you want:

```yaml
generators:
  - list:
      elements:
        # Uncomment your target:
        # - cloud: gke
        #   strategy: self-hosted
        - cloud: eks
          strategy: self-hosted
        # - cloud: aks
        #   strategy: managed
```

Each uncommented entry creates a separate ArgoCD Application (e.g., `connectk-eks-self-hosted`).

#### Verify ArgoCD sync

```bash
# List applications
argocd app list

# Check sync status
argocd app get connectk-eks-self-hosted

# Manually trigger a sync (if auto-sync is disabled)
argocd app sync connectk-eks-self-hosted

# View sync history
argocd app history connectk-eks-self-hosted
```

#### Sync policy

The ApplicationSet is configured with:

| Setting | Value | Effect |
|---------|-------|--------|
| `automated.prune` | `true` | Resources removed from Git are deleted from the cluster |
| `automated.selfHeal` | `true` | Manual cluster changes are reverted to match Git |
| `retry.limit` | `3` | Failed syncs are retried up to 3 times with exponential backoff |
| `ignoreDifferences` | `/spec/replicas` | HPA-managed replica counts are not overwritten by ArgoCD |

To **disable auto-sync** for production (manual approval required), remove the `automated` block:

```yaml
syncPolicy:
  # automated:           # ← comment out or remove
  #   prune: true
  #   selfHeal: true
  syncOptions:
    - CreateNamespace=true
```

### Switching Container Registries

The CI pipelines default to **GHCR** (GitHub Container Registry) for maximum portability. To switch to a client-specific registry, only two things change:

#### Switch to Amazon ECR

1. Set GitHub repo variable: `REGISTRY` = `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com`
2. Add GitHub secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
3. Update the overlay `kustomization.yaml` image `newName` to match

#### Switch to Azure ACR

1. Set GitHub repo variable: `REGISTRY` = `YOUR_ACR.azurecr.io`
2. Add GitHub secrets: `ACR_USERNAME`, `ACR_PASSWORD`
3. Update the overlay `kustomization.yaml` image `newName` to match

#### Switch to Google Artifact Registry

1. Set GitHub repo variable: `REGISTRY` = `REGION-docker.pkg.dev/PROJECT_ID/connectk`
2. Add GitHub secret: `GCP_SA_KEY` (service account JSON key)
3. Update the overlay `kustomization.yaml` image `newName` to match

> **Tip**: If the cluster can't pull from GHCR (private network), create an `imagePullSecret` in the `connectk` namespace and reference it in the Deployments. Or use a cloud-native registry that's on the same network as the cluster.

### End-to-End Flow

Here is the complete flow from code change to live deployment:

```
1.  Developer pushes backend fix to a feature branch
2.  GitHub Actions runs ci-backend.yaml → lint only (no image push on PRs)
3.  Developer merges PR into main
4.  GitHub Actions runs ci-backend.yaml on main:
      a. Lint passes
      b. Builds Docker image from backend/Dockerfile
      c. Pushes to ghcr.io/mahammadRafi06/connectk-backend:sha-a1b2c3d
      d. Runs kustomize edit set image in k8s/overlays/eks/self-hosted/
      e. Commits: "ci: update backend image to sha-a1b2c3d [skip ci]"
      f. Pushes to main (the [skip ci] prevents infinite loop)
5.  ArgoCD detects the new commit (polls every 3 min, or webhook for instant)
6.  ArgoCD runs kustomize build on k8s/overlays/eks/self-hosted/
7.  ArgoCD diffs: only the backend Deployment image changed
8.  ArgoCD applies the updated Deployment
9.  Kubernetes performs a rolling update:
      a. New backend pod starts with init container → runs alembic upgrade head
      b. Migration completes (already at head → instant)
      c. Main container starts, passes readiness probe
      d. Old pod is terminated
10. ArgoCD marks the sync as "Healthy"
```

**Frontend changes follow the same flow** independently via `ci-frontend.yaml`. Both can run in parallel without interfering.

#### Setting up ArgoCD webhooks (optional, for instant sync)

By default ArgoCD polls Git every 3 minutes. For instant sync on push:

```bash
# In your GitHub repo → Settings → Webhooks → Add webhook:
#   Payload URL: https://argocd.yourdomain.com/api/webhook
#   Content type: application/json
#   Secret: <your-webhook-secret>
#   Events: Just the push event

# Configure the secret in ArgoCD:
kubectl edit secret argocd-secret -n argocd
# Add: webhook.github.secret: <base64-encoded-secret>
```
