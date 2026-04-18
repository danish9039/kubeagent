# KubeAgent 🤖

**Autonomous MLOps Engineer Agent** — monitors Kubeflow Pipelines, detects failures, correlates with MLflow experiments, and auto-generates GitHub PR fixes using Claude claude-sonnet-4-5.

> Built for the Anthropic Hackathon 2026

---

## What It Does

KubeAgent runs as a daemon that:

1. **Polls** Kubeflow Pipelines every N minutes for failed runs
2. **Fetches** Kubernetes pod logs and events for each failure
3. **Correlates** failures with MLflow experiment metrics (accuracy degradation, loss spikes)
4. **Reasons** using Claude claude-sonnet-4-5 with 8 structured tools via `tool_use`
5. **Acts** — generates a YAML patch, opens a GitHub PR, writes an incident report
6. **Remembers** — persists all findings to `agent_memory.json` across cycles

```
KFP API ──────► KubeAgent (Claude claude-sonnet-4-5) ──────► GitHub PR
K8s Events ──►  [classify → patch → report]       ──────► Incident Report
MLflow ───────►                                    ──────► agent_memory.json
```

---

## Failure Types Detected & Fixed

| Failure | Detection | Auto-Fix |
|---|---|---|
| **OOM Kill** | `OOMKilled`, exit code 137 | Memory limit ×4 in pod spec |
| **Wrong Image Tag** | `ImagePullBackOff`, `ErrImagePull` | Tag → `latest` |
| **Missing Env Var** | `KeyError`, `EnvironmentError` | Add env placeholder to manifest |
| **CrashLoopBackOff** | >3 restarts, `CrashLoopBackOff` | Add resource limits + probe delay |
| **Resource Quota** | `Insufficient cpu/memory` | Reduce requests, add namespace hint |
| **Timeout** | `DeadlineExceeded`, context timeout | Increase activeDeadlineSeconds |
| **Dependency Fail** | Upstream step failed | Surface root cause in report |

---

## Project Structure

```
kubeagent/
├── agent/
│   ├── core.py          # Main Claude tool_use agent loop
│   ├── memory.py        # Atomic JSON persistent memory
│   └── tools.py         # 8 tool definitions + dispatch router
├── connectors/
│   ├── kfp_client.py    # Kubeflow Pipelines API wrapper
│   ├── mlflow_client.py # MLflow tracking client wrapper
│   ├── k8s_client.py    # Kubernetes pod/events client
│   └── github_client.py # GitHub PR creator (PyGithub)
├── reasoning/
│   ├── classifier.py    # Regex-based failure classifier
│   ├── patch_gen.py     # YAML patch generator
│   └── reporter.py      # Markdown incident report generator
├── config/settings.py   # All env vars with defaults
├── demo/
│   └── broken_pipeline.py  # Demo: 3 intentional KFP failures
├── tests/test_agent.py  # 44 unit tests
├── memory/
│   └── agent_memory.json
├── BLUEPRINT.md         # Full engineering architecture doc
├── SETUP.md             # Local kind cluster setup guide
└── Dockerfile
```

---

## Quick Start

### 1. Prerequisites
- Docker + kind
- Python 3.11+
- Anthropic API key
- GitHub Personal Access Token

### 2. Setup local cluster
```bash
# See SETUP.md for full instructions
kind create cluster --name kubeagent
# Install KFP + MLflow (see SETUP.md)
```

### 3. Configure
```bash
cp .env.example .env
# Edit .env with your keys:
# ANTHROPIC_API_KEY=sk-ant-...
# GITHUB_TOKEN=ghp_...
# KFP_ENDPOINT=http://localhost:8887
# MLFLOW_TRACKING_URI=http://localhost:5000
```

### 4. Install & run
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m kubeagent.agent.core
```

### 5. Trigger the demo
```bash
# In another terminal — submits a broken KFP pipeline
python demo/broken_pipeline.py
```

KubeAgent will detect all 3 failures and open GitHub PRs within one poll cycle.

---

## Claude Tool Definitions

The agent exposes 8 tools to Claude:

| Tool | Description |
|---|---|
| `get_failed_pipeline_runs` | Query KFP for FAILED runs in last N hours |
| `get_run_logs` | Fetch pod logs for a run ID |
| `get_mlflow_experiments` | Get recent MLflow runs + metrics |
| `get_k8s_pod_events` | Kubernetes pod Warning events |
| `generate_yaml_patch` | Produce fixed YAML manifest |
| `create_github_pr` | Open PR with the patch |
| `write_incident_report` | Generate markdown incident summary |
| `update_memory` | Persist findings to agent_memory.json |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required** |
| `KFP_ENDPOINT` | `http://localhost:8887` | KFP API server |
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | MLflow server |
| `GITHUB_TOKEN` | — | **Required** for PR creation |
| `GITHUB_REPO` | — | `owner/repo` to push fixes to |
| `POLL_INTERVAL_SECONDS` | `300` | How often to poll |
| `LOOKBACK_HOURS` | `1` | How far back to look for failures |
| `CLAUDE_MODEL` | `claude-sonnet-4-5` | Anthropic model |

See `config/settings.py` for the full list.

---

## Running Tests

```bash
pytest tests/ -v
# 44 tests covering classifier, patch gen, memory, connectors
```

---

## Architecture

See [BLUEPRINT.md](./BLUEPRINT.md) for the full engineering document

For visual diagrams (architecture, sequence, state machines, class diagram), see [DIAGRAMS.md](./DIAGRAMS.md) including:
- ASCII component diagrams
- Agent loop pseudocode
- Complete tool JSON schemas
- Failure classification rules
- Memory schema
- 3-day hackathon milestone plan

---

## Demo Scenario

`demo/broken_pipeline.py` creates a KFP pipeline with 3 intentional failures:

```python
# Failure 1: OOM — 50Mi memory limit for an ~800MB numpy operation
preprocess_task.set_memory_limit("50Mi")

# Failure 2: Wrong image tag — nonexistent tag triggers ImagePullBackOff  
base_image="tensorflow/tensorflow:2.99.99-gpu"

# Failure 3: Missing env var — KeyError at runtime
s3_bucket = os.environ["MODEL_REGISTRY_BUCKET"]  # not set
```

KubeAgent detects all three, generates patches, and opens PRs automatically.

---

## License

Apache 2.0
