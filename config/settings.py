"""
KubeAgent configuration settings loaded from environment variables.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Settings:
    """All configuration for KubeAgent, sourced from environment variables with defaults."""

    # ---------------------------------------------------------------------------
    # Kubeflow Pipelines
    # ---------------------------------------------------------------------------
    kfp_endpoint: str = field(
        default_factory=lambda: os.getenv("KFP_ENDPOINT", "http://localhost:8888")
    )
    kfp_namespace: str = field(
        default_factory=lambda: os.getenv("KFP_NAMESPACE", "kubeflow")
    )
    kfp_token: Optional[str] = field(
        default_factory=lambda: os.getenv("KFP_TOKEN")
    )

    # ---------------------------------------------------------------------------
    # MLflow
    # ---------------------------------------------------------------------------
    mlflow_tracking_uri: str = field(
        default_factory=lambda: os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
    )

    # ---------------------------------------------------------------------------
    # GitHub
    # ---------------------------------------------------------------------------
    github_token: str = field(
        default_factory=lambda: os.getenv("GITHUB_TOKEN", "")
    )
    github_repo: str = field(
        default_factory=lambda: os.getenv("GITHUB_REPO", "owner/repo")
    )
    github_base_branch: str = field(
        default_factory=lambda: os.getenv("GITHUB_BASE_BRANCH", "main")
    )

    # ---------------------------------------------------------------------------
    # Anthropic / Claude
    # ---------------------------------------------------------------------------
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    claude_model: str = field(
        default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
    )

    # ---------------------------------------------------------------------------
    # Agent behaviour
    # ---------------------------------------------------------------------------
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    )
    lookback_hours: int = field(
        default_factory=lambda: int(os.getenv("LOOKBACK_HOURS", "1"))
    )
    memory_path: str = field(
        default_factory=lambda: os.getenv("MEMORY_PATH", "memory/agent_memory.json")
    )
    reports_dir: str = field(
        default_factory=lambda: os.getenv("REPORTS_DIR", "reports")
    )

    # ---------------------------------------------------------------------------
    # Kubernetes
    # ---------------------------------------------------------------------------
    k8s_in_cluster: bool = field(
        default_factory=lambda: os.getenv("K8S_IN_CLUSTER", "false").lower() == "true"
    )
    k8s_namespace: str = field(
        default_factory=lambda: os.getenv("K8S_NAMESPACE", "kubeflow")
    )

    # ---------------------------------------------------------------------------
    # Slack (optional notification webhook)
    # ---------------------------------------------------------------------------
    slack_webhook_url: Optional[str] = field(
        default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL")
    )

    # ---------------------------------------------------------------------------
    # Safety / iteration limits
    # ---------------------------------------------------------------------------
    max_tool_iterations: int = field(
        default_factory=lambda: int(os.getenv("MAX_TOOL_ITERATIONS", "10"))
    )

    def validate(self) -> None:
        """Raise ValueError if any required configuration field is missing or empty.

        Required fields are those without sensible defaults that would cause the
        agent to fail silently: ANTHROPIC_API_KEY, GITHUB_TOKEN, and GITHUB_REPO.
        """
        errors = []

        if not self.anthropic_api_key:
            errors.append(
                "ANTHROPIC_API_KEY is not set. "
                "Export it as an environment variable before starting the agent."
            )

        if not self.github_token:
            errors.append(
                "GITHUB_TOKEN is not set. "
                "A GitHub personal access token with 'repo' scope is required."
            )

        if self.github_repo == "owner/repo":
            errors.append(
                "GITHUB_REPO is still set to the placeholder 'owner/repo'. "
                "Set it to the actual repository, e.g. 'my-org/ml-pipelines'."
            )

        if self.poll_interval_seconds < 10:
            errors.append(
                f"POLL_INTERVAL_SECONDS={self.poll_interval_seconds} is dangerously low. "
                "Minimum recommended value is 10 seconds."
            )

        if self.max_tool_iterations < 1:
            errors.append(
                f"MAX_TOOL_ITERATIONS={self.max_tool_iterations} must be at least 1."
            )

        if errors:
            raise ValueError("Settings validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
