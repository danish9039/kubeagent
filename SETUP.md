# KubeAgent Local Development Setup

This guide walks you through spinning up a complete local environment — kind cluster, Kubeflow Pipelines, MLflow, and KubeAgent — to run the broken-pipeline demo end-to-end.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Linux or macOS | Docker Desktop also works on macOS |
| Docker Engine 24+ | `docker --version` to verify |
| `kubectl` | `brew install kubectl` or [official install](https://kubernetes.io/docs/tasks/tools/) |
| Git | `git --version` |
| Python 3.11+ | `python3 --version` |
| GitHub account | A repo you own — KubeAgent will open PRs there |
| Anthropic API key | From [console.anthropic.com](https://console.anthropic.com) |

---

## Step 1: Install kind and Create Local Cluster

[kind](https://kind.sigs.k8s.io/) (Kubernetes IN Docker) provides a lightweight local cluster without needing a cloud provider.

```bash
# Install kind (Linux x86-64)
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.22.0/kind-linux-amd64
chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind

# macOS (arm64)
# curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.22.0/kind-darwin-arm64
# chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind

# Create cluster with extra port mappings for KFP UI and API
cat <<EOF | kind create cluster --name kubeagent --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 31380
    hostPort: 8888
    protocol: TCP
  - containerPort: 31390
    hostPort: 8887
    protocol: TCP
EOF

kubectl cluster-info --context kind-kubeagent
```

Expected output:
```
Kubernetes control plane is running at https://127.0.0.1:<port>
CoreDNS is running at https://127.0.0.1:<port>/api/v1/namespaces/...
```

---

## Step 2: Install Kubeflow Pipelines Standalone

This installs the KFP backend (API server, Argo Workflows, MySQL, MinIO) into a `kubeflow` namespace.

```bash
export PIPELINE_VERSION=2.2.0

# Install cluster-scoped resources first
kubectl apply -k \
  "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=$PIPELINE_VERSION"

kubectl wait --for condition=established --timeout=60s \
  crd/applications.app.k8s.io

# Install the platform-agnostic (no Istio) environment
kubectl apply -k \
  "github.com/kubeflow/pipelines/manifests/kustomize/env/platform-agnostic-pns?ref=$PIPELINE_VERSION"

# Wait until all pods are ready (can take 3–5 minutes on first pull)
kubectl wait --for=condition=ready pod --all -n kubeflow --timeout=300s

# Port-forward KFP UI and API (run in background)
kubectl port-forward svc/ml-pipeline-ui 8888:80 -n kubeflow &
kubectl port-forward svc/ml-pipeline 8887:8888 -n kubeflow &

echo "KFP UI:  http://localhost:8888"
echo "KFP API: http://localhost:8887"
```

> **Tip:** If `kubectl wait` times out, run `kubectl get pods -n kubeflow` to see which pods are still initializing. Large images (Argo, MinIO) can take longer on a slow connection.

---

## Step 3: Install MLflow via Docker Compose

MLflow tracks experiment metrics. KubeAgent correlates degrading MLflow metrics with KFP run failures.

```bash
cat > docker-compose.yml << 'EOF'
version: '3.8'
services:
  mlflow:
    image: ghcr.io/mlflow/mlflow:v2.12.0
    ports:
      - "5000:5000"
    command: mlflow server --host 0.0.0.0 --port 5000
    volumes:
      - mlflow-data:/mlflow
volumes:
  mlflow-data:
EOF

docker compose up -d mlflow

echo "MLflow UI: http://localhost:5000"
```

Verify it is running:
```bash
curl -s http://localhost:5000/health
# Expected: OK
```

---

## Step 4: Set Environment Variables

```bash
cat > .env << 'EOF'
# Kubeflow Pipelines
KFP_ENDPOINT=http://localhost:8887
KFP_NAMESPACE=kubeflow

# MLflow
MLFLOW_TRACKING_URI=http://localhost:5000

# GitHub — create a Personal Access Token with "repo" scope at
# https://github.com/settings/tokens/new
GITHUB_TOKEN=ghp_your_token_here
GITHUB_REPO=your-username/your-test-repo

# Anthropic
ANTHROPIC_API_KEY=sk-ant-your-key-here
CLAUDE_MODEL=claude-sonnet-4-5

# Agent Configuration
POLL_INTERVAL_SECONDS=60
LOOKBACK_HOURS=2
MEMORY_PATH=memory/agent_memory.json
REPORTS_DIR=reports

# Kubernetes — set to false when using a local kubeconfig (kind)
K8S_IN_CLUSTER=false

# Optional: Slack webhook for failure notifications
# SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
EOF

# Load all variables into current shell session
export $(grep -v '^#' .env | xargs)

echo "Environment loaded."
```

> **Security note:** Never commit `.env` to version control. It is already listed in `.gitignore`.

---

## Step 5: Install Python Dependencies

```bash
python3 -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install --upgrade pip

pip install -r requirements.txt

# Verify key packages
python -c "import kfp; print('KFP:', kfp.__version__)"
python -c "import mlflow; print('MLflow:', mlflow.__version__)"
python -c "import anthropic; print('Anthropic SDK: OK')"
python -c "from kubernetes import client; print('K8s client: OK')"
```

All four lines should print without errors. If any import fails, check that `requirements.txt` was installed correctly and that you are inside the `venv`.

---

## Step 6: Run the Agent

```bash
# Point kubectl at the kind cluster
kubectl config use-context kind-kubeagent

# Start KubeAgent (uses .env values already exported)
python -m kubeagent.agent.core

# Alternatively, pass variables inline without loading .env:
KFP_ENDPOINT=http://localhost:8887 \
MLFLOW_TRACKING_URI=http://localhost:5000 \
ANTHROPIC_API_KEY=sk-ant-xxx \
GITHUB_TOKEN=ghp_xxx \
GITHUB_REPO=your-username/your-test-repo \
python -m kubeagent.agent.core
```

Expected startup output:
```
2026-04-18 10:00:00 INFO  KubeAgent starting...
2026-04-18 10:00:01 INFO  Memory loaded: 0 processed runs
2026-04-18 10:00:02 INFO  Polling KFP for failed runs in last 1 hour(s)...
2026-04-18 10:00:03 INFO  Found 0 failed runs. Sleeping 60s...
```

The agent polls every `POLL_INTERVAL_SECONDS` seconds. Leave it running in a dedicated terminal.

---

## Step 7: Run the Demo

Open a **new terminal**, activate the virtualenv, and submit the intentionally broken pipeline:

```bash
source venv/bin/activate
export $(grep -v '^#' .env | xargs)

python demo/broken_pipeline.py
```

Expected output:
```
INFO  MLflow run logged: 3f8a1c9d2e7b...
INFO  Pipeline submitted. Run ID: abc123-def456-...
INFO  This run will fail with 3 intentional errors:
INFO    1. OOM Kill in data_preprocessing (50Mi memory limit)
INFO    2. ImagePullBackOff in model_training (nonexistent image tag)
INFO    3. Missing env var KeyError in model_evaluation
INFO  KubeAgent will detect and auto-fix these failures!
Run ID: abc123-def456-...
```

Within one poll interval (≤ 60 seconds), KubeAgent will:

1. Detect all 3 failures via the KFP API and Kubernetes pod events
2. Correlate them with the degrading MLflow accuracy metrics
3. Generate YAML patches for each fix
4. Open GitHub PRs containing the patches
5. Write incident reports to the `reports/` directory

---

## Step 8: Verify KubeAgent Actions

```bash
# List generated incident reports
ls -la reports/

# Inspect agent memory (persisted state between restarts)
cat memory/agent_memory.json | python3 -m json.tool

# Watch live KubeAgent output and save to a log file simultaneously
python -m kubeagent.agent.core 2>&1 | tee kubeagent.log
```

Check your GitHub repository — KubeAgent should have opened one PR per detected failure, each containing:
- A description of the failure and root cause
- The YAML patch (e.g., updated memory limit, corrected image tag, added env var)
- A link to the incident report

---

## Running Tests

```bash
# Run the full test suite
pytest tests/ -v

# Run only the failure classifier tests
pytest tests/test_agent.py::TestFailureClassifier -v

# Run with coverage report
pytest tests/ --cov=kubeagent --cov-report=term-missing
```

---

## Troubleshooting

### KFP API not reachable (`Connection refused` on port 8887)

The port-forward process may have exited. Re-run:
```bash
kubectl port-forward svc/ml-pipeline 8887:8888 -n kubeflow &
```

Check pod health first:
```bash
kubectl get pods -n kubeflow
# All pods should show STATUS=Running and READY=1/1 (or n/n)
```

### MLflow connection refused (port 5000)

```bash
docker compose ps         # verify mlflow container is Up
docker compose logs mlflow  # check for startup errors
docker compose up -d mlflow # restart if needed
```

### ImagePullBackOff on demo components (expected)

The `model_training` component uses `tensorflow/tensorflow:2.99.99-gpu`, which does not exist — that is the intentional failure KubeAgent is meant to detect. Do not attempt to pull or fix this image manually; let KubeAgent handle it.

### GitHub PR creation fails (403 Forbidden)

Verify your token has the `repo` scope:
```bash
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GITHUB_REPO | jq .permissions
# Should show: "push": true
```

### kind cluster lost after Docker restart

kind clusters do not survive Docker daemon restarts by default. Recreate with:
```bash
kind create cluster --name kubeagent --config=<(cat <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 31380
    hostPort: 8888
    protocol: TCP
  - containerPort: 31390
    hostPort: 8887
    protocol: TCP
EOF
)
```

Then re-run Steps 2 and 6.

### Anthropic API errors (`authentication_error`)

Double-check that `ANTHROPIC_API_KEY` starts with `sk-ant-` and is exported in the current shell:
```bash
echo $ANTHROPIC_API_KEY
```

---

## Architecture Quick Reference

```
┌─────────────────────────────────────────────────────────────────┐
│                        Local Machine                            │
│                                                                 │
│  ┌─────────────────┐          ┌──────────────────────────────┐  │
│  │  broken_pipeline│          │       KubeAgent              │  │
│  │  .py (demo)     │──submit──▶  kubeagent/agent/core.py     │  │
│  └─────────────────┘          │                              │  │
│                               │  1. Poll KFP API             │  │
│  ┌─────────────────┐          │  2. Fetch K8s pod events     │  │
│  │  MLflow :5000   │◀─metrics─│  3. Read MLflow metrics      │  │
│  │  (Docker)       │          │  4. Claude analysis          │  │
│  └─────────────────┘          │  5. Generate YAML patches    │  │
│                               │  6. Open GitHub PRs          │  │
│  ┌─────────────────────────┐  │  7. Write incident reports   │  │
│  │  kind cluster           │  └──────────────────────────────┘  │
│  │  ┌───────────────────┐  │          │            │             │
│  │  │ kubeflow namespace│  │          │            │             │
│  │  │  KFP API :8887    │◀─┼──poll────┘     ┌─────▼──────┐     │
│  │  │  KFP UI  :8888    │  │                │  GitHub    │     │
│  │  │  Argo Workflows   │  │                │  PRs       │     │
│  │  │  ML Pipeline Pods │  │                └────────────┘     │
│  │  └───────────────────┘  │                                   │
│  └─────────────────────────┘                                   │
└─────────────────────────────────────────────────────────────────┘
```

**Data flow summary:**

1. `broken_pipeline.py` logs degrading metrics to MLflow and submits the broken pipeline to KFP.
2. KFP schedules the pipeline steps; Kubernetes pods fail (OOM / ImagePullBackOff / KeyError).
3. KubeAgent polls the KFP API, detects failed runs, and fetches Kubernetes pod events for context.
4. KubeAgent calls Claude (Anthropic) with all the failure context to diagnose root causes.
5. Claude generates targeted YAML patches (memory limit bump, corrected image tag, env var injection).
6. KubeAgent opens a GitHub PR per failure and writes a markdown incident report to `reports/`.
