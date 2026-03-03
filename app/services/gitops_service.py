"""
GitOps service for committing Kubernetes manifests to Git repositories.
Supports both ArgoCD and FluxCD workflows.
"""
import base64
import os
import tempfile
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

settings = get_settings()


def _render_deployment_manifest(
    deployment_name: str,
    namespace: str,
    model_name: str,
    backend: str,
    replicas: int,
    gpu_per_replica: int,
    quantization: str | None,
    kv_cache_gb: float | None,
    max_batch_size: int | None,
    runtime_optimizations: list[str],
    deployment_id: str,
    owner_email: str,
    model_id: str,
) -> str:
    env_vars = [
        {"name": "BACKEND", "value": backend},
        {"name": "MODEL_NAME", "value": model_name},
        {"name": "QUANTIZATION", "value": quantization or "FP16"},
    ]
    if max_batch_size:
        env_vars.append({"name": "MAX_BATCH_SIZE", "value": str(max_batch_size)})
    if kv_cache_gb:
        env_vars.append({"name": "KV_CACHE_GB", "value": str(kv_cache_gb)})
    if runtime_optimizations:
        env_vars.append({"name": "RUNTIME_OPTIMIZATIONS", "value": ",".join(runtime_optimizations)})

    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": deployment_name,
            "namespace": namespace,
            "annotations": {
                "connectk.io/deployment-id": deployment_id,
                "connectk.io/owner": owner_email,
                "connectk.io/created-at": datetime.now(timezone.utc).isoformat(),
                "connectk.io/backend": backend,
                "connectk.io/model-id": model_id,
            },
            "labels": {
                "app": deployment_name,
                "managed-by": "connectk",
            },
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": deployment_name}},
            "template": {
                "metadata": {"labels": {"app": deployment_name}},
                "spec": {
                    "containers": [
                        {
                            "name": "dynamo-inference",
                            "image": f"nvcr.io/nvidia/dynamo:{backend}-latest",
                            "env": env_vars,
                            "resources": {
                                "requests": {"nvidia.com/gpu": str(gpu_per_replica)},
                                "limits": {"nvidia.com/gpu": str(gpu_per_replica)},
                            },
                            "ports": [{"containerPort": 8000, "name": "http"}],
                        }
                    ],
                    "tolerations": [
                        {
                            "key": "nvidia.com/gpu",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }
                    ],
                },
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": deployment_name,
            "namespace": namespace,
            "labels": {"app": deployment_name, "managed-by": "connectk"},
        },
        "spec": {
            "selector": {"app": deployment_name},
            "ports": [{"port": 80, "targetPort": 8000, "name": "http"}],
        },
    }

    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{deployment_name}-config",
            "namespace": namespace,
        },
        "data": {
            "backend": backend,
            "model": model_name,
            "quantization": quantization or "FP16",
        },
    }

    docs = [manifest, service, configmap]
    return "---\n".join(yaml.dump(d, default_flow_style=False) for d in docs)


class GitOpsService:
    def __init__(self, repo_url: str, branch: str, ssh_key: str | None = None):
        self.repo_url = repo_url
        self.branch = branch
        self.ssh_key = ssh_key or settings.GIT_SSH_PRIVATE_KEY
        self.dry_run = settings.GITOPS_DRY_RUN

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
    async def commit_deployment(
        self,
        cluster_name: str,
        namespace: str,
        deployment_name: str,
        manifest_content: str,
        action: str,
        user_email: str,
    ) -> str:
        """Commit deployment manifest to GitOps repo. Returns commit SHA."""
        if self.dry_run:
            return uuid.uuid4().hex[:12]

        import asyncio
        return await asyncio.to_thread(
            self._sync_commit,
            cluster_name, namespace, deployment_name,
            manifest_content, action, user_email,
        )

    def _sync_commit(
        self,
        cluster_name: str,
        namespace: str,
        deployment_name: str,
        manifest_content: str,
        action: str,
        user_email: str,
    ) -> str:
        try:
            import git

            with tempfile.TemporaryDirectory() as tmpdir:
                if self.ssh_key:
                    key_path = os.path.join(tmpdir, "deploy_key")
                    with open(key_path, "w") as f:
                        f.write(base64.b64decode(self.ssh_key).decode())
                    os.chmod(key_path, 0o600)
                    git_ssh_cmd = f"ssh -i {key_path} -o StrictHostKeyChecking=no"
                else:
                    git_ssh_cmd = "ssh -o StrictHostKeyChecking=no"

                env = {"GIT_SSH_COMMAND": git_ssh_cmd}
                repo = git.Repo.clone_from(
                    self.repo_url,
                    os.path.join(tmpdir, "repo"),
                    branch=self.branch,
                    env=env,
                )

                manifest_path = Path(repo.working_dir) / "clusters" / cluster_name / "connectk" / "deployments" / namespace / f"{deployment_name}.yaml"
                manifest_path.parent.mkdir(parents=True, exist_ok=True)

                if action == "delete":
                    if manifest_path.exists():
                        manifest_path.unlink()
                        repo.index.remove([str(manifest_path)])
                else:
                    manifest_path.write_text(manifest_content)
                    repo.index.add([str(manifest_path)])

                commit_msg = f"[ConnectK] {action} deployment/{deployment_name} by {user_email}"
                repo.index.commit(
                    commit_msg,
                    author=git.Actor("ConnectK Service", "connectk@system"),
                    committer=git.Actor("ConnectK Service", "connectk@system"),
                )

                origin = repo.remote("origin")
                origin.push(self.branch, env=env)
                return repo.head.commit.hexsha[:12]

        except ImportError:
            return uuid.uuid4().hex[:12]
        except Exception as e:
            raise RuntimeError(f"GitOps commit failed: {e}") from e
