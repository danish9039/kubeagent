"""
Claude tool_use definitions and dispatch logic for KubeAgent.

TOOL_DEFINITIONS is the list passed directly to the Anthropic API.
dispatch_tool_call() routes a tool_use response to the correct connector method.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schemas  (Anthropic tool_use format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: List[Dict] = [
    {
        "name": "get_failed_pipeline_runs",
        "description": (
            "Retrieve KFP pipeline runs that have failed within a recent time window. "
            "Use this as the first step to discover what needs investigation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "last_n_hours": {
                    "type": "integer",
                    "description": "How many hours back to search for failed runs (e.g. 1, 6, 24).",
                },
                "namespace": {
                    "type": "string",
                    "description": (
                        "Kubernetes namespace to search in. "
                        "Defaults to the configured KFP namespace if omitted."
                    ),
                },
            },
            "required": ["last_n_hours"],
        },
    },
    {
        "name": "get_run_logs",
        "description": (
            "Retrieve pod logs for a specific KFP run. "
            "Use this after identifying a failed run to understand the error in detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The KFP run ID to fetch logs for.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace where the run's pods live.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum log lines to return per pod (default 200).",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_mlflow_experiments",
        "description": (
            "Fetch recent MLflow runs, optionally filtered by experiment name. "
            "Use this to correlate KFP failures with MLflow metrics (e.g. sudden accuracy drops)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "experiment_name_filter": {
                    "type": "string",
                    "description": (
                        "Substring to filter experiment names by. "
                        "Leave empty to retrieve all experiments."
                    ),
                },
                "last_n_runs": {
                    "type": "integer",
                    "description": "Number of most-recent runs to return per experiment (default 20).",
                },
                "kfp_run_name": {
                    "type": "string",
                    "description": (
                        "If provided, attempt to correlate this KFP run name with an MLflow run "
                        "using tag matching."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_k8s_pod_events",
        "description": (
            "Retrieve Kubernetes events for pods in a namespace. "
            "Useful for diagnosing OOMKilled, ImagePullBackOff, and scheduling failures."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace to query events from.",
                },
                "pod_name_prefix": {
                    "type": "string",
                    "description": (
                        "Optional pod name or prefix to filter events to a specific pod. "
                        "Leave empty for all pods in the namespace."
                    ),
                },
                "run_id": {
                    "type": "string",
                    "description": (
                        "If provided, first resolve the pods for this run ID "
                        "and then fetch their events."
                    ),
                },
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "generate_yaml_patch",
        "description": (
            "Generate a patched Kubernetes YAML manifest that fixes the identified failure. "
            "Returns the modified YAML as a string ready for committing to Git."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "failure_type": {
                    "type": "string",
                    "enum": [
                        "OOM_KILL",
                        "WRONG_IMAGE",
                        "MISSING_ENV",
                        "CRASH_LOOP",
                        "RESOURCE_QUOTA",
                        "TIMEOUT",
                        "DEPENDENCY_FAIL",
                        "UNKNOWN",
                    ],
                    "description": "The failure type as classified by the classifier.",
                },
                "original_yaml": {
                    "type": "string",
                    "description": "The original Kubernetes YAML manifest content.",
                },
                "suggested_fix": {
                    "type": "object",
                    "description": (
                        "Optional hints to guide the fix. Keys depend on failure_type: "
                        "OOM_KILL → memory_multiplier (float), min_memory (string like '512Mi'); "
                        "WRONG_IMAGE → fallback_tag (string); "
                        "MISSING_ENV → env_key (string), placeholder_value (string); "
                        "CRASH_LOOP → initial_delay_seconds (int); "
                        "RESOURCE_QUOTA → request_reduction_factor (float); "
                        "TIMEOUT → timeout_multiplier (float)."
                    ),
                },
            },
            "required": ["failure_type", "original_yaml"],
        },
    },
    {
        "name": "create_github_pr",
        "description": (
            "Commit a patched YAML file to a new branch and open a GitHub pull request. "
            "Call this after generating a YAML patch to propose the fix for review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {
                    "type": "string",
                    "description": "Branch name for the fix, e.g. 'kubeagent/fix-oom-run-abc123'.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Repository-relative path of the file to create or update.",
                },
                "file_content": {
                    "type": "string",
                    "description": "Full YAML content of the fixed manifest.",
                },
                "pr_title": {
                    "type": "string",
                    "description": "Title for the pull request.",
                },
                "pr_body": {
                    "type": "string",
                    "description": "Markdown body describing the change and its rationale.",
                },
                "base_branch": {
                    "type": "string",
                    "description": "Branch to merge into (default 'main').",
                },
            },
            "required": ["branch_name", "file_path", "file_content", "pr_title", "pr_body"],
        },
    },
    {
        "name": "write_incident_report",
        "description": (
            "Write a structured Markdown incident report to disk. "
            "Call this after investigating a failure to leave a permanent audit trail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "KFP run ID for this incident.",
                },
                "failure_type": {
                    "type": "string",
                    "description": "Detected failure category string.",
                },
                "root_cause": {
                    "type": "string",
                    "description": "Short description of the root cause.",
                },
                "fix_applied": {
                    "type": "string",
                    "description": "Description of the remediation applied.",
                },
                "pr_url": {
                    "type": "string",
                    "description": "URL of the GitHub PR, if one was created.",
                },
                "logs_excerpt": {
                    "type": "string",
                    "description": "Relevant log snippet to include in the report.",
                },
            },
            "required": ["run_id", "failure_type", "root_cause", "fix_applied"],
        },
    },
    {
        "name": "update_memory",
        "description": (
            "Mark a KFP run as processed in persistent agent memory and record the outcome. "
            "Always call this as the final step for each run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "KFP run ID to mark as processed.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Outcome string, e.g. 'pr_created', 'report_only', 'no_fix_needed', "
                        "'error'."
                    ),
                },
                "details": {
                    "type": "object",
                    "description": "Arbitrary dict with additional context (PR URL, failure type, etc.).",
                },
                "failure_type": {
                    "type": "string",
                    "description": "Failure type string for stats tracking (optional).",
                },
            },
            "required": ["run_id", "status"],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatch function
# ---------------------------------------------------------------------------

def dispatch_tool_call(
    tool_name: str,
    tool_input: Dict[str, Any],
    connectors: Dict[str, Any],
) -> Any:
    """Route a Claude tool_use request to the appropriate connector method.

    Args:
        tool_name:  The ``name`` field from the Claude tool_use block.
        tool_input: The ``input`` dict from the Claude tool_use block.
        connectors: Dict containing connector instances under keys:
                    ``"kfp"``, ``"mlflow"``, ``"k8s"``, ``"github"``,
                    ``"memory"``, ``"classifier"``, ``"patch_gen"``,
                    ``"reporter"``.

    Returns:
        Serialisable result (str, dict, list, or bool) to feed back to Claude.
    """
    logger.info("Dispatching tool: %s  input=%s", tool_name, json.dumps(tool_input)[:200])

    try:
        if tool_name == "get_failed_pipeline_runs":
            return _tool_get_failed_runs(tool_input, connectors)

        elif tool_name == "get_run_logs":
            return _tool_get_run_logs(tool_input, connectors)

        elif tool_name == "get_mlflow_experiments":
            return _tool_get_mlflow(tool_input, connectors)

        elif tool_name == "get_k8s_pod_events":
            return _tool_get_pod_events(tool_input, connectors)

        elif tool_name == "generate_yaml_patch":
            return _tool_generate_patch(tool_input, connectors)

        elif tool_name == "create_github_pr":
            return _tool_create_pr(tool_input, connectors)

        elif tool_name == "write_incident_report":
            return _tool_write_report(tool_input, connectors)

        elif tool_name == "update_memory":
            return _tool_update_memory(tool_input, connectors)

        else:
            logger.warning("Unknown tool name: %s", tool_name)
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as exc:
        logger.error("Tool %s raised an exception: %s", tool_name, exc, exc_info=True)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Individual tool handlers
# ---------------------------------------------------------------------------

def _tool_get_failed_runs(tool_input: Dict, connectors: Dict) -> List[Dict]:
    kfp = connectors["kfp"]
    last_n_hours = tool_input.get("last_n_hours", 1)
    namespace = tool_input.get("namespace")
    return kfp.get_failed_runs(last_n_hours=last_n_hours, namespace=namespace)


def _tool_get_run_logs(tool_input: Dict, connectors: Dict) -> str:
    run_id = tool_input["run_id"]
    namespace = tool_input.get("namespace")
    max_lines = tool_input.get("max_lines", 200)

    k8s: Any = connectors.get("k8s")
    kfp: Any = connectors["kfp"]

    # Prefer Kubernetes pod logs if k8s connector is available
    if k8s and namespace:
        pod_names = k8s.list_pods_for_run(namespace=namespace, run_id=run_id)
        if pod_names:
            log_parts = []
            for pod_name in pod_names[:3]:  # cap at 3 pods to avoid flooding context
                logs = k8s.get_pod_logs(
                    namespace=namespace,
                    pod_name=pod_name,
                    tail_lines=max_lines,
                )
                log_parts.append(f"=== Pod: {pod_name} ===\n{logs}")
            return "\n\n".join(log_parts)

    # Fallback to KFP API logs
    return kfp.get_run_logs(run_id=run_id, max_lines=max_lines)


def _tool_get_mlflow(tool_input: Dict, connectors: Dict) -> Any:
    mlflow = connectors.get("mlflow")
    if not mlflow:
        return {"error": "MLflow connector not configured."}

    kfp_run_name = tool_input.get("kfp_run_name")
    if kfp_run_name:
        result = mlflow.correlate_with_kfp_run(kfp_run_name)
        return result or {"message": "No correlated MLflow run found."}

    return mlflow.get_recent_runs(
        experiment_name_filter=tool_input.get("experiment_name_filter"),
        last_n_runs=tool_input.get("last_n_runs", 20),
    )


def _tool_get_pod_events(tool_input: Dict, connectors: Dict) -> List[Dict]:
    k8s = connectors.get("k8s")
    if not k8s:
        return [{"error": "Kubernetes connector not configured."}]

    namespace = tool_input["namespace"]
    pod_name_prefix = tool_input.get("pod_name_prefix")
    run_id = tool_input.get("run_id")

    # If run_id provided, resolve pod names first
    if run_id and not pod_name_prefix:
        pods = k8s.list_pods_for_run(namespace=namespace, run_id=run_id)
        if pods:
            pod_name_prefix = pods[0]  # use first pod for event filtering

    return k8s.get_pod_events(namespace=namespace, pod_name_prefix=pod_name_prefix)


def _tool_generate_patch(tool_input: Dict, connectors: Dict) -> str:
    from kubeagent.reasoning.classifier import FailureType
    patch_gen = connectors["patch_gen"]

    failure_type_str = tool_input["failure_type"]
    try:
        failure_type = FailureType(failure_type_str)
    except ValueError:
        failure_type = FailureType.UNKNOWN

    original_yaml = tool_input["original_yaml"]
    suggested_fix = tool_input.get("suggested_fix", {})

    return patch_gen.generate_patch(
        failure_type=failure_type,
        original_yaml=original_yaml,
        suggested_fix=suggested_fix,
    )


def _tool_create_pr(tool_input: Dict, connectors: Dict) -> str:
    github = connectors.get("github")
    if not github:
        return "GitHub connector not configured – PR creation skipped."

    pr_url = github.create_fix_pr(
        branch_name=tool_input["branch_name"],
        file_path=tool_input["file_path"],
        file_content=tool_input["file_content"],
        pr_title=tool_input["pr_title"],
        pr_body=tool_input["pr_body"],
        base_branch=tool_input.get("base_branch", "main"),
    )

    # Update memory PR counter
    memory_mgr = connectors.get("memory")
    if memory_mgr:
        memory_mgr.increment_pr_count()

    return pr_url


def _tool_write_report(tool_input: Dict, connectors: Dict) -> str:
    reporter = connectors["reporter"]
    k8s_events = tool_input.get("k8s_events", [])

    report_path = reporter.generate_report(
        run_id=tool_input["run_id"],
        failure_type=tool_input["failure_type"],
        root_cause=tool_input["root_cause"],
        fix_applied=tool_input["fix_applied"],
        pr_url=tool_input.get("pr_url"),
        logs_excerpt=tool_input.get("logs_excerpt"),
        k8s_events=k8s_events,
    )

    memory_mgr = connectors.get("memory")
    if memory_mgr:
        memory_mgr.increment_report_count()

    return report_path


def _tool_update_memory(tool_input: Dict, connectors: Dict) -> Dict:
    memory_mgr = connectors["memory"]
    run_id = tool_input["run_id"]
    status = tool_input["status"]
    details = tool_input.get("details", {})
    failure_type = tool_input.get("failure_type")

    memory_mgr.mark_run_processed(run_id=run_id, status=status, details=details)

    if failure_type:
        memory_mgr.update_stats(failure_type)

    return {"success": True, "run_id": run_id, "status": status}
