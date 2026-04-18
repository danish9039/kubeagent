# KubeAgent Engineering Blueprint

> **Version:** 1.0.0 — Hackathon Reference Document  
> **Model:** Claude claude-sonnet-4-5 (tool_use API)  
> **Status:** Pre-implementation design specification

---

## 1. Project Summary

KubeAgent is an autonomous MLOps engineer agent that continuously monitors a Kubeflow Pipelines deployment, detects pipeline and training job failures, and resolves them without human intervention. Powered by Claude claude-sonnet-4-5's tool_use API, the agent follows an observe-reason-act loop: it polls KFP for failed runs, fetches pod logs and Kubernetes events, correlates those failures with MLflow experiment metrics, classifies the root cause, generates a YAML patch fix, and opens a GitHub pull request — all autonomously. The agent maintains persistent memory across polling cycles so it never re-processes the same failure twice, and it writes structured incident reports for every event it handles.

What makes KubeAgent novel is the cross-system correlation layer. Traditional MLOps monitoring tools alert humans to failures; KubeAgent closes the loop. By feeding raw evidence — pod OOM events, container exit codes, missing environment variables, image pull errors — together with correlated MLflow metric trajectories directly into Claude claude-sonnet-4-5's reasoning context, the agent can distinguish between a transient scheduling failure and a systematic hyperparameter misconfiguration. Claude then chooses from a typed toolset to generate the minimum-diff YAML patch that fixes the problem and submits it as a reviewable PR, keeping humans in the loop for final approval without requiring them to diagnose the issue at all.

KubeAgent is scoped for hackathon delivery in three days. The architecture is deliberately lean: a single polling process, a flat JSON memory store, and ten well-defined Python modules. Every external system interaction goes through a typed tool definition that Claude can call, making the reasoning trace fully auditable. The design is intentionally extensible — Slack notifications, Prometheus alerting, and multi-cluster support are identified as post-hackathon scope but do not compromise the core loop.

---

## 2. System Architecture

### 2.1 Component Diagram

#### External Systems View

```
┌─────────────────┐     poll every N min    ┌──────────────────────────────────┐
│  KFP API Server │ ◄────────────────────── │                                  │
│  :8888          │ ─────run data──────────►│         KubeAgent Core           │
└─────────────────┘                         │     (Claude claude-sonnet-4-5 agent loop)    │
                                            │                                  │
┌─────────────────┐                         │  ┌──────────┐  ┌─────────────┐  │
│  MLflow Server  │ ◄────────────────────── │  │  memory  │  │  tools.py   │  │
│  :5000          │ ─────metrics──────────►│  │  .json   │  │ (tool_use)  │  │
└─────────────────┘                         │  └──────────┘  └─────────────┘  │
                                            │                                  │
┌─────────────────┐                         │  ┌──────────┐  ┌─────────────┐  │
│  Kubernetes API │ ◄────────────────────── │  │classifier│  │ patch_gen   │  │
│  (pod events)   │ ─────pod logs─────────►│  └──────────┘  └─────────────┘  │
└─────────────────┘                         └──────────────────────────────────┘
                                                            │
                                            ┌───────────────▼──────────────────┐
                                            │         GitHub API               │
                                            │    (PR with YAML patch fix)      │
                                            └──────────────────────────────────┘
```

#### Internal Module Diagram

```
kubeagent/
│
├── agent/
│   ├── core.py          ← Main agent loop, Claude API calls, tool dispatch
│   ├── memory.py        ← Read/write agent_memory.json, dedup logic
│   └── tools.py         ← Tool schema definitions (JSON) + dispatcher
│
├── connectors/
│   ├── kfp_client.py    ← Wraps kfp.Client: list failed runs, retry
│   ├── mlflow_client.py ← Wraps MlflowClient: search runs, get metrics
│   ├── k8s_client.py    ← Wraps kubernetes.client: pod logs, events
│   └── github_client.py ← Wraps PyGithub: create branch, commit, PR
│
├── reasoning/
│   ├── classifier.py    ← Regex + heuristic failure classification
│   ├── patch_gen.py     ← YAML diff generation for each failure type
│   └── reporter.py      ← Incident report markdown renderer
│
├── reports/             ← Auto-generated incident reports (gitignored)
├── agent_memory.json    ← Persistent state across polling cycles
├── config.py            ← Env var loading + defaults
└── main.py              ← Entrypoint: scheduler + loop invocation
```

```
                    ┌──────────────────────────────────────────────────────────┐
                    │                    agent/core.py                         │
                    │                                                          │
                    │  run_agent_cycle()                                       │
                    │    │                                                     │
                    │    ├─► memory.load()           ──► agent_memory.json    │
                    │    │                                                     │
                    │    ├─► kfp_client.get_failed_runs()                     │
                    │    │        └─► [run_id, pipeline_name, error_msg ...]   │
                    │    │                                                     │
                    │    ├─► For each unprocessed run:                        │
                    │    │     ├─► kfp_client.get_run_logs(run_id)            │
                    │    │     ├─► k8s_client.get_pod_events(namespace)       │
                    │    │     └─► mlflow_client.search_correlated(run_name)  │
                    │    │                                                     │
                    │    ├─► Build context payload                            │
                    │    │                                                     │
                    │    └─► anthropic.messages.create(                       │
                    │              model="claude-sonnet-4-5",                        │
                    │              tools=TOOL_SCHEMAS,                         │
                    │              messages=[context]                          │
                    │        )                                                 │
                    │              │                                           │
                    │    ┌─────────▼──────────────────────────────┐           │
                    │    │         Tool Dispatch Loop              │           │
                    │    │  while stop_reason == "tool_use":       │           │
                    │    │    tool_call = parse_response()         │           │
                    │    │    result = dispatch(tool_call)         │           │
                    │    │    append tool_result to messages       │           │
                    │    │    re-call anthropic.messages.create()  │           │
                    │    └──────┬──────────────────────────────────┘           │
                    │           │                                              │
                    │    ┌──────▼──────────────────────────────────────────┐  │
                    │    │           Tool Implementations                   │  │
                    │    │  generate_yaml_patch  → reasoning/patch_gen.py  │  │
                    │    │  create_github_pr     → connectors/github_client │  │
                    │    │  write_incident_report→ reasoning/reporter.py   │  │
                    │    │  update_memory        → agent/memory.py         │  │
                    │    └─────────────────────────────────────────────────┘  │
                    └──────────────────────────────────────────────────────────┘
```

---

### 2.2 Data Flow Narrative

**Step 1 — Load State**  
`core.run_agent_cycle()` begins by calling `memory.load()`, which reads `agent_memory.json` from disk. This gives the agent the set of already-processed `run_id` values so it never re-diagnoses a failure it has already acted on. The `last_poll_time` field is used to scope the KFP API query.

**Step 2 — Discover Failures**  
`kfp_client.get_failed_pipeline_runs(last_n_hours=1)` calls `kfp.Client._run_api.list_runs()` with a filter on `state=FAILED` and `created_at >= now - last_n_hours`. The connector normalises the raw `kfp_server_api.ApiRun` objects into plain dicts: `{run_id, pipeline_name, error_message, start_time, node_id}`.

**Step 3 — Deduplication**  
The agent filters the failed-run list against `processed_runs` in memory. Any `run_id` already present with status `FIXED` or `REPORTED` is skipped. Runs with status `IGNORED` are also skipped. This is the only guard against infinite re-processing.

**Step 4 — Evidence Collection**  
For each unprocessed failed run, the agent concurrently (or sequentially for hackathon scope) calls three evidence-gathering tools:

- `get_run_logs(run_id)` → calls `k8s_client` to fetch pod logs for the workflow pods associated with the KFP run. Returns up to `max_lines` lines of raw log text.
- `get_k8s_pod_events(namespace, pod_name_prefix)` → calls `kubernetes.client.CoreV1Api().list_namespaced_event()` filtered by `involved_object.name` prefix. Returns typed events: `OOMKilled`, `BackOff`, `Failed`, etc.
- `get_mlflow_experiments(experiment_name_filter, last_n_runs)` → calls `MlflowClient.search_runs()` with a name filter matching the KFP pipeline name convention. Returns metric histories that reveal whether the model was diverging before the crash.

**Step 5 — Reasoning (Claude claude-sonnet-4-5)**  
All collected evidence is assembled into a structured prompt:

```
You are KubeAgent, an autonomous MLOps engineer.
A KFP pipeline run has failed. Here is the evidence:

PIPELINE RUN: {run_id} — {pipeline_name}
ERROR: {error_message}
POD LOGS (last 200 lines): {logs}
K8S EVENTS: {events_json}
MLFLOW CORRELATION: {mlflow_metrics_json}

Diagnose the root cause and take corrective action using the available tools.
```

The Claude claude-sonnet-4-5 API is called with `tool_choice={"type": "auto"}`. Claude reads the evidence, selects a failure type from the classifier taxonomy, and begins calling tools.

**Step 6 — Tool Dispatch Loop**  
`core.py` runs a `while stop_reason == "tool_use"` loop. Each iteration:

1. Parse `response.content` for `tool_use` blocks.
2. Call the corresponding Python function via `tools.dispatch(tool_name, tool_input)`.
3. Append a `tool_result` message to the conversation.
4. Re-call `anthropic.messages.create()` with the updated message history.

This continues until Claude returns `stop_reason == "end_turn"`, indicating it is satisfied that it has diagnosed and acted on the failure.

**Step 7 — Action: Patch & PR**  
Claude calls `generate_yaml_patch(failure_type, original_yaml, suggested_fix)`. `patch_gen.py` applies the typed fix strategy (e.g., bump `resources.limits.memory` for OOM) and returns a patched YAML string. Claude then calls `create_github_pr(...)` which uses PyGithub to: create a branch, commit the patched file, and open a PR with the incident summary as the PR body.

**Step 8 — Incident Report**  
Claude calls `write_incident_report(...)`. `reporter.py` renders a structured markdown file to `reports/{run_id}.md` with the full diagnosis, correlation data, fix applied, and PR link.

**Step 9 — Memory Update**  
Claude calls `update_memory(run_id, status="FIXED", findings={...}, actions_taken=[...])`. `memory.py` writes the updated state back to `agent_memory.json`.

**Step 10 — Sleep**  
`main.py` sleeps for `POLL_INTERVAL_SECONDS` (default: 300) before triggering the next `run_agent_cycle()`.

---

### 2.3 Agent Loop Pseudocode

```
AGENT LOOP (every POLL_INTERVAL_SECONDS):
  1. LOAD memory from agent_memory.json
  2. POLL: call get_failed_pipeline_runs(last_n_hours=1)
  3. For each failed run:
     a. OBSERVE: call get_run_logs(run_id)
     b. OBSERVE: call get_k8s_pod_events(namespace)
     c. CORRELATE: call get_mlflow_experiments(run_name_filter)
     d. REASON: send all context to Claude claude-sonnet-4-5 with tool_use schema
     e. ACT: Claude returns tool_use calls:
        - generate_yaml_patch(failure_type, original_manifest)
        - create_github_pr(patch, description)
        - write_incident_report(summary)
     f. LOG: call update_memory(findings, actions_taken)
  4. SLEEP POLL_INTERVAL_SECONDS
  5. REPEAT
```

```python
# Expanded pseudocode

def run_agent_cycle(config, memory):
    failed_runs = kfp_client.get_failed_pipeline_runs(
        last_n_hours=config.LOOKBACK_HOURS
    )
    new_failures = [r for r in failed_runs
                    if r["run_id"] not in memory["processed_runs"]]

    for run in new_failures:
        evidence = {
            "run": run,
            "logs": k8s_client.get_run_logs(run["run_id"]),
            "events": k8s_client.get_pod_events(config.NAMESPACE, run["run_id"]),
            "mlflow": mlflow_client.get_correlated_runs(run["pipeline_name"]),
        }

        messages = [build_system_prompt(evidence)]
        stop_reason = None

        while stop_reason != "end_turn":
            response = anthropic.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4096,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
            stop_reason = response.stop_reason
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "tool_use":
                    result = tools.dispatch(block.name, block.input)
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        }]
                    })

    memory.save()
```

---

## 3. What Already Exists (DO NOT REBUILD)

### KFP SDK v2

| Class / Method | What it does | Our usage |
|---|---|---|
| `kfp.Client(host=KFP_ENDPOINT)` | Main KFP client; authenticates via IAP OAuth2, existing_token, or cookie | Instantiated once in `kfp_client.py`; host from `KF_PIPELINES_ENDPOINT` |
| `client._run_api` (`RunServiceApi`) | Low-level run CRUD | We call `.list_runs(filter=...)` and `.get_run(run_id)` |
| `client._run_api.list_runs()` | Paginated list of runs; supports state/time filters | Called with `state=FAILED` to discover new failures |
| `client._run_api.get_run(run_id)` | Full run object with `pipeline_spec`, `resource_references`, error detail | Fetched to get the pipeline YAML path and node-level error message |
| `client._run_api.retry_run(run_id)` | Re-submits a failed run | Reserved for future "auto-retry transient failures" feature |
| `client._experiment_api.list_experiments()` | Lists all KFP experiments | Used to resolve experiment name → experiment_id |
| `client._pipelines_api.list_pipelines()` | Lists all registered pipelines | Used to look up the pipeline YAML for patch generation |
| `@dsl.component`, `@dsl.pipeline` | DSL decorators for pipeline authoring | Not used by agent; referenced in patched YAML files |
| `kfp.compiler.Compiler().compile()` | Compiles pipeline to YAML/JSON | Not used by agent at runtime |
| Run states | `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED`, `ERROR` | Agent filters on `FAILED` and `ERROR` |

### Katib SDK

| Class / Method | What it does | Our usage |
|---|---|---|
| `KatibClient(namespace="kubeflow")` | Main Katib client; communicates with Katib controller via Kubernetes API | Instantiated in a future `katib_client.py` connector |
| `katib_client.list_experiments(namespace)` | Returns list of `V1beta1Experiment` CRDs | Used to scan for experiments in `Failed` or `Stopped` state |
| `katib_client.get_experiment(name, namespace)` | Returns full experiment spec + status | Used to retrieve trial failure details |
| `katib_client.get_optimal_hyperparameters(name, namespace)` | Returns best trial's hyperparameter set | Used to populate incident report with last-known-good params |
| `V1beta1Experiment`, `V1beta1ExperimentSpec` | CRD model objects | Deserialized from API response; not reconstructed by agent |
| `V1beta1ParameterSpec` | Defines search space (min/max/discrete values) | Read to suggest parameter range expansions in patch |

> **Hackathon scope:** Katib integration is implemented as a read-only connector. Experiment creation and trial management are out of scope.

### MLflow Tracking

| Class / Method | What it does | Our usage |
|---|---|---|
| `MlflowClient(tracking_uri=MLFLOW_URI)` | Main MLflow client; REST-backed | Instantiated once in `mlflow_client.py` |
| `client.search_runs(experiment_ids, filter_string, order_by, max_results)` | SQL-like run query | Called with `filter_string=f"tags.kfp_run_id = '{run_id}'"` for correlation |
| `client.get_run(run_id)` | Full run with `RunInfo`, `RunData` (metrics, params, tags) | Used to extract metric history at time of failure |
| `client.get_metric_history(run_id, key)` | Time-series list of `Metric` objects | Used to detect diverging loss before OOM crash |
| `client.get_experiment_by_name(name)` | Resolves experiment name → `Experiment` object | Used when KFP run name matches MLflow experiment name |
| `client.search_experiments()` | Lists all experiments | Used for fuzzy-match when exact name lookup fails |
| `client.download_artifacts(run_id, path)` | Downloads artifact files | Reserved for future: download model checkpoint to inspect |
| `RunInfo`, `RunData`, `Metric`, `Param`, `RunTag` | Entity models | Deserialized from API; fields accessed directly |
| Run statuses | `RUNNING`, `SCHEDULED`, `FINISHED`, `FAILED`, `KILLED` | Correlated with KFP run state |

### Training Operator

| Class / Method | What it does | Our usage |
|---|---|---|
| `kubeflow_trainer_api.*` (generated OpenAPI client) | Full CRUD for training job CRDs | Read-only: list jobs, get status |
| `PyTorchJob`, `TFJob`, `MPIJob`, `PaddleJob`, `JAXJob`, `XGBoostJob` | CRD types for distributed training | Agent checks all types for `Failed` status conditions |
| `spec.pytorchReplicaSpecs["Master"]` / `["Worker"]` | Container replica specs | Patched to adjust resource limits (OOM fix target) |
| `container.resources.limits["nvidia.com/gpu"]` | GPU resource limit | Adjusted in patch when GPU OOM is detected |
| Status conditions: `Created`, `Running`, `Succeeded`, `Failed` | Job lifecycle states | Agent filters on `Failed` condition |

### Kubernetes Python Client

| Class / Method | What it does | Our usage |
|---|---|---|
| `kubernetes.config.load_incluster_config()` | Loads in-cluster service account config | Used when agent runs inside the cluster |
| `kubernetes.config.load_kube_config()` | Loads `~/.kube/config` | Used for local development |
| `CoreV1Api().list_namespaced_event(namespace)` | Lists events in a namespace | Called to retrieve `OOMKilled`, `BackOff`, `Failed` events |
| `CoreV1Api().read_namespaced_pod_log(name, namespace)` | Streams pod logs | Called to fetch container logs for failed workflow pods |
| `CoreV1Api().list_namespaced_pod(namespace)` | Lists pods with label selectors | Used to find pods associated with a KFP run_id |

---

## 4. What We Build (KubeAgent Contribution)

### 4.1 Agent Core (`agent/core.py`)

**Purpose:** Orchestrates the entire agent loop. Owns the Claude API call, the tool dispatch while-loop, and the per-run evidence assembly. This is the only file that imports the Anthropic SDK.

**Key classes/functions:**

```python
class KubeAgent:
    def __init__(self, config: Config)
    def run_cycle(self) -> CycleResult
    def _collect_evidence(self, run: dict) -> Evidence
    def _run_claude_loop(self, evidence: Evidence) -> list[ToolCall]
    def _dispatch_tool(self, tool_name: str, tool_input: dict) -> str
    def _build_prompt(self, evidence: Evidence) -> list[dict]
```

**Dependencies:** `anthropic`, `agent/memory.py`, `agent/tools.py`, all `connectors/`, `config.py`

**Claude API call pattern:**

```python
response = self.anthropic_client.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=4096,
    system=SYSTEM_PROMPT,
    tools=TOOL_SCHEMAS,
    messages=messages,
)
```

---

### 4.2 Memory Manager (`agent/memory.py`)

**Purpose:** Provides a simple read/write interface to `agent_memory.json`. Handles file locking for safety, schema migration, and the deduplication check. Also accumulates cycle-level statistics.

**Key classes/functions:**

```python
class MemoryManager:
    def __init__(self, memory_path: str = "agent_memory.json")
    def load(self) -> dict                          # reads + validates JSON
    def save(self, state: dict) -> None             # atomic write via tempfile
    def is_processed(self, run_id: str) -> bool     # dedup check
    def mark_processed(self, run_id: str, record: dict) -> None
    def increment_stats(self, failure_type: str) -> None
    def update_last_poll(self) -> None
```

**Dependencies:** `json`, `fcntl` (file lock), `pathlib`

**Deduplication logic:**
```python
def is_processed(self, run_id: str) -> bool:
    state = self.load()
    return run_id in state["processed_runs"]
```

---

### 4.3 Tool Definitions (`agent/tools.py`)

**Purpose:** Defines the JSON schemas for all 8 Claude tools, plus the dispatcher that routes `tool_name` + `tool_input` to the correct Python implementation. This is the single source of truth for the tool interface.

**Key objects/functions:**

```python
TOOL_SCHEMAS: list[dict]          # all 8 tool JSON schemas — passed to Anthropic API

def dispatch(tool_name: str, tool_input: dict) -> str:
    """Routes Claude tool_use calls to Python implementations."""
    router = {
        "get_failed_pipeline_runs": kfp_client.get_failed_pipeline_runs,
        "get_run_logs":             k8s_client.get_run_logs,
        "get_mlflow_experiments":   mlflow_client.get_mlflow_experiments,
        "get_k8s_pod_events":       k8s_client.get_pod_events,
        "generate_yaml_patch":      patch_gen.generate_yaml_patch,
        "create_github_pr":         github_client.create_github_pr,
        "write_incident_report":    reporter.write_incident_report,
        "update_memory":            memory.update_from_tool,
    }
    fn = router[tool_name]
    result = fn(**tool_input)
    return json.dumps(result) if not isinstance(result, str) else result
```

**Dependencies:** all `connectors/`, all `reasoning/`, `agent/memory.py`

---

### 4.4 KFP Connector (`connectors/kfp_client.py`)

**Purpose:** Wraps `kfp.Client` to expose a clean interface for the agent. Normalises raw KFP API response objects into plain Python dicts. Handles pagination transparently.

**Key functions:**

```python
def get_failed_pipeline_runs(last_n_hours: int = 1, namespace: str = "kubeflow") -> list[dict]:
    """
    Returns list of dicts:
    [{"run_id": str, "pipeline_name": str, "error_message": str,
      "start_time": str, "node_id": str, "workflow_name": str}]
    """

def get_pipeline_yaml(pipeline_id: str) -> str:
    """Downloads the pipeline YAML from KFP for use in patch generation."""

def retry_run(run_id: str) -> bool:
    """Triggers a retry of the specified run. Returns True on success."""
```

**Dependencies:** `kfp`, `kfp_server_api`, `config.py`

**Auth pattern:**
```python
client = kfp.Client(
    host=config.KFP_ENDPOINT,
    existing_token=config.KFP_TOKEN,  # or None for in-cluster IAP
)
```

---

### 4.5 MLflow Connector (`connectors/mlflow_client.py`)

**Purpose:** Wraps `MlflowClient` to search for experiments correlated with a failing KFP run. Implements fuzzy name matching, metric history extraction, and trend analysis (diverging loss detection).

**Key functions:**

```python
def get_mlflow_experiments(experiment_name_filter: str, last_n_runs: int = 5) -> list[dict]:
    """
    Returns list of dicts:
    [{"run_id": str, "experiment_name": str, "status": str,
      "metrics": {"accuracy": float, "loss": float, ...},
      "params": dict, "tags": dict}]
    """

def get_metric_trend(run_id: str, metric_key: str) -> list[dict]:
    """Returns time-series metric history as list of {step, value, timestamp}."""

def correlate_with_kfp_run(kfp_run_id: str, pipeline_name: str) -> dict | None:
    """
    Attempts to find an MLflow run tagged with the KFP run ID.
    Falls back to fuzzy match on experiment name.
    """
```

**Dependencies:** `mlflow`, `config.py`

**Correlation strategy:**
1. Primary: search for MLflow runs tagged with `kfp_run_id = run_id`
2. Fallback: `search_experiments()` with name similarity to KFP pipeline name
3. If no match: return empty dict (agent proceeds without MLflow context)

---

### 4.6 Kubernetes Connector (`connectors/k8s_client.py`)

**Purpose:** Wraps the `kubernetes` Python client to fetch pod logs and Kubernetes events. Resolves KFP run IDs to actual pod names via label selectors. Handles both in-cluster and local (kubeconfig) authentication.

**Key functions:**

```python
def get_run_logs(run_id: str, max_lines: int = 200) -> str:
    """
    Finds pods labelled workflow-run-id=run_id and returns
    concatenated container logs (last max_lines lines).
    """

def get_pod_events(namespace: str, pod_name_prefix: str) -> list[dict]:
    """
    Returns list of K8s events:
    [{"type": str, "reason": str, "message": str,
      "timestamp": str, "count": int, "involved_object": str}]
    """

def get_pod_resource_usage(namespace: str, pod_name: str) -> dict:
    """Fetches current CPU/memory usage via metrics-server (if available)."""
```

**Dependencies:** `kubernetes`, `config.py`

**Auth pattern:**
```python
try:
    kubernetes.config.load_incluster_config()
except ConfigException:
    kubernetes.config.load_kube_config()
```

---

### 4.7 GitHub Connector (`connectors/github_client.py`)

**Purpose:** Wraps PyGithub to create branches, commit patched YAML files, and open pull requests. Handles branch name sanitisation and duplicate PR detection (idempotent).

**Key functions:**

```python
def create_github_pr(
    repo: str,
    branch_name: str,
    file_path: str,
    file_content: str,
    pr_title: str,
    pr_body: str,
) -> str:
    """
    Creates a branch from main, commits file_content to file_path,
    opens a PR, and returns the PR HTML URL.
    Idempotent: if branch already exists, updates the file.
    """

def get_existing_pr(repo: str, branch_name: str) -> str | None:
    """Returns PR URL if a PR for branch_name already exists, else None."""

def sanitise_branch_name(run_id: str, failure_type: str) -> str:
    """Returns 'kubeagent/fix-{failure_type}-{run_id[:8]}' (URL-safe)."""
```

**Dependencies:** `PyGithub`, `config.py`

**Auth pattern:**
```python
gh = Github(config.GITHUB_TOKEN)
repo = gh.get_repo(config.GITHUB_REPO)  # "org/repo-name"
```

---

### 4.8 Failure Classifier (`reasoning/classifier.py`)

**Purpose:** Pre-classifies failures using regex and keyword heuristics before sending to Claude. This gives Claude a "hint" in the prompt, reducing token usage and improving accuracy. Claude can override the pre-classification.

**Key functions:**

```python
FAILURE_PATTERNS: dict[str, list[str]]  # failure_type → list of regex patterns

def classify(logs: str, events: list[dict]) -> FailureClassification:
    """
    Returns:
    FailureClassification(
        failure_type="OOM_KILL",
        confidence=0.95,
        matched_patterns=["OOMKilled", "exit code 137"],
        suggested_fix="Increase memory limit",
    )
    """

def extract_error_context(logs: str, pattern: str, context_lines: int = 5) -> str:
    """Returns the matching log line plus N lines of context."""
```

**Dependencies:** `re`, `dataclasses`

---

### 4.9 YAML Patch Generator (`reasoning/patch_gen.py`)

**Purpose:** Given a `failure_type` and the `original_yaml` string of the pipeline/job manifest, applies the appropriate fix and returns the patched YAML. Uses `ruamel.yaml` to preserve comments and formatting. Each failure type has a typed patch strategy.

**Key functions:**

```python
def generate_yaml_patch(
    failure_type: str,
    original_yaml: str,
    suggested_fix: dict,
) -> str:
    """
    Dispatches to the appropriate patch strategy function.
    Returns patched YAML string.
    """

# Patch strategies (one per failure type):
def _patch_oom(yaml_doc: dict, suggested_fix: dict) -> dict
def _patch_wrong_image(yaml_doc: dict, suggested_fix: dict) -> dict
def _patch_missing_env(yaml_doc: dict, suggested_fix: dict) -> dict
def _patch_crash_loop(yaml_doc: dict, suggested_fix: dict) -> dict
def _patch_resource_quota(yaml_doc: dict, suggested_fix: dict) -> dict
def _patch_timeout(yaml_doc: dict, suggested_fix: dict) -> dict
```

**`suggested_fix` dict examples by type:**
```python
# OOM_KILL
{"memory_limit": "8Gi", "memory_request": "6Gi"}

# WRONG_IMAGE
{"image": "gcr.io/myproject/trainer:v1.2.3"}

# MISSING_ENV
{"env_vars": [{"name": "MODEL_PATH", "value": "/mnt/models/v1"}]}

# TIMEOUT
{"timeout_seconds": 7200}
```

**Dependencies:** `ruamel.yaml`, `copy`

---

### 4.10 Incident Reporter (`reasoning/reporter.py`)

**Purpose:** Renders a structured markdown incident report for every processed failure. Reports are written to `reports/{run_id}.md` and linked in the GitHub PR body.

**Key functions:**

```python
def write_incident_report(
    run_id: str,
    failure_type: str,
    root_cause: str,
    fix_applied: str,
    pr_url: str,
    mlflow_correlation: dict,
) -> str:
    """
    Renders INCIDENT_TEMPLATE with provided fields.
    Writes to reports/{run_id}.md.
    Returns the file path.
    """
```

**Report template structure:**
```markdown
# Incident Report — {run_id}
**Detected:** {timestamp}  
**Failure Type:** {failure_type}  
**Pipeline:** {pipeline_name}

## Root Cause
{root_cause}

## Evidence
### Pod Logs (excerpt)
```
{log_excerpt}
```
### Kubernetes Events
{events_table}

## MLflow Correlation
{mlflow_metrics_table}

## Fix Applied
{fix_applied}

## Pull Request
{pr_url}
```

**Dependencies:** `jinja2` or f-strings, `pathlib`

---

## 5. Claude Tool Definitions (JSON Schemas)

These schemas are passed verbatim in the `tools` parameter of every `anthropic.messages.create()` call.

### Tool 1: `get_failed_pipeline_runs`

```json
{
  "name": "get_failed_pipeline_runs",
  "description": "Queries the KFP API server for pipeline runs that have failed within the specified time window. Returns a list of failed runs with enough detail to begin diagnosis. Always call this first in each agent cycle.",
  "input_schema": {
    "type": "object",
    "properties": {
      "last_n_hours": {
        "type": "integer",
        "description": "Number of hours to look back for failed runs. Default is 1.",
        "default": 1
      },
      "namespace": {
        "type": "string",
        "description": "Kubernetes namespace where KFP is deployed. Default is 'kubeflow'.",
        "default": "kubeflow"
      }
    },
    "required": []
  }
}
```

**Output shape:**
```json
[
  {
    "run_id": "abc123",
    "pipeline_name": "train-resnet-pipeline",
    "error_message": "Error in step 'train': exit code 137",
    "start_time": "2024-01-15T10:30:00Z",
    "workflow_name": "train-resnet-pipeline-abc123",
    "node_id": "train-resnet-pipeline-abc123-1234567890"
  }
]
```

---

### Tool 2: `get_run_logs`

```json
{
  "name": "get_run_logs",
  "description": "Fetches the container logs for all pods associated with a specific KFP pipeline run. Returns the last max_lines lines of logs concatenated across all workflow pods. Use this immediately after identifying a failed run to gather raw error evidence.",
  "input_schema": {
    "type": "object",
    "properties": {
      "run_id": {
        "type": "string",
        "description": "The KFP run ID (UUID) for which to fetch logs."
      },
      "max_lines": {
        "type": "integer",
        "description": "Maximum number of log lines to return. Default is 200.",
        "default": 200
      }
    },
    "required": ["run_id"]
  }
}
```

**Output shape:**
```
"[pod: train-step-abc123] 2024-01-15 10:31:00 INFO  Starting training...\n
[pod: train-step-abc123] 2024-01-15 10:32:45 ERROR Killed\n
[pod: train-step-abc123] 2024-01-15 10:32:45 ERROR exit code 137"
```

---

### Tool 3: `get_mlflow_experiments`

```json
{
  "name": "get_mlflow_experiments",
  "description": "Searches MLflow for experiment runs correlated with a failing KFP pipeline by name. Returns metric snapshots (accuracy, loss, etc.) and run status. Use this to determine whether the model was diverging before the infrastructure failure occurred.",
  "input_schema": {
    "type": "object",
    "properties": {
      "experiment_name_filter": {
        "type": "string",
        "description": "Substring filter applied to MLflow experiment names. Typically the KFP pipeline name or a prefix thereof."
      },
      "last_n_runs": {
        "type": "integer",
        "description": "Maximum number of MLflow runs to return, ordered by most recent. Default is 5.",
        "default": 5
      }
    },
    "required": ["experiment_name_filter"]
  }
}
```

**Output shape:**
```json
[
  {
    "run_id": "mlflow-run-def456",
    "experiment_name": "train-resnet-pipeline",
    "status": "FAILED",
    "metrics": {"accuracy": 0.72, "loss": 2.1, "val_loss": 3.4},
    "params": {"learning_rate": "0.01", "batch_size": "128"},
    "tags": {"kfp_run_id": "abc123"}
  }
]
```

---

### Tool 4: `get_k8s_pod_events`

```json
{
  "name": "get_k8s_pod_events",
  "description": "Retrieves Kubernetes events for pods matching the given name prefix in the specified namespace. Events include OOMKilled signals, ImagePullBackOff errors, scheduling failures, and CrashLoopBackOff conditions. Essential for diagnosing infrastructure-level failures.",
  "input_schema": {
    "type": "object",
    "properties": {
      "namespace": {
        "type": "string",
        "description": "Kubernetes namespace to query for events.",
        "default": "kubeflow"
      },
      "pod_name_prefix": {
        "type": "string",
        "description": "Prefix of the pod name(s) to filter events for. Usually the workflow name derived from the KFP run ID."
      }
    },
    "required": ["namespace", "pod_name_prefix"]
  }
}
```

**Output shape:**
```json
[
  {
    "type": "Warning",
    "reason": "OOMKilling",
    "message": "Memory cgroup out of memory: Kill process 1234 (python) score 999 or sacrifice child",
    "timestamp": "2024-01-15T10:32:44Z",
    "count": 1,
    "involved_object": "train-step-abc123"
  },
  {
    "type": "Warning",
    "reason": "BackOff",
    "message": "Back-off restarting failed container",
    "timestamp": "2024-01-15T10:32:50Z",
    "count": 3,
    "involved_object": "train-step-abc123"
  }
]
```

---

### Tool 5: `generate_yaml_patch`

```json
{
  "name": "generate_yaml_patch",
  "description": "Generates a patched YAML manifest to fix the identified failure. Provide the exact failure type, the original YAML content of the failing pipeline component or job spec, and a suggested_fix dict with the specific values to change. Returns the complete patched YAML string ready for committing to git.",
  "input_schema": {
    "type": "object",
    "properties": {
      "failure_type": {
        "type": "string",
        "description": "The classified failure type.",
        "enum": ["OOM_KILL", "WRONG_IMAGE", "MISSING_ENV", "CRASH_LOOP", "TIMEOUT", "RESOURCE_QUOTA", "DEPENDENCY_FAIL"]
      },
      "original_yaml": {
        "type": "string",
        "description": "The original YAML content of the pipeline component, training job spec, or Kubernetes manifest that needs to be patched."
      },
      "suggested_fix": {
        "type": "object",
        "description": "Key-value pairs describing the specific fix to apply. Structure varies by failure_type.",
        "examples": [
          {"memory_limit": "8Gi", "memory_request": "6Gi"},
          {"image": "gcr.io/project/trainer:v1.2.3"},
          {"env_vars": [{"name": "MODEL_PATH", "value": "/mnt/models/v1"}]},
          {"timeout_seconds": 7200}
        ]
      }
    },
    "required": ["failure_type", "original_yaml", "suggested_fix"]
  }
}
```

**Output shape:**
```
"# Patched by KubeAgent — OOM_KILL fix\napiVersion: ...\nresources:\n  limits:\n    memory: \"8Gi\"\n  requests:\n    memory: \"6Gi\"\n"
```

---

### Tool 6: `create_github_pr`

```json
{
  "name": "create_github_pr",
  "description": "Creates a GitHub pull request with the patched YAML fix. Creates a new branch from main, commits the patched file, and opens a PR. The PR body should include a summary of the failure, root cause, and the fix applied. Returns the HTML URL of the created PR.",
  "input_schema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "GitHub repository in 'owner/repo' format.",
        "example": "myorg/ml-pipelines"
      },
      "branch_name": {
        "type": "string",
        "description": "Name of the new branch to create. Use format: 'kubeagent/fix-{failure_type}-{run_id_prefix}'.",
        "example": "kubeagent/fix-oom-kill-abc123"
      },
      "file_path": {
        "type": "string",
        "description": "Repository-relative path to the file to create or update.",
        "example": "pipelines/train-resnet/component.yaml"
      },
      "file_content": {
        "type": "string",
        "description": "Complete new content for the file (the patched YAML)."
      },
      "pr_title": {
        "type": "string",
        "description": "Title of the pull request.",
        "example": "[KubeAgent] Fix OOM_KILL in train-resnet-pipeline (run abc123)"
      },
      "pr_body": {
        "type": "string",
        "description": "Full markdown body of the PR including failure summary, root cause, fix description, and incident report link."
      }
    },
    "required": ["repo", "branch_name", "file_path", "file_content", "pr_title", "pr_body"]
  }
}
```

**Output shape:**
```
"https://github.com/myorg/ml-pipelines/pull/42"
```

---

### Tool 7: `write_incident_report`

```json
{
  "name": "write_incident_report",
  "description": "Writes a structured markdown incident report for the diagnosed failure. The report is saved to reports/{run_id}.md and should be called after the PR is created so the PR URL can be included. Returns the file path of the written report.",
  "input_schema": {
    "type": "object",
    "properties": {
      "run_id": {
        "type": "string",
        "description": "The KFP run ID this incident report covers."
      },
      "failure_type": {
        "type": "string",
        "description": "The classified failure type.",
        "enum": ["OOM_KILL", "WRONG_IMAGE", "MISSING_ENV", "CRASH_LOOP", "TIMEOUT", "RESOURCE_QUOTA", "DEPENDENCY_FAIL"]
      },
      "root_cause": {
        "type": "string",
        "description": "Human-readable explanation of what caused the failure and why."
      },
      "fix_applied": {
        "type": "string",
        "description": "Description of the YAML change made to fix the issue."
      },
      "pr_url": {
        "type": "string",
        "description": "URL of the GitHub PR created with the fix."
      },
      "mlflow_correlation": {
        "type": "object",
        "description": "Dict of correlated MLflow run data: run_id, metrics, and any anomalies detected.",
        "properties": {
          "mlflow_run_id": {"type": "string"},
          "metrics_at_failure": {"type": "object"},
          "anomaly_detected": {"type": "boolean"},
          "anomaly_description": {"type": "string"}
        }
      }
    },
    "required": ["run_id", "failure_type", "root_cause", "fix_applied", "pr_url", "mlflow_correlation"]
  }
}
```

**Output shape:**
```
"reports/abc123.md"
```

---

### Tool 8: `update_memory`

```json
{
  "name": "update_memory",
  "description": "Persists the outcome of diagnosing and acting on a failed run to agent_memory.json. Always call this as the final step after all other actions are complete. This prevents the agent from re-processing the same failure on the next polling cycle.",
  "input_schema": {
    "type": "object",
    "properties": {
      "run_id": {
        "type": "string",
        "description": "The KFP run ID to mark as processed."
      },
      "status": {
        "type": "string",
        "description": "Final disposition of this run.",
        "enum": ["FIXED", "REPORTED", "IGNORED"]
      },
      "findings": {
        "type": "object",
        "description": "Dict of diagnostic findings to store.",
        "properties": {
          "failure_type": {"type": "string"},
          "root_cause": {"type": "string"},
          "mlflow_run_id": {"type": "string"},
          "metrics_at_failure": {"type": "object"}
        }
      },
      "actions_taken": {
        "type": "array",
        "description": "List of action descriptions taken by the agent.",
        "items": {"type": "string"},
        "example": ["Generated OOM YAML patch", "Created PR #42", "Wrote incident report"]
      }
    },
    "required": ["run_id", "status", "findings", "actions_taken"]
  }
}
```

**Output shape:**
```
"Memory updated: run abc123 marked as FIXED"
```

---

## 6. Failure Classification Rules

| Failure Type | Error Patterns (regex / keywords) | Root Cause | Fix Strategy | YAML Patch Target |
|---|---|---|---|---|
| **OOM_KILL** | `OOMKilled`, `exit code 137`, `memory limit exceeded`, `Killed`, `Cannot allocate memory`, `memory cgroup out of memory` | Container exceeded its memory limit; OS killed the process | Increase `resources.limits.memory` (2× current); optionally increase `resources.requests.memory` | `spec.containers[].resources.limits.memory` in pipeline component or TrainingJob replica spec |
| **WRONG_IMAGE** | `ImagePullBackOff`, `ErrImagePull`, `manifest unknown`, `not found`, `repository does not exist`, `unauthorized: authentication required` | Docker image tag does not exist, was deleted, or registry credentials are missing | Update image tag to a valid existing tag; add `imagePullSecret` if auth issue | `spec.containers[].image` in component YAML; `spec.pytorchReplicaSpecs.Worker.template.spec.containers[0].image` for TrainingJob |
| **MISSING_ENV** | `KeyError`, `EnvironmentError`, `os.environ\[`, `getenv returned None`, `required env var`, `Missing required environment variable` | A required environment variable is not set in the container spec | Add the missing env var to `spec.containers[].env` with a value or reference to a Secret/ConfigMap | `spec.containers[].env` — append `{name: VAR_NAME, value: ...}` or `valueFrom.secretKeyRef` |
| **CRASH_LOOP** | `CrashLoopBackOff`, restart count > 3, `back-off restarting failed container`, `Error` reason in events | Container starts and immediately exits; typically an application startup error | Inspect entrypoint; patch `command`/`args` or add startup probe; surface the inner exit code | `spec.containers[].command`, `spec.containers[].args`, `spec.containers[].startupProbe` |
| **RESOURCE_QUOTA** | `Insufficient cpu`, `Insufficient memory`, `exceeded quota`, `pods "..." is forbidden`, `resource quota exceeded` | Namespace resource quota prevents pod scheduling | Reduce resource requests, or (if admin) update the ResourceQuota; alternatively request a different node pool | `spec.containers[].resources.requests.cpu/memory`; note new ResourceQuota object if quota increase needed |
| **TIMEOUT** | `DeadlineExceeded`, `context deadline exceeded`, `timeout`, `execution timed out`, `Operation timed out`, `504` | Step exceeded its configured timeout; could be a slow dataset, hung process, or undersized machine | Increase step timeout; optionally add `resources.limits` for more CPU to speed processing | `timeout` field in pipeline step DSL YAML; `spec.activeDeadlineSeconds` in pod spec |
| **DEPENDENCY_FAIL** | `upstream step failed`, `input artifact not found`, `artifact .* does not exist`, `ValueError: input .* is required`, `PipelineRunStepNotFound` | A preceding pipeline step failed and its output artifact was never produced, causing a downstream step to fail on missing input | Fix the upstream step first (agent should re-classify the upstream failure); mark downstream as `DEPENDENCY_FAIL` to avoid redundant PRs | No YAML patch for the downstream step; incident report references the upstream `run_id` to fix |

---

## 7. Memory Schema (`agent_memory.json`)

The agent reads and writes this file atomically at the start and end of every cycle. It is the agent's only persistent state.

```json
{
  "schema_version": "1.0",
  "last_poll_time": "2024-01-15T10:45:00Z",
  "processed_runs": {
    "run-id-xyz": {
      "status": "FIXED",
      "failure_type": "OOM_KILL",
      "pipeline_name": "train-resnet-pipeline",
      "pr_url": "https://github.com/myorg/ml-pipelines/pull/42",
      "incident_report_path": "reports/run-id-xyz.md",
      "timestamp": "2024-01-15T10:35:22Z",
      "mlflow_run_id": "abc123def456",
      "metrics_at_failure": {
        "accuracy": 0.72,
        "loss": 2.1,
        "val_loss": 3.4,
        "epoch": 14
      },
      "actions_taken": [
        "Generated OOM_KILL YAML patch: memory limit 4Gi → 8Gi",
        "Created PR #42: https://github.com/myorg/ml-pipelines/pull/42",
        "Wrote incident report: reports/run-id-xyz.md"
      ]
    },
    "run-id-abc": {
      "status": "REPORTED",
      "failure_type": "MISSING_ENV",
      "pipeline_name": "feature-engineering-pipeline",
      "pr_url": "https://github.com/myorg/ml-pipelines/pull/43",
      "incident_report_path": "reports/run-id-abc.md",
      "timestamp": "2024-01-15T09:12:00Z",
      "mlflow_run_id": null,
      "metrics_at_failure": {},
      "actions_taken": [
        "Generated MISSING_ENV YAML patch: added MODEL_PATH env var",
        "Created PR #43",
        "Wrote incident report"
      ]
    }
  },
  "cycle_count": 42,
  "last_error": null,
  "stats": {
    "total_failures_detected": 15,
    "total_prs_created": 8,
    "total_incidents_reported": 10,
    "total_ignored": 5,
    "failure_type_counts": {
      "OOM_KILL": 5,
      "WRONG_IMAGE": 3,
      "MISSING_ENV": 2,
      "CRASH_LOOP": 2,
      "RESOURCE_QUOTA": 1,
      "TIMEOUT": 1,
      "DEPENDENCY_FAIL": 1
    }
  }
}
```

### Memory Update Rules

| Event | Field updated |
|---|---|
| Cycle starts | `last_poll_time` |
| Failure detected and processed | `processed_runs[run_id]`, `stats.total_failures_detected`, `stats.failure_type_counts[type]` |
| PR created | `processed_runs[run_id].pr_url`, `stats.total_prs_created` |
| Report written | `processed_runs[run_id].incident_report_path`, `stats.total_incidents_reported` |
| Run skipped (already processed) | No update |
| Agent errors | `last_error` — stringified exception |
| Cycle completes | `cycle_count += 1` |

---

## 8. Configuration Reference

All configuration is loaded from environment variables in `config.py`. No config file format is used; 12-factor app style.

| Variable | Required | Default | Description | Example |
|---|---|---|---|---|
| `KFP_ENDPOINT` | Yes | — | URL of the KFP API server | `http://kubeflow.example.com:8888` |
| `KFP_TOKEN` | No | `None` | Bearer token for KFP API auth. If unset, in-cluster IAP is attempted | `ya29.abc123...` |
| `MLFLOW_TRACKING_URI` | Yes | — | URI of the MLflow tracking server | `http://mlflow.example.com:5000` |
| `GITHUB_TOKEN` | Yes | — | GitHub Personal Access Token with `repo` scope | `ghp_abc123...` |
| `GITHUB_REPO` | Yes | — | Target repository for PRs in `owner/repo` format | `myorg/ml-pipelines` |
| `GITHUB_BASE_BRANCH` | No | `main` | Base branch for PRs | `main` |
| `KUBEFLOW_NAMESPACE` | No | `kubeflow` | Kubernetes namespace where KFP and training jobs run | `kubeflow` |
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key for Claude claude-sonnet-4-5 | `sk-ant-api03-...` |
| `POLL_INTERVAL_SECONDS` | No | `300` | Seconds to sleep between agent cycles | `60` |
| `LOOKBACK_HOURS` | No | `1` | Hours of history to query for failed runs per cycle | `2` |
| `MAX_LOG_LINES` | No | `200` | Max pod log lines to include in Claude context | `500` |
| `MAX_MLFLOW_RUNS` | No | `5` | Max MLflow runs to retrieve per correlation query | `10` |
| `MEMORY_PATH` | No | `agent_memory.json` | Path to the persistent memory file | `/data/agent_memory.json` |
| `REPORTS_DIR` | No | `reports/` | Directory for incident report markdown files | `/data/reports/` |
| `DRY_RUN` | No | `false` | If `true`, skip PR creation and memory writes (for testing) | `true` |
| `LOG_LEVEL` | No | `INFO` | Python logging level | `DEBUG` |

### `config.py` pattern

```python
import os
from dataclasses import dataclass

@dataclass
class Config:
    kfp_endpoint: str = os.environ["KFP_ENDPOINT"]
    kfp_token: str | None = os.getenv("KFP_TOKEN")
    mlflow_tracking_uri: str = os.environ["MLFLOW_TRACKING_URI"]
    github_token: str = os.environ["GITHUB_TOKEN"]
    github_repo: str = os.environ["GITHUB_REPO"]
    github_base_branch: str = os.getenv("GITHUB_BASE_BRANCH", "main")
    kubeflow_namespace: str = os.getenv("KUBEFLOW_NAMESPACE", "kubeflow")
    anthropic_api_key: str = os.environ["ANTHROPIC_API_KEY"]
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    lookback_hours: int = int(os.getenv("LOOKBACK_HOURS", "1"))
    max_log_lines: int = int(os.getenv("MAX_LOG_LINES", "200"))
    max_mlflow_runs: int = int(os.getenv("MAX_MLFLOW_RUNS", "5"))
    memory_path: str = os.getenv("MEMORY_PATH", "agent_memory.json")
    reports_dir: str = os.getenv("REPORTS_DIR", "reports/")
    dry_run: bool = os.getenv("DRY_RUN", "false").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
```

---

## 9. Hackathon Scope & Milestones

### Day 1 — Foundation (Hours 0–8)

**Goal:** Get the agent loop running end-to-end with mocked tools.

| Task | File(s) | Done when |
|---|---|---|
| Scaffold directory structure | All dirs | `tree kubeagent/` shows all modules |
| Implement `config.py` | `config.py` | All env vars load without error |
| Implement `agent/memory.py` | `agent/memory.py` | Load/save/dedup round-trip test passes |
| Define all 8 tool JSON schemas | `agent/tools.py` | Schemas validate against JSON Schema spec |
| Implement `connectors/kfp_client.py` | `connectors/kfp_client.py` | `get_failed_pipeline_runs()` returns list against live or mocked KFP |
| Implement `connectors/k8s_client.py` | `connectors/k8s_client.py` | `get_run_logs()` and `get_pod_events()` return data |
| Implement `agent/core.py` skeleton | `agent/core.py` | Agent loop calls Claude and parses tool_use response |
| Write `main.py` with scheduler | `main.py` | `python main.py` starts polling loop |
| **Milestone:** | | Agent calls Claude claude-sonnet-4-5, receives tool_use response, dispatches a no-op tool |

### Day 2 — Connectors & Reasoning (Hours 8–16)

**Goal:** All connectors functional; classifier and patch generator working for the two highest-priority failure types (OOM_KILL, WRONG_IMAGE).

| Task | File(s) | Done when |
|---|---|---|
| Implement `connectors/mlflow_client.py` | `connectors/mlflow_client.py` | `get_mlflow_experiments()` returns correlated runs |
| Implement `connectors/github_client.py` | `connectors/github_client.py` | `create_github_pr()` opens a real PR in test repo |
| Implement `reasoning/classifier.py` | `reasoning/classifier.py` | OOM and WRONG_IMAGE correctly classified from sample logs |
| Implement `reasoning/patch_gen.py` (OOM + WRONG_IMAGE) | `reasoning/patch_gen.py` | Patched YAML has correct `memory` / `image` values |
| Implement `reasoning/reporter.py` | `reasoning/reporter.py` | `reports/{run_id}.md` renders correctly |
| Wire all tools in `agent/tools.py` dispatcher | `agent/tools.py` | All 8 tools dispatch without `NotImplementedError` |
| Full dry-run cycle test | all | `DRY_RUN=true python main.py` processes a sample failure end-to-end |
| **Milestone:** | | Agent detects OOM failure, generates patch, creates real GitHub PR (in test repo) |

### Day 3 — Hardening & Demo (Hours 16–24)

**Goal:** All failure types handled; demo-ready with real KFP/MLflow; polish docs.

| Task | File(s) | Done when |
|---|---|---|
| Implement remaining patch strategies (MISSING_ENV, CRASH_LOOP, TIMEOUT, RESOURCE_QUOTA) | `reasoning/patch_gen.py` | All 7 failure types have passing unit tests |
| MLflow correlation in Claude prompt | `agent/core.py` | Incident report includes MLflow metrics |
| Error handling: API timeouts, auth failures, empty responses | all connectors | Agent logs error and continues; doesn't crash |
| Memory persistence across restarts | `agent/memory.py` | `processed_runs` survives process kill/restart |
| `DRY_RUN` mode complete coverage | all | `DRY_RUN=true` logs all actions without side effects |
| Demo scenario: inject 3 artificial failures | `tests/inject_failures.py` | OOM, WRONG_IMAGE, MISSING_ENV each produce a PR |
| README.md with quickstart | `README.md` | New contributor can run in < 10 min |
| **Milestone:** | | Live demo: 3 KFP failures → 3 GitHub PRs, fully autonomous |

---

## 10. Known Limitations & Future Work

### Hackathon Limitations

| Limitation | Impact | Workaround |
|---|---|---|
| **Sequential failure processing** | Only one failure is diagnosed at a time per cycle; high-failure environments will lag | Increase `POLL_INTERVAL_SECONDS`; process most recent N failures only |
| **Flat JSON memory** | Not safe for concurrent processes; no indexing | Single-process deployment only; use SQLite in follow-up |
| **No log streaming** | Logs fetched once, not streamed; transient log output may be missed | Increase `MAX_LOG_LINES`; integrate Loki/Elasticsearch in follow-up |
| **Name-based MLflow correlation** | Relies on KFP pipeline name ≈ MLflow experiment name convention; will miss misnamed experiments | Require `kfp_run_id` tag to be set in MLflow runs as part of pipeline template |
| **YAML patch is best-effort** | `patch_gen.py` handles known formats; non-standard manifests may produce invalid patches | Claude can decline to patch and only report; `DRY_RUN` preview before merge |
| **No PR auto-merge** | PRs require human review and merge | By design; keeps human in the loop for safety |
| **Single cluster** | Agent is scoped to one KFP namespace | Multi-cluster: run one agent per cluster; shared memory via S3 in follow-up |
| **No Katib write operations** | Katib connector is read-only | Katib experiment patching (parameter range expansion) planned for v1.1 |

### Future Work (Post-Hackathon)

| Feature | Description | Complexity |
|---|---|---|
| **Slack/PagerDuty notifications** | Agent sends incident summary to Slack channel alongside PR | Low |
| **Auto-retry transient failures** | For `TIMEOUT` and `RESOURCE_QUOTA`, trigger `client.retry_run(run_id)` before patching | Low |
| **Prometheus metrics export** | Export `kubeagent_failures_total`, `kubeagent_prs_created_total` for Grafana dashboards | Medium |
| **SQLite memory backend** | Replace JSON file with SQLite for concurrency safety and query capability | Medium |
| **Multi-cluster support** | Support multiple KFP endpoints via config list; shared memory in S3/GCS | Medium |
| **Katib hyperparameter patching** | When Katib experiments fail due to search space exhaustion, expand parameter ranges | High |
| **LangChain/LangGraph integration** | Refactor agent loop as a LangGraph StateGraph for richer branching logic | High |
| **Historical trend analysis** | Use MLflow metric history to detect degradation before failure; proactive alerts | High |
| **RBAC + audit log** | Track which human approved each auto-PR; restrict agent's GitHub token scope | High |
| **Web UI** | React dashboard showing active failures, cycle history, and agent decisions | High |

---

*Document generated for KubeAgent hackathon — internal engineering reference only.*
