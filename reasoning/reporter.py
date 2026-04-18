"""
Markdown incident report generator for KubeAgent.

Each report is written to a timestamped file under the configured reports
directory and is formatted for readability in GitHub Markdown.
"""
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional


class IncidentReporter:
    """Generates structured Markdown incident reports and writes them to disk."""

    def __init__(self, reports_dir: str = "reports") -> None:
        """Initialise the reporter.

        Args:
            reports_dir: Directory where report files will be written.
                         Created automatically if it does not exist.
        """
        self.reports_dir = os.path.abspath(reports_dir)
        os.makedirs(self.reports_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_report(
        self,
        run_id: str,
        failure_type: str,
        root_cause: str,
        fix_applied: str,
        pr_url: Optional[str] = None,
        mlflow_correlation: Optional[Dict] = None,
        logs_excerpt: Optional[str] = None,
        k8s_events: Optional[List[Dict]] = None,
    ) -> str:
        """Write a Markdown incident report to disk.

        Args:
            run_id:              KFP run identifier.
            failure_type:        Human-readable failure category string.
            root_cause:          Short description of the detected root cause.
            fix_applied:         Description of the fix that was (or will be)
                                 applied.
            pr_url:              URL of the GitHub pull request, if created.
            mlflow_correlation:  Dict of correlated MLflow run data, if any.
            logs_excerpt:        Relevant excerpt from pod logs.
            k8s_events:          List of Kubernetes event dicts.

        Returns:
            Absolute path to the written report file.
        """
        timestamp = datetime.now(tz=timezone.utc)
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
        short_run = run_id[:8] if len(run_id) >= 8 else run_id
        filename = f"incident_{ts_str}_{short_run}.md"
        file_path = os.path.join(self.reports_dir, filename)

        content = self._render(
            run_id=run_id,
            failure_type=failure_type,
            root_cause=root_cause,
            fix_applied=fix_applied,
            timestamp=timestamp,
            pr_url=pr_url,
            mlflow_correlation=mlflow_correlation,
            logs_excerpt=logs_excerpt,
            k8s_events=k8s_events or [],
        )

        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(content)

        return file_path

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _render(
        self,
        run_id: str,
        failure_type: str,
        root_cause: str,
        fix_applied: str,
        timestamp: datetime,
        pr_url: Optional[str],
        mlflow_correlation: Optional[Dict],
        logs_excerpt: Optional[str],
        k8s_events: List[Dict],
    ) -> str:
        """Assemble the full Markdown report string.

        Args:
            All parameters correspond 1-1 to :meth:`generate_report`.

        Returns:
            A multi-line Markdown string.
        """
        ts_human = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        sections: List[str] = []

        # ---- Header -------------------------------------------------------
        sections.append(f"# Incident Report — {failure_type}")
        sections.append(f"\n**Generated:** {ts_human}  ")
        sections.append(f"**Run ID:** `{run_id}`  ")
        sections.append(f"**Failure Type:** `{failure_type}`  ")
        if pr_url:
            sections.append(f"**Fix PR:** [{pr_url}]({pr_url})  ")

        # ---- Summary -------------------------------------------------------
        sections.append("\n---\n")
        sections.append("## Summary\n")
        sections.append(f"| Field | Value |")
        sections.append(f"|---|---|")
        sections.append(f"| Root Cause | {root_cause} |")
        sections.append(f"| Fix Applied | {fix_applied} |")
        sections.append(f"| PR Created | {'Yes – ' + pr_url if pr_url else 'No'} |")

        # ---- MLflow correlation -------------------------------------------
        if mlflow_correlation:
            sections.append("\n## MLflow Correlation\n")
            sections.append(self._format_metrics(mlflow_correlation))

        # ---- Kubernetes Events --------------------------------------------
        if k8s_events:
            sections.append("\n## Kubernetes Events\n")
            sections.append(self._format_events_table(k8s_events))

        # ---- Log excerpt --------------------------------------------------
        if logs_excerpt:
            sections.append("\n## Log Excerpt\n")
            # Truncate very long excerpts to keep the report readable
            excerpt = logs_excerpt[:3000]
            if len(logs_excerpt) > 3000:
                excerpt += "\n\n… *(truncated – see full logs in Kubernetes)*"
            sections.append("```\n" + excerpt + "\n```")

        # ---- Recommendations ---------------------------------------------
        sections.append("\n## Next Steps\n")
        sections.append(self._recommendations(failure_type, pr_url))

        # ---- Footer -------------------------------------------------------
        sections.append("\n---\n")
        sections.append(
            f"*Report generated automatically by KubeAgent at {ts_human}.*"
        )

        return "\n".join(sections) + "\n"

    def _format_events_table(self, events: List[Dict]) -> str:
        """Render a Markdown table of Kubernetes events.

        Args:
            events: List of event dicts with keys:
                    ``type``, ``reason``, ``message``, ``timestamp``, ``count``.

        Returns:
            Markdown table string.
        """
        if not events:
            return "*No events recorded.*"

        rows = ["| Type | Reason | Count | Timestamp | Message |", "|---|---|---|---|---|"]
        for ev in events:
            ev_type = ev.get("type", "")
            reason = ev.get("reason", "")
            count = str(ev.get("count", ""))
            ts = str(ev.get("timestamp", ev.get("last_timestamp", "")))
            # Escape pipe characters in the message
            message = ev.get("message", "").replace("|", "\\|")[:120]
            rows.append(f"| {ev_type} | {reason} | {count} | {ts} | {message} |")

        return "\n".join(rows)

    def _format_metrics(self, mlflow_data: Dict) -> str:
        """Render MLflow correlation data as a readable Markdown block.

        Args:
            mlflow_data: Dict, typically the result of
                         :meth:`~MLflowConnector.correlate_with_kfp_run`.

        Returns:
            Markdown string.
        """
        if not mlflow_data:
            return "*No MLflow data.*"

        lines = []
        run_id = mlflow_data.get("run_id", "unknown")
        experiment = mlflow_data.get("experiment_name", "unknown")
        status = mlflow_data.get("status", "unknown")
        lines.append(f"**MLflow Run:** `{run_id}`  ")
        lines.append(f"**Experiment:** {experiment}  ")
        lines.append(f"**Status:** {status}  ")

        metrics = mlflow_data.get("metrics", {})
        if metrics:
            lines.append("\n**Metrics:**\n")
            lines.append("| Metric | Value |")
            lines.append("|---|---|")
            for k, v in metrics.items():
                lines.append(f"| {k} | {v} |")

        params = mlflow_data.get("params", {})
        if params:
            lines.append("\n**Parameters:**\n")
            lines.append("| Parameter | Value |")
            lines.append("|---|---|")
            for k, v in params.items():
                lines.append(f"| {k} | {v} |")

        return "\n".join(lines)

    @staticmethod
    def _recommendations(failure_type: str, pr_url: Optional[str]) -> str:
        """Return a short bulleted action list tailored to the failure type.

        Args:
            failure_type: String representation of the failure category.
            pr_url:       If a PR was created, include a review step.

        Returns:
            Markdown bullet list.
        """
        items = []

        type_upper = failure_type.upper()

        if "OOM" in type_upper:
            items.append("- Review the new memory limits in the generated PR and verify they are appropriate for your workload.")
            items.append("- Consider profiling the pipeline step to understand peak memory usage.")
        elif "IMAGE" in type_upper:
            items.append("- Verify the container registry credentials are up to date.")
            items.append("- Pin the image to a specific digest once the correct tag is confirmed.")
        elif "ENV" in type_upper or "MISSING" in type_upper:
            items.append("- Add the missing environment variable to the Kubernetes Secret or ConfigMap.")
            items.append("- Update the pipeline YAML to reference the correct secret key.")
        elif "CRASH" in type_upper:
            items.append("- Inspect the full container logs for the root-cause exception.")
            items.append("- Consider adding a readiness probe if the service needs a warm-up period.")
        elif "QUOTA" in type_upper:
            items.append("- Request a resource quota increase from the cluster administrator.")
            items.append("- Verify no other workloads are competing for the same namespace quota.")
        elif "TIMEOUT" in type_upper:
            items.append("- Investigate whether the pipeline step can be optimised or parallelised.")
            items.append("- Confirm the new timeout value won't mask underlying performance issues.")
        elif "DEPEND" in type_upper:
            items.append("- Check the health of upstream services and databases.")
            items.append("- Review upstream task logs for their own failure causes.")
        else:
            items.append("- Manually investigate the attached logs and events.")

        if pr_url:
            items.append(f"- Review and merge the automated fix PR: {pr_url}")
        else:
            items.append("- No automated fix was applied – manual remediation is required.")

        return "\n".join(items)
