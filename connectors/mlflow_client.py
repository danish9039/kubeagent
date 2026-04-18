"""
MLflow connector for KubeAgent.

Provides a simplified interface over the MLflow Tracking client to retrieve
experiment runs and detect metric regressions.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from mlflow.tracking import MlflowClient
from mlflow.entities import ViewType

logger = logging.getLogger(__name__)


class MLflowConnector:
    """Wrapper around the MLflow Tracking client tailored for KubeAgent."""

    def __init__(self, tracking_uri: str) -> None:
        """Initialise and connect to the MLflow tracking server.

        Args:
            tracking_uri: URI of the MLflow tracking server,
                          e.g. ``http://mlflow.kubeflow:5000``.
        """
        self.tracking_uri = tracking_uri
        self._client = MlflowClient(tracking_uri=tracking_uri)
        logger.info("MLflow client connected to %s", tracking_uri)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_recent_runs(
        self,
        experiment_name_filter: Optional[str] = None,
        last_n_runs: int = 20,
    ) -> List[Dict]:
        """Return recent MLflow runs, optionally filtered by experiment name.

        Args:
            experiment_name_filter: If provided, only include experiments whose
                                    name contains this substring (case-insensitive).
            last_n_runs:            Maximum number of runs to return per experiment.

        Returns:
            List of dicts with keys:
            ``run_id``, ``experiment_name``, ``status``, ``metrics``,
            ``params``, ``start_time``, ``end_time``.
        """
        try:
            all_experiments = self._client.search_experiments(view_type=ViewType.ACTIVE_ONLY)
        except Exception as exc:
            logger.error("Failed to list MLflow experiments: %s", exc)
            return []

        # Filter experiments by name substring if requested
        if experiment_name_filter:
            needle = experiment_name_filter.lower()
            all_experiments = [
                e for e in all_experiments if needle in e.name.lower()
            ]

        if not all_experiments:
            logger.warning("No MLflow experiments matched the filter.")
            return []

        result: List[Dict] = []
        for experiment in all_experiments:
            try:
                runs = self._client.search_runs(
                    experiment_ids=[experiment.experiment_id],
                    filter_string="",
                    order_by=["start_time DESC"],
                    max_results=last_n_runs,
                )
                for run in runs:
                    result.append(self._run_to_dict(run, experiment.name))
            except Exception as exc:
                logger.warning(
                    "Could not fetch runs for experiment %s: %s",
                    experiment.name,
                    exc,
                )

        return result

    def get_run_metrics_history(
        self, run_id: str, metric_key: str
    ) -> List[Dict]:
        """Return the full metric history for a single metric key.

        Args:
            run_id:     MLflow run identifier.
            metric_key: Name of the metric to retrieve history for.

        Returns:
            List of dicts with keys ``step``, ``timestamp``, ``value``,
            sorted by step ascending.
        """
        try:
            history = self._client.get_metric_history(run_id=run_id, key=metric_key)
            return sorted(
                [
                    {
                        "step": m.step,
                        "timestamp": m.timestamp,
                        "value": m.value,
                    }
                    for m in history
                ],
                key=lambda x: x["step"],
            )
        except Exception as exc:
            logger.error(
                "Failed to fetch metric history for run %s / %s: %s",
                run_id, metric_key, exc,
            )
            return []

    def detect_metric_degradation(
        self,
        run_id: str,
        metric_key: str = "accuracy",
        threshold: float = 0.05,
    ) -> bool:
        """Detect whether a metric has degraded relative to its initial value.

        Compares the *last* recorded value against the *first* recorded value.
        A decrease greater than ``threshold`` (absolute) is considered
        degradation.

        Args:
            run_id:     MLflow run identifier.
            metric_key: Metric name to inspect (default ``"accuracy"``).
            threshold:  Absolute drop threshold (default 0.05 = 5 pp).

        Returns:
            True if the metric has degraded beyond the threshold, False
            otherwise (including when insufficient history is available).
        """
        history = self.get_run_metrics_history(run_id, metric_key)
        if len(history) < 2:
            return False

        first_value = history[0]["value"]
        last_value = history[-1]["value"]

        drop = first_value - last_value
        degraded = drop > threshold
        if degraded:
            logger.info(
                "Metric degradation detected for run %s: %s dropped by %.4f "
                "(first=%.4f, last=%.4f).",
                run_id, metric_key, drop, first_value, last_value,
            )
        return degraded

    def correlate_with_kfp_run(self, kfp_run_name: str) -> Optional[Dict]:
        """Find an MLflow run that corresponds to a given KFP run name.

        Searches all experiments for an MLflow run whose:
        - ``mlflow.runName`` tag matches ``kfp_run_name``, OR
        - ``kfp_run_id`` or ``kfp_run_name`` tag contains ``kfp_run_name``.

        Falls back to a simple display-name substring match.

        Args:
            kfp_run_name: The ``display_name`` or ``run_id`` of the KFP run.

        Returns:
            A normalised run dict (same schema as :meth:`get_recent_runs`)
            or ``None`` if no match is found.
        """
        try:
            all_experiments = self._client.search_experiments(view_type=ViewType.ACTIVE_ONLY)
        except Exception as exc:
            logger.error("MLflow experiment listing failed: %s", exc)
            return None

        # Try tag-based search first for speed
        tag_filter = (
            f"tags.`mlflow.runName` = '{kfp_run_name}' OR "
            f"tags.`kfp_run_id` = '{kfp_run_name}' OR "
            f"tags.`kfp_run_name` = '{kfp_run_name}'"
        )
        for experiment in all_experiments:
            try:
                runs = self._client.search_runs(
                    experiment_ids=[experiment.experiment_id],
                    filter_string=tag_filter,
                    max_results=5,
                )
                if runs:
                    return self._run_to_dict(runs[0], experiment.name)
            except Exception:
                pass  # malformed filter for this experiment – continue

        # Fallback: substring match on run name tag
        for experiment in all_experiments:
            try:
                all_runs = self._client.search_runs(
                    experiment_ids=[experiment.experiment_id],
                    filter_string="",
                    order_by=["start_time DESC"],
                    max_results=50,
                )
                for run in all_runs:
                    run_name = run.data.tags.get("mlflow.runName", "")
                    if kfp_run_name.lower() in run_name.lower():
                        return self._run_to_dict(run, experiment.name)
            except Exception as exc:
                logger.debug(
                    "Skipping experiment %s during correlation search: %s",
                    experiment.name, exc,
                )

        logger.info("No MLflow run correlated to KFP run '%s'.", kfp_run_name)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_to_dict(run, experiment_name: str) -> Dict:
        """Convert an MLflow Run object to a plain dict.

        Args:
            run:             ``mlflow.entities.Run`` object.
            experiment_name: Human-readable experiment name.

        Returns:
            Normalised dict with keys matching the public API contract.
        """
        info = run.info
        data = run.data

        start_ts = info.start_time
        end_ts = info.end_time

        return {
            "run_id": info.run_id,
            "experiment_name": experiment_name,
            "status": info.status,
            "metrics": dict(data.metrics),
            "params": dict(data.params),
            "tags": dict(data.tags),
            "start_time": (
                datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc).isoformat()
                if start_ts else None
            ),
            "end_time": (
                datetime.fromtimestamp(end_ts / 1000, tz=timezone.utc).isoformat()
                if end_ts else None
            ),
        }
