"""
Kubeflow Pipelines connector.

Wraps the KFP v2 Python SDK to provide a simplified interface for the agent.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import kfp
import kfp_server_api

logger = logging.getLogger(__name__)


class KFPConnector:
    """Thin wrapper around the KFP v2 client tailored for KubeAgent."""

    def __init__(
        self,
        endpoint: str,
        namespace: str = "kubeflow",
        existing_token: Optional[str] = None,
    ) -> None:
        """Initialise and connect to the KFP API server.

        Args:
            endpoint:       Full URL of the KFP API server,
                            e.g. ``http://ml-pipeline.kubeflow:8888``.
            namespace:      Kubernetes namespace where KFP runs.
            existing_token: Optional bearer token for authenticated clusters.
        """
        self.endpoint = endpoint
        self.namespace = namespace
        self._token = existing_token

        try:
            if existing_token:
                self._client = kfp.Client(
                    host=endpoint,
                    existing_token=existing_token,
                )
            else:
                self._client = kfp.Client(host=endpoint)
            logger.info("KFP client connected to %s", endpoint)
        except Exception as exc:
            logger.error("Failed to connect to KFP at %s: %s", endpoint, exc)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_failed_runs(
        self,
        last_n_hours: int = 1,
        namespace: Optional[str] = None,
    ) -> List[Dict]:
        """Return KFP runs that failed within the last ``last_n_hours`` hours.

        Args:
            last_n_hours: How far back to look.
            namespace:    Override the connector's default namespace.

        Returns:
            List of dicts with keys:
            ``run_id``, ``pipeline_name``, ``error_message``, ``state``,
            ``created_at``, ``finished_at``, ``node_states``.
        """
        ns = namespace or self.namespace
        since = datetime.now(tz=timezone.utc) - timedelta(hours=last_n_hours)

        try:
            # Build a filter for FAILED state
            predicate = kfp_server_api.V2beta1Predicate(
                operation=kfp_server_api.V2beta1PredicateOperation.EQUALS,
                key="state",
                string_value="FAILED",
            )
            api_filter = kfp_server_api.V2beta1Filter(predicates=[predicate])
            filter_str = json.dumps(
                {
                    "predicates": [
                        {
                            "operation": "EQUALS",
                            "key": "state",
                            "stringValue": "FAILED",
                        }
                    ]
                }
            )

            response = self._client._run_api.list_runs(
                namespace=ns,
                filter=filter_str,
                page_size=100,
            )
        except Exception as exc:
            logger.error("Error listing KFP runs: %s", exc)
            return []

        runs_list = response.runs or []
        result = []

        for run in runs_list:
            try:
                created = _parse_dt(run.created_at)
                if created is not None and created < since:
                    continue  # outside the lookback window

                result.append(self._run_to_dict(run))
            except Exception as exc:
                logger.warning("Skipping run %s due to parse error: %s", run.run_id, exc)

        logger.info("Found %d failed run(s) in the last %d hour(s).", len(result), last_n_hours)
        return result

    def get_run_details(self, run_id: str) -> Dict:
        """Fetch full details for a single run.

        Args:
            run_id: The KFP run identifier.

        Returns:
            Dict with full run detail including ``node_states``.
        """
        try:
            run = self._client._run_api.get_run(run_id=run_id)
            return self._run_to_dict(run)
        except Exception as exc:
            logger.error("Failed to fetch run %s: %s", run_id, exc)
            return {}

    def get_run_logs(self, run_id: str, max_lines: int = 200) -> str:
        """Retrieve log output for all pods associated with a run.

        Falls back to the run's error message if Kubernetes logs are not
        available through this client (which is typical unless the KFP
        server-side logging integration is enabled).

        Args:
            run_id:    The KFP run identifier.
            max_lines: Maximum log lines to return per pod.

        Returns:
            Concatenated log string.
        """
        try:
            run_detail = self._client._run_api.get_run(run_id=run_id)
            # KFP v2 stores logs inside pipeline_spec or runtime_config;
            # the actual pod logs are fetched via the Kubernetes connector.
            # Return whatever structured error info is available here.
            error_msg = getattr(run_detail, "error", None) or ""
            if error_msg:
                return f"[KFP run error]\n{error_msg}"
            return "[No log available through KFP API – use K8sConnector.get_pod_logs]"
        except Exception as exc:
            logger.warning("Could not retrieve logs for run %s: %s", run_id, exc)
            return ""

    def retry_run(self, run_id: str) -> bool:
        """Retry a failed run.

        Args:
            run_id: The KFP run identifier.

        Returns:
            True if the retry request was accepted, False otherwise.
        """
        try:
            self._client._run_api.retry_run(run_id=run_id)
            logger.info("Retry requested for run %s.", run_id)
            return True
        except Exception as exc:
            logger.error("Could not retry run %s: %s", run_id, exc)
            return False

    def list_experiments(self) -> List[Dict]:
        """List all KFP experiments.

        Returns:
            List of dicts with ``experiment_id``, ``name``, ``description``.
        """
        try:
            response = self._client._experiment_api.list_experiments(page_size=200)
            experiments = response.experiments or []
            return [
                {
                    "experiment_id": exp.experiment_id,
                    "name": exp.display_name,
                    "description": getattr(exp, "description", ""),
                }
                for exp in experiments
            ]
        except Exception as exc:
            logger.error("Failed to list experiments: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_to_dict(run) -> Dict:
        """Convert a KFP run object to a plain dict.

        Args:
            run: A ``V2beta1Run`` object returned by the KFP API.

        Returns:
            Normalised dict representation.
        """
        node_states: Dict = {}
        try:
            if hasattr(run, "runtime_details") and run.runtime_details:
                node_states = run.runtime_details.task_details or {}
        except Exception:
            pass

        error_msg = ""
        if hasattr(run, "error") and run.error:
            err = run.error
            if hasattr(err, "message"):
                error_msg = err.message
            else:
                error_msg = str(err)

        return {
            "run_id": run.run_id,
            "pipeline_name": getattr(run, "display_name", "unknown"),
            "error_message": error_msg,
            "state": str(getattr(run, "state", "UNKNOWN")),
            "created_at": str(getattr(run, "created_at", "")),
            "finished_at": str(getattr(run, "finished_at", "")),
            "node_states": node_states,
        }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _parse_dt(value) -> Optional[datetime]:
    """Parse a datetime value that may already be a datetime object or a string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
