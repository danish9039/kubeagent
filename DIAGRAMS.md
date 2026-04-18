# KubeAgent — System Diagrams

> All diagrams use [Mermaid](https://mermaid.js.org/) syntax. They render natively on GitHub, Notion, and most modern markdown viewers.

---

## 1. High-Level System Architecture

```mermaid
graph TB
    subgraph External["External Systems"]
        KFP["🔵 Kubeflow Pipelines\nAPI Server :8887"]
        MLF["🟠 MLflow Tracking\nServer :5000"]
        K8S["⚙️ Kubernetes API\nPod Logs & Events"]
        GH["🐙 GitHub API\nPR Creation"]
    end

    subgraph KubeAgent["KubeAgent (Claude claude-sonnet-4-5 Powered)"]
        CORE["agent/core.py\nMain Loop"]
        MEM["agent/memory.py\nagent_memory.json"]
        TOOLS["agent/tools.py\n8 Tool Definitions"]

        subgraph Connectors["connectors/"]
            KC["kfp_client.py"]
            MC["mlflow_client.py"]
            KK["k8s_client.py"]
            GC["github_client.py"]
        end

        subgraph Reasoning["reasoning/"]
            CL["classifier.py\nFailure Classifier"]
            PG["patch_gen.py\nYAML Patch Generator"]
            RP["reporter.py\nIncident Reporter"]
        end
    end

    subgraph Outputs["Outputs"]
        PR["📬 GitHub Pull Request\n(YAML Fix)"]
        IR["📄 Incident Report\n(Markdown)"]
        LOG["🧠 Agent Memory\n(JSON State)"]
        SLACK["💬 Slack Alert\n(Optional)"]
    end

    KFP -->|"failed runs"| KC
    MLF -->|"metrics & runs"| MC
    K8S -->|"pod logs & events"| KK
    GC -->|"creates PR"| GH

    CORE --> KC
    CORE --> MC
    CORE --> KK
    CORE --> GC
    CORE <-->|"read/write state"| MEM
    CORE -->|"tool schemas"| TOOLS
    CORE --> CL
    CORE --> PG
    CORE --> RP

    PG --> PR
    RP --> IR
    MEM --> LOG
    CORE -.->|"optional webhook"| SLACK
```

---

## 2. Agent Poll Cycle — Flowchart

```mermaid
flowchart TD
    START([🟢 KubeAgent Starts]) --> LOAD[Load agent_memory.json]
    LOAD --> LOOP{{"⏱️ Every POLL_INTERVAL_SECONDS"}}

    LOOP --> POLL["Poll KFP API\nget_failed_runs(last_n_hours)"]
    POLL --> CHECK{Any\nfailed runs?}

    CHECK -->|"No"| SLEEP["😴 Sleep POLL_INTERVAL_SECONDS\nand repeat"]
    SLEEP --> LOOP

    CHECK -->|"Yes"| ITER["For each failed run"]
    ITER --> SEEN{Already in\nmemory?}

    SEEN -->|"Yes — skip"| ITER
    SEEN -->|"No — process"| FETCH

    subgraph DiagnosisLoop["🤖 Claude Diagnosis Loop"]
        FETCH["Fetch context:\n• Pod logs\n• K8s events\n• MLflow metrics"]
        SEND["Send to Claude claude-sonnet-4-5\nwith 8 tool definitions"]
        TOOL{Claude calls\na tool?}
        EXEC["Execute tool\nreturn result"]
        FEED["Feed result\nback to Claude"]
        DONE{stop_reason\n== end_turn?}

        FETCH --> SEND
        SEND --> TOOL
        TOOL -->|"tool_use"| EXEC
        EXEC --> FEED
        FEED --> SEND
        TOOL -->|"end_turn"| DONE
    end

    DONE -->|"Yes"| ACT

    subgraph Actions["⚡ Automated Actions"]
        ACT["Generate YAML Patch\npatch_gen.py"]
        PR["Open GitHub PR\ngithub_client.py"]
        RPT["Write Incident Report\nreporter.py"]
        UPD["Update Memory\nagent_memory.json"]

        ACT --> PR
        PR --> RPT
        RPT --> UPD
    end

    UPD --> ITER
    ITER -->|"all runs done"| SLEEP
```

---

## 3. Multi-Turn Claude Tool-Use Loop — Sequence Diagram

```mermaid
sequenceDiagram
    participant Agent as KubeAgent Core
    participant Claude as Claude claude-sonnet-4-5
    participant Tools as Tool Dispatcher
    participant KFP as KFP Connector
    participant K8s as K8s Connector
    participant MLF as MLflow Connector
    participant GH as GitHub Connector
    participant Mem as Memory Manager

    Agent->>Mem: load agent_memory.json
    Agent->>KFP: get_failed_runs(last_n_hours=1)
    KFP-->>Agent: [run_id, pipeline_name, error, state]

    loop For each new failed run
        Agent->>Claude: messages.create(system_prompt + run_context + 8 tools)

        Claude->>Tools: tool_use: get_run_logs(run_id)
        Tools->>K8s: read_namespaced_pod_log()
        K8s-->>Tools: raw log string
        Tools-->>Claude: tool_result: logs

        Claude->>Tools: tool_use: get_k8s_pod_events(namespace)
        Tools->>K8s: list_namespaced_event()
        K8s-->>Tools: [{type, reason, message, timestamp}]
        Tools-->>Claude: tool_result: events

        Claude->>Tools: tool_use: get_mlflow_experiments(kfp_run_name)
        Tools->>MLF: search_runs() + get_metric_history()
        MLF-->>Tools: [{run_id, metrics, params, status}]
        Tools-->>Claude: tool_result: mlflow_data

        Note over Claude: 🧠 Reason: classify failure,<br/>determine fix strategy

        Claude->>Tools: tool_use: generate_yaml_patch(OOM_KILL, original_yaml)
        Tools->>Tools: patch_gen.generate_patch()
        Tools-->>Claude: tool_result: patched_yaml

        Claude->>Tools: tool_use: create_github_pr(branch, file, content, title, body)
        Tools->>GH: repo.create_pull()
        GH-->>Tools: pr_url
        Tools-->>Claude: tool_result: https://github.com/.../pull/42

        Claude->>Tools: tool_use: write_incident_report(run_id, failure_type, root_cause, pr_url)
        Tools->>Tools: reporter.generate_report()
        Tools-->>Claude: tool_result: reports/run-xyz.md

        Claude->>Tools: tool_use: update_memory(run_id, FIXED, details)
        Tools->>Mem: mark_run_processed()
        Mem-->>Tools: ok
        Tools-->>Claude: tool_result: memory updated

        Claude-->>Agent: stop_reason=end_turn\n"Fixed OOM in data_preprocessing..."
        Agent->>Mem: save agent_memory.json
    end
```

---

## 4. Failure Classification — Decision Tree

```mermaid
flowchart TD
    IN["📥 Input: logs + k8s events + error_message"]

    IN --> P1{OOMKilled\nexit code 137\nmemory limit?}
    P1 -->|"✅ match"| OOM["🔴 OOM_KILL\n→ Increase memory limit ×4\n→ Patch: resources.limits.memory"]

    P1 -->|"❌"| P2{ImagePullBackOff\nErrImagePull\nmanifest unknown?}
    P2 -->|"✅ match"| IMG["🟠 WRONG_IMAGE\n→ Fix image tag → latest\n→ Patch: spec.containers[].image"]

    P2 -->|"❌"| P3{KeyError\nEnvironmentError\nos.getenv returned None?}
    P3 -->|"✅ match"| ENV["🟡 MISSING_ENV\n→ Add env var placeholder\n→ Patch: spec.containers[].env"]

    P3 -->|"❌"| P4{CrashLoopBackOff\nrestarts > 3?}
    P4 -->|"✅ match"| CL["🔵 CRASH_LOOP\n→ Add resource limits\n→ Increase initialDelaySeconds"]

    P4 -->|"❌"| P5{Insufficient cpu\nInsufficient memory\nexceeded quota?}
    P5 -->|"✅ match"| RQ["🟣 RESOURCE_QUOTA\n→ Reduce requests\n→ Add namespace annotation"]

    P5 -->|"❌"| P6{DeadlineExceeded\ncontext deadline\ntimeout?}
    P6 -->|"✅ match"| TO["🟤 TIMEOUT\n→ Increase activeDeadlineSeconds\n→ Patch pipeline timeout"]

    P6 -->|"❌"| P7{upstream step failed\ninput not found?}
    P7 -->|"✅ match"| DF["⚫ DEPENDENCY_FAIL\n→ Surface root cause\n→ Report only, no YAML patch"]

    P7 -->|"❌"| UNK["❓ UNKNOWN\n→ Log for manual review\n→ Write incident report"]

    OOM & IMG & ENV & CL & RQ & TO & DF & UNK --> OUT["📤 ClassificationResult\n{failure_type, confidence, fix_strategy, patch_target}"]
```

---

## 5. Component Interaction — Class Diagram

```mermaid
classDiagram
    class KubeAgent {
        +Settings settings
        +MemoryManager memory
        +Anthropic anthropic_client
        +KFPConnector _kfp
        +MLflowConnector _mlflow
        +K8sConnector _k8s
        +GitHubConnector _github
        +FailureClassifier classifier
        +YAMLPatchGenerator patch_gen
        +IncidentReporter reporter
        +run()
        +_run_cycle()
        +_process_failed_run(run)
        +_call_claude(messages)
        +_handle_tool_use(tool_name, tool_input)
    }

    class MemoryManager {
        +str memory_path
        +load() dict
        +save(memory)
        +is_run_processed(run_id) bool
        +mark_run_processed(run_id, status, details)
        +update_stats(failure_type)
        +get_recent_incidents(limit) list
    }

    class KFPConnector {
        +kfp.Client client
        +get_failed_runs(last_n_hours, namespace) List
        +get_run_details(run_id) Dict
        +get_run_logs(run_id, max_lines) str
        +retry_run(run_id) bool
        +list_experiments() List
    }

    class MLflowConnector {
        +MlflowClient client
        +get_recent_runs(experiment_name_filter, last_n_runs) List
        +get_run_metrics_history(run_id, metric_key) List
        +detect_metric_degradation(run_id, metric_key, threshold) bool
        +correlate_with_kfp_run(kfp_run_name) Optional~Dict~
    }

    class K8sConnector {
        +CoreV1Api v1
        +get_pod_events(namespace, pod_name_prefix) List
        +get_pod_logs(namespace, pod_name, tail_lines) str
        +get_failed_pods(namespace) List
        +list_pods_for_run(namespace, run_id) List~str~
    }

    class GitHubConnector {
        +Github github
        +Repository repo
        +create_fix_pr(branch_name, file_path, file_content, pr_title, pr_body) str
        +pr_exists_for_branch(branch_name) bool
        +_get_or_create_branch(repo, branch_name, base_branch) str
        +_file_exists(repo, file_path, branch) Tuple
    }

    class FailureClassifier {
        +classify(logs, events, error_message) ClassificationResult
        +_match_patterns(text, patterns) List~str~
        +_calculate_confidence(matched, total) float
    }

    class YAMLPatchGenerator {
        +generate_patch(failure_type, original_yaml, suggested_fix) str
        +_fix_oom(manifest, suggested_fix) dict
        +_fix_wrong_image(manifest, suggested_fix) dict
        +_fix_missing_env(manifest, suggested_fix) dict
        +_fix_crash_loop(manifest, suggested_fix) dict
        +_increase_memory(current_value, multiplier) str
    }

    class IncidentReporter {
        +str reports_dir
        +generate_report(run_id, failure_type, root_cause, fix_applied, pr_url, mlflow_correlation) str
        +_format_events_table(events) str
        +_format_metrics(metrics) str
    }

    class Settings {
        +str kfp_endpoint
        +str mlflow_tracking_uri
        +str github_token
        +str anthropic_api_key
        +int poll_interval_seconds
        +int lookback_hours
        +validate()
    }

    KubeAgent --> MemoryManager
    KubeAgent --> KFPConnector
    KubeAgent --> MLflowConnector
    KubeAgent --> K8sConnector
    KubeAgent --> GitHubConnector
    KubeAgent --> FailureClassifier
    KubeAgent --> YAMLPatchGenerator
    KubeAgent --> IncidentReporter
    KubeAgent --> Settings
```

---

## 6. Demo Scenario — State Diagram

```mermaid
stateDiagram-v2
    [*] --> PipelineSubmitted : python demo/broken_pipeline.py

    PipelineSubmitted --> Running : KFP schedules run
    Running --> Preprocessing : data_preprocessing task starts

    state Preprocessing {
        [*] --> Allocating : np.random.rand(10000,10000) ~800MB
        Allocating --> OOMKilled : memory limit=50Mi exceeded
        OOMKilled --> [*]
    }

    Preprocessing --> TrainingQueued : (parallel task)

    state TrainingQueued {
        [*] --> ImagePull : pull tensorflow/tensorflow:2.99.99-gpu
        ImagePull --> ImagePullBackOff : tag does not exist
        ImagePullBackOff --> [*]
    }

    TrainingQueued --> EvalQueued : (parallel task)

    state EvalQueued {
        [*] --> EnvLookup : os.environ["MODEL_REGISTRY_BUCKET"]
        EnvLookup --> KeyError : env var not set
        KeyError --> [*]
    }

    Preprocessing --> FAILED
    TrainingQueued --> FAILED
    EvalQueued --> FAILED

    FAILED --> KubeAgentDetects : next poll cycle

    state KubeAgentDetects {
        [*] --> FetchLogs
        FetchLogs --> FetchEvents
        FetchEvents --> FetchMLflow
        FetchMLflow --> ClaudeReasoning : send all context
        ClaudeReasoning --> OOM_Classified : OOMKilled → OOM_KILL
        ClaudeReasoning --> Image_Classified : ImagePullBackOff → WRONG_IMAGE
        ClaudeReasoning --> Env_Classified : KeyError → MISSING_ENV
    }

    KubeAgentDetects --> PatchGenerated : generate_yaml_patch × 3
    PatchGenerated --> PROpened : create_github_pr × 3
    PROpened --> ReportWritten : write_incident_report
    ReportWritten --> MemoryUpdated : update_memory(status=FIXED)
    MemoryUpdated --> [*] : run marked as processed ✅
```

---

## 7. Data Flow — End-to-End

```mermaid
flowchart LR
    subgraph Input["📥 Data Sources"]
        R1["KFP Run API\n{run_id, state, error}"]
        R2["K8s Pod Logs\nraw stdout/stderr"]
        R3["K8s Events\n{reason, message}"]
        R4["MLflow Metrics\n{accuracy, loss, ...}"]
    end

    subgraph Processing["🧠 KubeAgent Processing"]
        AGG["Aggregate\nContext"]
        CLF["Classify\nFailure Type"]
        CLAUDE["Claude claude-sonnet-4-5\ntool_use reasoning"]
        PATCH["Generate\nYAML Patch"]
    end

    subgraph Output["📤 Outputs"]
        O1["GitHub PR\nwith YAML fix"]
        O2["Incident Report\nreports/*.md"]
        O3["Memory Update\nagent_memory.json"]
        O4["Agent Logs\nstdout"]
    end

    R1 & R2 & R3 & R4 --> AGG
    AGG --> CLF
    CLF -->|"FailureType + confidence"| CLAUDE
    CLAUDE -->|"tool: generate_yaml_patch"| PATCH
    PATCH -->|"tool: create_github_pr"| O1
    CLAUDE -->|"tool: write_incident_report"| O2
    CLAUDE -->|"tool: update_memory"| O3
    CLAUDE --> O4
```

---

## 8. GitHub PR Creation Flow

```mermaid
sequenceDiagram
    participant PG as patch_gen.py
    participant GC as github_client.py
    participant GH as GitHub API

    PG->>GC: create_fix_pr(branch_name, file_path, patched_yaml, title, body)

    GC->>GH: GET /repos/{owner}/{repo}/branches/main
    GH-->>GC: {sha: "abc123..."}

    GC->>GH: POST /repos/{owner}/{repo}/git/refs\nbody: {ref: "refs/heads/fix/oom-run-xyz", sha: "abc123"}
    GH-->>GC: branch created

    GC->>GH: GET /repos/{owner}/{repo}/contents/{file_path}?ref=fix/oom-run-xyz
    GH-->>GC: 404 Not Found (new file)

    GC->>GH: PUT /repos/{owner}/{repo}/contents/{file_path}\nbody: {message, content: base64(yaml), branch}
    GH-->>GC: {content: {sha: "def456"}}

    GC->>GH: POST /repos/{owner}/{repo}/pulls\nbody: {title, body, head: fix/oom-run-xyz, base: main}
    GH-->>GC: {html_url: "https://github.com/.../pull/42"}

    GC-->>PG: "https://github.com/.../pull/42"
```

---

## 9. Memory State Machine

```mermaid
stateDiagram-v2
    [*] --> Empty : First run\n(agent_memory.json created)

    Empty --> Tracking : Failed run detected

    state Tracking {
        [*] --> Observed : run fetched from KFP
        Observed --> Diagnosing : Claude tool_use loop started
        Diagnosing --> Fixing : patch generated
        Fixing --> FIXED : PR opened successfully
        Fixing --> REPORTED : PR failed, report only
        Diagnosing --> IGNORED : below confidence threshold
    }

    FIXED --> [*] : is_run_processed() → true
    REPORTED --> [*] : is_run_processed() → true
    IGNORED --> [*] : is_run_processed() → true

    note right of FIXED
        memory entry:
        {
          "status": "FIXED",
          "failure_type": "OOM_KILL",
          "pr_url": "https://...",
          "timestamp": "ISO8601"
        }
    end note
```

---

## 10. Deployment Architecture (kind + Docker)

```mermaid
graph TB
    subgraph LocalMachine["💻 Local Machine / CI"]
        DEV["Developer\nTerminal"]
        DEMO["demo/broken_pipeline.py"]
        AGENT["python -m kubeagent.agent.core"]
    end

    subgraph KindCluster["⎈ kind Cluster (kubeagent)"]
        subgraph KubeflowNS["namespace: kubeflow"]
            KFP_API["ml-pipeline\n:8888 → :8887 (port-forward)"]
            KFP_UI["ml-pipeline-ui\n:80 → :8888 (port-forward)"]
            PODS["Pipeline Pods\n(broken-ml-training-pipeline)"]
        end
    end

    subgraph DockerCompose["🐳 Docker Compose"]
        MLF_SVC["mlflow:latest\n:5000"]
    end

    subgraph GitHub["🐙 GitHub"]
        REPO["danish9039/kubeagent\n(or target fix repo)"]
    end

    subgraph Anthropic["🤖 Anthropic API"]
        CLAUDE_API["claude-sonnet-4-5\napi.anthropic.com"]
    end

    DEV -->|"kubectl apply"| KindCluster
    DEMO -->|"kfp.Client(host=:8887)"| KFP_API
    DEMO -->|"mlflow.log_metric()"| MLF_SVC

    AGENT -->|"KFPConnector"| KFP_API
    AGENT -->|"K8sConnector\n(kubeconfig)"| KindCluster
    AGENT -->|"MLflowConnector"| MLF_SVC
    AGENT -->|"GitHubConnector"| REPO
    AGENT -->|"anthropic.Anthropic()"| CLAUDE_API

    KFP_API --> PODS
```
