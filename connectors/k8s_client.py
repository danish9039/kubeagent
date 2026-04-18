"""
Kubernetes connector for KubeAgent.

Wraps the official Kubernetes Python client to surface pod logs,
events, and resource usage for failed pipeline pods.
"""
import logging
from typing import Dict, List, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


class K8sConnector:
    """Thin Kubernetes API wrapper for KubeAgent diagnostic operations."""

    def __init__(
        self,
        in_cluster: bool = False,
        kubeconfig_path: Optional[str] = None,
    ) -> None:
        """Initialise and configure the Kubernetes client.

        In-cluster config is used when the agent runs as a Pod inside the
        cluster.  Outside the cluster, either the default ``~/.kube/config``
        or a custom path is loaded.

        Args:
            in_cluster:      True when running inside a Kubernetes pod.
            kubeconfig_path: Absolute path to a kubeconfig file.  If None,
                             the default kubeconfig location is used.
        """
        self.in_cluster = in_cluster

        try:
            if in_cluster:
                config.load_incluster_config()
                logger.info("Loaded in-cluster Kubernetes config.")
            elif kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path)
                logger.info("Loaded kubeconfig from %s.", kubeconfig_path)
            else:
                config.load_kube_config()
                logger.info("Loaded default kubeconfig.")
        except Exception as exc:
            logger.error("Failed to load Kubernetes config: %s", exc)
            raise

        self._core_v1 = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()
        self._metrics = client.CustomObjectsApi()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_pod_events(
        self,
        namespace: str,
        pod_name_prefix: Optional[str] = None,
    ) -> List[Dict]:
        """Retrieve Kubernetes events for pods in a namespace.

        Optionally filters events to those whose ``involvedObject.name``
        starts with ``pod_name_prefix``.

        Args:
            namespace:       Kubernetes namespace to query.
            pod_name_prefix: Optional pod name prefix filter.

        Returns:
            List of event dicts with keys:
            ``type``, ``reason``, ``message``, ``timestamp``, ``count``,
            ``involved_object``.
        """
        try:
            if pod_name_prefix:
                field_selector = f"involvedObject.name={pod_name_prefix}"
                event_list = self._core_v1.list_namespaced_event(
                    namespace=namespace,
                    field_selector=field_selector,
                )
            else:
                event_list = self._core_v1.list_namespaced_event(namespace=namespace)
        except ApiException as exc:
            logger.error(
                "Failed to list events in namespace %s: %s", namespace, exc
            )
            return []

        result = []
        for ev in event_list.items:
            # Skip Normal events to reduce noise unless there are few events
            result.append(
                {
                    "type": ev.type or "Unknown",
                    "reason": ev.reason or "",
                    "message": ev.message or "",
                    "timestamp": str(ev.last_timestamp or ev.first_timestamp or ""),
                    "count": ev.count or 1,
                    "involved_object": ev.involved_object.name if ev.involved_object else "",
                }
            )

        # Sort: Warning events first, then by timestamp descending
        result.sort(
            key=lambda e: (0 if e["type"] == "Warning" else 1, e["timestamp"]),
            reverse=False,
        )
        return result

    def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container: Optional[str] = None,
        tail_lines: int = 100,
    ) -> str:
        """Retrieve recent logs from a pod container.

        Args:
            namespace:   Kubernetes namespace.
            pod_name:    Exact name of the pod.
            container:   Container name (required for multi-container pods).
            tail_lines:  Number of log lines to return from the end.

        Returns:
            Log string, or an error message if unavailable.
        """
        kwargs: Dict = {
            "name": pod_name,
            "namespace": namespace,
            "tail_lines": tail_lines,
            "timestamps": True,
        }
        if container:
            kwargs["container"] = container

        try:
            logs = self._core_v1.read_namespaced_pod_log(**kwargs)
            return logs or ""
        except ApiException as exc:
            if exc.status == 400:
                # Pod may be in a bad state; try fetching previous container logs
                try:
                    kwargs["previous"] = True
                    logs = self._core_v1.read_namespaced_pod_log(**kwargs)
                    return f"[Previous container logs]\n{logs}"
                except ApiException:
                    pass
            logger.warning(
                "Could not retrieve logs for pod %s/%s: %s", namespace, pod_name, exc
            )
            return f"[Log unavailable: {exc.reason}]"

    def get_failed_pods(self, namespace: str) -> List[Dict]:
        """List pods in a Failed or Error state in the given namespace.

        Args:
            namespace: Kubernetes namespace to inspect.

        Returns:
            List of dicts with keys:
            ``name``, ``namespace``, ``phase``, ``reason``, ``message``,
            ``container_statuses``.
        """
        try:
            pod_list = self._core_v1.list_namespaced_pod(
                namespace=namespace,
                field_selector="status.phase=Failed",
            )
        except ApiException as exc:
            logger.error("Failed to list pods in %s: %s", namespace, exc)
            return []

        result = []
        for pod in pod_list.items:
            container_statuses = []
            if pod.status and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    state_info = {}
                    if cs.state:
                        if cs.state.terminated:
                            t = cs.state.terminated
                            state_info = {
                                "state": "terminated",
                                "exit_code": t.exit_code,
                                "reason": t.reason or "",
                                "message": t.message or "",
                            }
                        elif cs.state.waiting:
                            w = cs.state.waiting
                            state_info = {
                                "state": "waiting",
                                "reason": w.reason or "",
                                "message": w.message or "",
                            }
                    container_statuses.append(
                        {
                            "name": cs.name,
                            "image": cs.image,
                            "restart_count": cs.restart_count,
                            **state_info,
                        }
                    )

            result.append(
                {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "phase": pod.status.phase if pod.status else "Unknown",
                    "reason": (pod.status.reason if pod.status else "") or "",
                    "message": (pod.status.message if pod.status else "") or "",
                    "container_statuses": container_statuses,
                }
            )

        return result

    def get_pod_resource_usage(
        self, namespace: str, pod_name: str
    ) -> Dict:
        """Fetch current CPU and memory usage for a pod via the Metrics API.

        Requires the ``metrics-server`` addon to be installed in the cluster.

        Args:
            namespace: Kubernetes namespace.
            pod_name:  Pod name.

        Returns:
            Dict with keys ``containers``, each being a list of dicts with
            ``name``, ``cpu``, ``memory``.  Returns empty dict on failure.
        """
        try:
            metrics = self._custom.get_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=namespace,
                plural="pods",
                name=pod_name,
            )
            containers = []
            for c in metrics.get("containers", []):
                usage = c.get("usage", {})
                containers.append(
                    {
                        "name": c.get("name", ""),
                        "cpu": usage.get("cpu", "0"),
                        "memory": usage.get("memory", "0"),
                    }
                )
            return {"containers": containers}
        except ApiException as exc:
            if exc.status == 404:
                logger.debug("Metrics API not available or pod %s not found.", pod_name)
            else:
                logger.warning("Could not fetch metrics for pod %s: %s", pod_name, exc)
            return {}

    def list_pods_for_run(self, namespace: str, run_id: str) -> List[str]:
        """Find pods associated with a KFP run by label or annotation.

        Tries the following label selectors in order:
        1. ``pipeline/runid=<run_id>``
        2. ``workflows.argoproj.io/completed=true`` + annotation match
        3. ``run-id=<run_id>``

        Args:
            namespace: Kubernetes namespace.
            run_id:    KFP run identifier.

        Returns:
            List of pod names that belong to the run.
        """
        pod_names: List[str] = []

        label_selectors = [
            f"pipeline/runid={run_id}",
            f"run-id={run_id}",
            f"kubeflow.org/run-id={run_id}",
        ]

        for selector in label_selectors:
            try:
                pod_list = self._core_v1.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=selector,
                )
                for pod in pod_list.items:
                    name = pod.metadata.name
                    if name not in pod_names:
                        pod_names.append(name)
            except ApiException as exc:
                logger.debug(
                    "Label selector %r failed in %s: %s", selector, namespace, exc
                )

        if not pod_names:
            # Final fallback: scan annotations for workflow-id
            try:
                pod_list = self._core_v1.list_namespaced_pod(namespace=namespace)
                for pod in pod_list.items:
                    annotations = pod.metadata.annotations or {}
                    if run_id in annotations.get("workflows.argoproj.io/name", ""):
                        pod_names.append(pod.metadata.name)
            except ApiException as exc:
                logger.debug("Annotation-based pod search failed: %s", exc)

        logger.info(
            "Found %d pod(s) for run %s in namespace %s.",
            len(pod_names), run_id, namespace,
        )
        return pod_names
