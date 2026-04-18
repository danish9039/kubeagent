"""
KubeAgent main loop.

Polls Kubeflow Pipelines for failed runs, invokes Claude claude-sonnet-4-5 with
a rich tool-use loop to diagnose each failure, and orchestrates automated
remediation (YAML patch → GitHub PR → incident report).
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic

from kubeagent.agent.memory import MemoryManager
from kubeagent.agent.tools import TOOL_DEFINITIONS, dispatch_tool_call
from kubeagent.config.settings import Settings
from kubeagent.connectors.github_client import GitHubConnector
from kubeagent.connectors.k8s_client import K8sConnector
from kubeagent.connectors.kfp_client import KFPConnector
from kubeagent.connectors.mlflow_client import MLflowConnector
from kubeagent.reasoning.classifier import FailureClassifier
from kubeagent.reasoning.patch_gen import YAMLPatchGenerator
from kubeagent.reasoning.reporter import IncidentReporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class KubeAgent:
    """Main agent class that runs the polling and remediation loop."""

    def __init__(self) -> None:
        """Initialise settings, connectors, and the Anthropic client."""
        self.settings = Settings()
        try:
            self.settings.validate()
        except ValueError as exc:
            logger.error("Configuration error:\n%s", exc)
            sys.exit(1)

        # Core services
        self.memory = MemoryManager(self.settings.memory_path)
        self.anthropic_client = anthropic.Anthropic(
            api_key=self.settings.anthropic_api_key
        )

        # Connectors (initialised separately so partial failures don't abort startup)
        self._kfp: Optional[KFPConnector] = None
        self._mlflow: Optional[MLflowConnector] = None
        self._k8s: Optional[K8sConnector] = None
        self._github: Optional[GitHubConnector] = None

        self._init_connectors()

        # Reasoning modules
        self.classifier = FailureClassifier()
        self.patch_gen = YAMLPatchGenerator()
        self.reporter = IncidentReporter(reports_dir=self.settings.reports_dir)

        # Build the connectors dict passed to dispatch_tool_call
        self._connectors: Dict[str, Any] = {
            "kfp": self._kfp,
            "mlflow": self._mlflow,
            "k8s": self._k8s,
            "github": self._github,
            "memory": self.memory,
            "classifier": self.classifier,
            "patch_gen": self.patch_gen,
            "reporter": self.reporter,
        }

        # Shutdown flag
        self._running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info("KubeAgent initialised. Model: %s", self.settings.claude_model)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the main polling loop.

        Each cycle:
        1. Fetches failed KFP runs from the last ``lookback_hours`` hours.
        2. Skips runs already recorded in memory.
        3. Processes new failures through the Claude tool-use loop.
        4. Saves memory and sleeps until the next poll interval.
        """
        logger.info(
            "Starting polling loop. Interval: %ds, Lookback: %dh.",
            self.settings.poll_interval_seconds,
            self.settings.lookback_hours,
        )

        while self._running:
            cycle_start = datetime.now(tz=timezone.utc)
            logger.info("=== Polling cycle started at %s ===", cycle_start.isoformat())

            try:
                self._run_cycle()
            except Exception as exc:
                logger.error(
                    "Unhandled exception in polling cycle: %s", exc, exc_info=True
                )

            if not self._running:
                break

            elapsed = (datetime.now(tz=timezone.utc) - cycle_start).total_seconds()
            sleep_time = max(0, self.settings.poll_interval_seconds - elapsed)
            logger.info(
                "Cycle completed in %.1fs. Next poll in %.0fs.", elapsed, sleep_time
            )
            self._interruptible_sleep(sleep_time)

        logger.info("KubeAgent shut down cleanly.")

    # ------------------------------------------------------------------
    # Single poll cycle
    # ------------------------------------------------------------------

    def _run_cycle(self) -> None:
        """Execute one poll-and-process cycle."""
        if self._kfp is None:
            logger.error("KFP connector unavailable – skipping cycle.")
            return

        failed_runs = self._kfp.get_failed_runs(
            last_n_hours=self.settings.lookback_hours,
            namespace=self.settings.kfp_namespace,
        )

        if not failed_runs:
            logger.info("No failed runs found.")
            return

        logger.info("Found %d failed run(s) to process.", len(failed_runs))
        for run in failed_runs:
            run_id = run.get("run_id", "unknown")
            if self.memory.is_run_processed(run_id):
                logger.debug("Run %s already processed – skipping.", run_id)
                continue
            self._process_failed_run(run)

    # ------------------------------------------------------------------
    # Per-run processing
    # ------------------------------------------------------------------

    def _process_failed_run(self, run: Dict) -> None:
        """Diagnose and remediate a single failed KFP run using Claude.

        Runs a multi-turn tool-use conversation with Claude claude-sonnet-4-5.
        Each turn Claude may invoke one or more tools; the agent executes
        those tools and feeds the results back until Claude produces a final
        text response (``stop_reason == "end_turn"``).

        Args:
            run: Normalised run dict from :class:`KFPConnector`.
        """
        run_id = run.get("run_id", "unknown")
        logger.info("Processing failed run: %s (%s)", run.get("pipeline_name", ""), run_id)

        messages: List[Dict] = [
            {"role": "user", "content": self._build_user_message(run)},
        ]

        iteration = 0
        final_response: Optional[str] = None

        while iteration < self.settings.max_tool_iterations:
            iteration += 1
            logger.debug("Claude turn %d for run %s.", iteration, run_id)

            try:
                # TODO: Call Claude claude-sonnet-4-5 here
                # response = self.anthropic_client.messages.create(
                #     model=self.settings.claude_model,
                #     max_tokens=4096,
                #     system=self._build_system_prompt(),
                #     tools=TOOL_DEFINITIONS,
                #     messages=messages,
                # )
                response = self._call_claude(messages)
            except anthropic.APIError as exc:
                logger.error(
                    "Anthropic API error on turn %d for run %s: %s",
                    iteration, run_id, exc,
                )
                break

            # Append Claude's response to the message history
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # Extract final text from the last assistant message
                for block in response.content:
                    if hasattr(block, "text"):
                        final_response = block.text
                        break
                logger.info(
                    "Claude finished analysis for run %s after %d turn(s).",
                    run_id, iteration,
                )
                break

            if response.stop_reason == "tool_use":
                # Execute all requested tool calls
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = self._handle_tool_use(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result, default=str),
                            }
                        )

                # Feed results back as a user turn
                messages.append({"role": "user", "content": tool_results})

            else:
                logger.warning(
                    "Unexpected stop_reason '%s' on turn %d for run %s.",
                    response.stop_reason, iteration, run_id,
                )
                break

        if iteration >= self.settings.max_tool_iterations:
            logger.warning(
                "Max tool iterations (%d) reached for run %s.",
                self.settings.max_tool_iterations, run_id,
            )

        if final_response:
            logger.info(
                "Agent conclusion for run %s:\n%s", run_id, final_response[:500]
            )

    # ------------------------------------------------------------------
    # Claude API call
    # ------------------------------------------------------------------

    def _call_claude(self, messages: List[Dict]):
        """Invoke the Anthropic Messages API.

        Args:
            messages: The full message history in Anthropic format.

        Returns:
            The Anthropic API response object.
        """
        # TODO: This is where the actual Claude API call happens.
        # The implementation is intentionally left as a clear TODO so
        # developers can see exactly where to plug in the API key and model.
        return self.anthropic_client.messages.create(
            model=self.settings.claude_model,
            max_tokens=4096,
            system=self._build_system_prompt(),
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _handle_tool_use(self, tool_name: str, tool_input: Dict) -> Any:
        """Execute a single tool call and return its result.

        Args:
            tool_name:  Name of the tool as requested by Claude.
            tool_input: Input dict from Claude's tool_use block.

        Returns:
            Serialisable result to feed back to Claude.
        """
        return dispatch_tool_call(
            tool_name=tool_name,
            tool_input=tool_input,
            connectors=self._connectors,
        )

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build the Claude system prompt.

        Returns:
            A multi-paragraph system prompt string.
        """
        return (
            "You are KubeAgent, an autonomous SRE assistant specialised in diagnosing "
            "and remediating Kubeflow Pipelines (KFP) failures in a Kubernetes cluster.\n\n"
            "Your responsibilities:\n"
            "1. Investigate failed KFP pipeline runs using the available tools.\n"
            "2. Identify the root cause (OOM kill, wrong image, missing environment variable, "
            "crash loop, resource quota exceeded, timeout, dependency failure, or unknown).\n"
            "3. If a fix can be automated, generate a patched YAML manifest and open a GitHub PR.\n"
            "4. Always write an incident report and update agent memory.\n\n"
            "Principles:\n"
            "- Be methodical: fetch logs and events before drawing conclusions.\n"
            "- Be conservative: only create PRs for high-confidence fixes (>0.6 confidence).\n"
            "- Never delete or truncate existing configuration — only extend or adjust it.\n"
            "- Cite specific log lines or events in your root cause analysis.\n"
            "- Always call update_memory as the last step for each run.\n\n"
            f"Current UTC time: {datetime.now(tz=timezone.utc).isoformat()}\n"
            f"KFP namespace: {self.settings.kfp_namespace}\n"
            f"GitHub repo: {self.settings.github_repo}"
        )

    def _build_user_message(self, run: Dict) -> str:
        """Build the initial user message describing the failed run.

        Args:
            run: Normalised run dict from :class:`KFPConnector`.

        Returns:
            A human-readable message string that kicks off the analysis.
        """
        run_id = run.get("run_id", "unknown")
        pipeline_name = run.get("pipeline_name", "unknown")
        state = run.get("state", "UNKNOWN")
        error_message = run.get("error_message", "")
        created_at = run.get("created_at", "")
        finished_at = run.get("finished_at", "")

        return (
            f"A KFP pipeline run has failed and requires investigation.\n\n"
            f"**Run ID:** {run_id}\n"
            f"**Pipeline:** {pipeline_name}\n"
            f"**State:** {state}\n"
            f"**Created:** {created_at}\n"
            f"**Finished:** {finished_at}\n"
            f"**Error:** {error_message or '(none reported)'}\n\n"
            f"Please investigate this failure using the available tools:\n"
            f"1. Fetch pod logs with get_run_logs.\n"
            f"2. Check Kubernetes events with get_k8s_pod_events.\n"
            f"3. Correlate with MLflow if relevant.\n"
            f"4. Classify the failure and generate a fix if appropriate.\n"
            f"5. Create a GitHub PR if confidence is high enough.\n"
            f"6. Write an incident report.\n"
            f"7. Update memory to mark this run as processed."
        )

    # ------------------------------------------------------------------
    # Connector initialisation
    # ------------------------------------------------------------------

    def _init_connectors(self) -> None:
        """Initialise each connector, logging warnings for any that fail.

        The agent can operate in degraded mode if some connectors are
        unavailable (e.g. no Kubernetes access when running locally).
        """
        # KFP (required)
        try:
            self._kfp = KFPConnector(
                endpoint=self.settings.kfp_endpoint,
                namespace=self.settings.kfp_namespace,
                existing_token=self.settings.kfp_token,
            )
        except Exception as exc:
            logger.error("KFP connector failed to initialise: %s", exc)
            # KFP failure is fatal; the agent cannot do anything without it
            sys.exit(1)

        # MLflow (optional)
        try:
            self._mlflow = MLflowConnector(
                tracking_uri=self.settings.mlflow_tracking_uri
            )
        except Exception as exc:
            logger.warning(
                "MLflow connector unavailable (MLflow features disabled): %s", exc
            )

        # Kubernetes (optional – needed for pod logs and events)
        try:
            self._k8s = K8sConnector(
                in_cluster=self.settings.k8s_in_cluster
            )
        except Exception as exc:
            logger.warning(
                "Kubernetes connector unavailable (K8s features disabled): %s", exc
            )

        # GitHub (optional – needed for PR creation)
        try:
            if self.settings.github_token:
                self._github = GitHubConnector(
                    token=self.settings.github_token,
                    default_repo=self.settings.github_repo,
                )
        except Exception as exc:
            logger.warning(
                "GitHub connector unavailable (PR creation disabled): %s", exc
            )

    # ------------------------------------------------------------------
    # Signal handling and sleep
    # ------------------------------------------------------------------

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        """Gracefully handle SIGINT / SIGTERM.

        Args:
            signum: Signal number.
            frame:  Current stack frame (unused).
        """
        logger.info("Received signal %d – shutting down after current cycle.", signum)
        self._running = False

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` but wake immediately if shutdown is requested.

        Args:
            seconds: Total sleep duration in seconds.
        """
        tick = 1.0
        elapsed = 0.0
        while elapsed < seconds and self._running:
            time.sleep(min(tick, seconds - elapsed))
            elapsed += tick


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Module entry point – create and run the agent."""
    agent = KubeAgent()
    agent.run()


if __name__ == "__main__":
    main()
