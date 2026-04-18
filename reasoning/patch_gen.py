"""
YAML patch generator for KubeAgent.

Produces a modified Kubernetes manifest YAML string given a failure type and
optional suggested-fix hints from the classifier.
"""
import copy
import re
import yaml
from typing import Any, Dict, Optional

from kubeagent.reasoning.classifier import FailureType

# ---------------------------------------------------------------------------
# Memory size helpers
# ---------------------------------------------------------------------------

_UNIT_MULTIPLIERS = {
    "ki": 2**10,
    "mi": 2**20,
    "gi": 2**30,
    "ti": 2**40,
    "k":  1_000,
    "m":  1_000_000,
    "g":  1_000_000_000,
    "t":  1_000_000_000_000,
    # No suffix → bytes
    "":   1,
}

_ORDERED_MEMORY_UNITS = ["Ti", "Gi", "Mi", "Ki", ""]


def _parse_memory_bytes(value: str) -> int:
    """Parse a Kubernetes memory string into bytes.

    Handles suffixes: Ki, Mi, Gi, Ti (binary) and K, M, G, T (decimal),
    as well as plain integer strings.

    Args:
        value: Memory string such as ``"512Mi"``, ``"1Gi"``, ``"2G"``, ``"131072"``.

    Returns:
        Integer byte count.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    value = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTkmgt][iI]?)?", value)
    if not match:
        raise ValueError(f"Cannot parse memory value: {value!r}")
    number_str, unit_str = match.group(1), (match.group(2) or "")
    number = float(number_str)
    multiplier = _UNIT_MULTIPLIERS.get(unit_str.lower(), None)
    if multiplier is None:
        raise ValueError(f"Unknown memory unit {unit_str!r} in {value!r}")
    return int(number * multiplier)


def _bytes_to_memory_string(byte_count: int) -> str:
    """Convert a byte count back to the most appropriate Kubernetes memory string.

    Args:
        byte_count: Integer byte count.

    Returns:
        A human-readable string such as ``"2Gi"`` or ``"512Mi"``.
    """
    for unit in ["Ti", "Gi", "Mi", "Ki"]:
        divisor = _UNIT_MULTIPLIERS[unit.lower()]
        if byte_count >= divisor and byte_count % divisor == 0:
            return f"{byte_count // divisor}{unit}"
    # Fallback: try nearest Mi
    divisor = _UNIT_MULTIPLIERS["mi"]
    mebibytes = round(byte_count / divisor)
    if mebibytes >= 1:
        return f"{mebibytes}Mi"
    return str(byte_count)


class YAMLPatchGenerator:
    """Generates patched Kubernetes manifest YAML for common pipeline failures."""

    def generate_patch(
        self,
        failure_type: FailureType,
        original_yaml: str,
        suggested_fix: Optional[Dict] = None,
    ) -> str:
        """Apply the appropriate fix for ``failure_type`` to the manifest.

        Args:
            failure_type:   Detected failure category.
            original_yaml:  Raw YAML string of the Kubernetes manifest.
            suggested_fix:  Optional hints from the classifier (e.g. memory
                            multiplier, placeholder value).

        Returns:
            Patched YAML string.  If the manifest cannot be parsed, the
            original string is returned unchanged.
        """
        if suggested_fix is None:
            suggested_fix = {}

        try:
            manifest = yaml.safe_load(original_yaml)
        except yaml.YAMLError as exc:
            return original_yaml  # cannot parse – return as-is

        if not isinstance(manifest, dict):
            return original_yaml

        manifest = copy.deepcopy(manifest)

        dispatch = {
            FailureType.OOM_KILL: self._fix_oom,
            FailureType.WRONG_IMAGE: self._fix_wrong_image,
            FailureType.MISSING_ENV: self._fix_missing_env,
            FailureType.CRASH_LOOP: self._fix_crash_loop,
            FailureType.RESOURCE_QUOTA: self._fix_resource_quota,
            FailureType.TIMEOUT: self._fix_timeout,
            FailureType.DEPENDENCY_FAIL: self._fix_dependency_fail,
        }

        fix_fn = dispatch.get(failure_type)
        if fix_fn is not None:
            manifest = fix_fn(manifest, suggested_fix)
        # FailureType.UNKNOWN → no automated fix, return original

        return yaml.dump(manifest, default_flow_style=False, allow_unicode=True)

    # ------------------------------------------------------------------
    # Fix implementations
    # ------------------------------------------------------------------

    def _fix_oom(self, manifest: dict, suggested_fix: Dict) -> dict:
        """Increase the memory limit for all containers.

        Locates ``spec.containers`` (or ``spec.template.spec.containers`` for
        Deployments) and multiplies the ``resources.limits.memory`` of each
        container by the configured multiplier.

        Args:
            manifest:      Deep-copied manifest dict.
            suggested_fix: May contain ``memory_multiplier`` (default 4.0).

        Returns:
            Modified manifest dict.
        """
        multiplier = float(suggested_fix.get("memory_multiplier", 4.0))
        min_memory = suggested_fix.get("min_memory", "512Mi")

        containers = _get_containers(manifest)
        for container in containers:
            resources = container.setdefault("resources", {})
            limits = resources.setdefault("limits", {})
            current = limits.get("memory")
            limits["memory"] = self._increase_memory(
                current or "128Mi", multiplier=multiplier, minimum=min_memory
            )
            # Also increase requests to at least half the new limit
            requests = resources.setdefault("requests", {})
            current_req = requests.get("memory")
            new_limit_bytes = _parse_memory_bytes(limits["memory"])
            requests["memory"] = _bytes_to_memory_string(max(new_limit_bytes // 2, _parse_memory_bytes("64Mi")))

        return manifest

    def _fix_wrong_image(self, manifest: dict, suggested_fix: Dict) -> dict:
        """Replace the image tag with ``latest`` or a suggested tag.

        For private registry errors the registry prefix is left intact; only
        the tag is replaced.

        Args:
            manifest:      Deep-copied manifest dict.
            suggested_fix: May contain ``fallback_tag`` (default ``"latest"``).

        Returns:
            Modified manifest dict.
        """
        fallback_tag = suggested_fix.get("fallback_tag", "latest")
        containers = _get_containers(manifest)
        for container in containers:
            image = container.get("image", "")
            if ":" in image:
                repo = image.rsplit(":", 1)[0]
                container["image"] = f"{repo}:{fallback_tag}"
            elif image:
                container["image"] = f"{image}:{fallback_tag}"
        return manifest

    def _fix_missing_env(self, manifest: dict, suggested_fix: Dict) -> dict:
        """Add a placeholder environment variable entry.

        The placeholder uses the key ``MISSING_VAR`` with a sentinel value
        so developers know exactly where to fill in the real value.

        Args:
            manifest:      Deep-copied manifest dict.
            suggested_fix: May contain ``env_key`` and ``placeholder_value``.

        Returns:
            Modified manifest dict.
        """
        placeholder = suggested_fix.get("placeholder_value", "REPLACE_ME")
        env_key = suggested_fix.get("env_key", "MISSING_VAR")

        containers = _get_containers(manifest)
        for container in containers:
            env_list = container.setdefault("env", [])
            # Check if the key already exists
            existing_keys = {e.get("name") for e in env_list if isinstance(e, dict)}
            if env_key not in existing_keys:
                env_list.append({
                    "name": env_key,
                    "value": placeholder,
                    # TODO: replace with secretKeyRef or configMapKeyRef as appropriate
                })
        return manifest

    def _fix_crash_loop(self, manifest: dict, suggested_fix: Dict) -> dict:
        """Add resource limits and a liveness probe with a generous initial delay.

        Args:
            manifest:      Deep-copied manifest dict.
            suggested_fix: May contain ``initial_delay_seconds``,
                           ``failure_threshold``, ``default_memory_limit``,
                           ``default_cpu_limit``.

        Returns:
            Modified manifest dict.
        """
        initial_delay = int(suggested_fix.get("initial_delay_seconds", 30))
        failure_threshold = int(suggested_fix.get("failure_threshold", 5))
        default_mem = suggested_fix.get("default_memory_limit", "512Mi")
        default_cpu = suggested_fix.get("default_cpu_limit", "500m")

        containers = _get_containers(manifest)
        for container in containers:
            # Add resource limits if absent
            resources = container.setdefault("resources", {})
            limits = resources.setdefault("limits", {})
            limits.setdefault("memory", default_mem)
            limits.setdefault("cpu", default_cpu)
            requests = resources.setdefault("requests", {})
            requests.setdefault("memory", "256Mi")
            requests.setdefault("cpu", "100m")

            # Add / update liveness probe
            probe = container.setdefault("livenessProbe", {})
            probe.setdefault("exec", {"command": ["/bin/sh", "-c", "exit 0"]})
            probe["initialDelaySeconds"] = max(
                probe.get("initialDelaySeconds", 0), initial_delay
            )
            probe["failureThreshold"] = max(
                probe.get("failureThreshold", 3), failure_threshold
            )
            probe.setdefault("periodSeconds", 10)

        return manifest

    def _fix_resource_quota(self, manifest: dict, suggested_fix: Dict) -> dict:
        """Halve the resource requests so the pod fits within quota.

        Args:
            manifest:      Deep-copied manifest dict.
            suggested_fix: May contain ``request_reduction_factor`` (default 0.5).

        Returns:
            Modified manifest dict.
        """
        factor = float(suggested_fix.get("request_reduction_factor", 0.5))

        containers = _get_containers(manifest)
        for container in containers:
            resources = container.get("resources", {})
            requests = resources.get("requests", {})

            if "memory" in requests:
                current_bytes = _parse_memory_bytes(requests["memory"])
                new_bytes = max(int(current_bytes * factor), _parse_memory_bytes("64Mi"))
                requests["memory"] = _bytes_to_memory_string(new_bytes)

            if "cpu" in requests:
                cpu_str = requests["cpu"]
                millis = _parse_cpu_millis(cpu_str)
                new_millis = max(int(millis * factor), 10)
                requests["cpu"] = f"{new_millis}m"

        return manifest

    def _fix_timeout(self, manifest: dict, suggested_fix: Dict) -> dict:
        """Double the activeDeadlineSeconds on the pod spec.

        Args:
            manifest:      Deep-copied manifest dict.
            suggested_fix: May contain ``timeout_multiplier`` and
                           ``min_timeout_seconds``.

        Returns:
            Modified manifest dict.
        """
        multiplier = float(suggested_fix.get("timeout_multiplier", 2.0))
        min_timeout = int(suggested_fix.get("min_timeout_seconds", 3600))

        spec = _get_pod_spec(manifest)
        current = spec.get("activeDeadlineSeconds", min_timeout)
        spec["activeDeadlineSeconds"] = max(int(current * multiplier), min_timeout)
        return manifest

    def _fix_dependency_fail(self, manifest: dict, suggested_fix: Dict) -> dict:
        """Add an annotation noting the dependency failure for manual review.

        Automated fixes are not safe for dependency failures without more
        context, so the patch only adds an annotation as a breadcrumb.

        Args:
            manifest:      Deep-copied manifest dict.
            suggested_fix: Unused.

        Returns:
            Modified manifest dict.
        """
        metadata = manifest.setdefault("metadata", {})
        annotations = metadata.setdefault("annotations", {})
        annotations["kubeagent.io/fix-needed"] = (
            "dependency-failure-detected – review upstream task and service availability"
        )
        return manifest

    # ------------------------------------------------------------------
    # Memory arithmetic
    # ------------------------------------------------------------------

    def _increase_memory(
        self,
        current_value: str,
        multiplier: float = 4.0,
        minimum: str = "512Mi",
    ) -> str:
        """Parse a memory string, multiply it, and return the new string.

        Applies the following heuristic:
        - If current < 100 Mi → return at least 512 Mi
        - Otherwise multiply by ``multiplier`` (capped at 128 Gi)

        Args:
            current_value: Current memory string, e.g. ``"50Mi"``.
            multiplier:    Factor to scale by.
            minimum:       Floor value.

        Returns:
            New memory string.
        """
        try:
            current_bytes = _parse_memory_bytes(current_value)
        except ValueError:
            return minimum

        min_bytes = _parse_memory_bytes(minimum)
        max_bytes = _parse_memory_bytes("128Gi")

        new_bytes = int(current_bytes * multiplier)
        new_bytes = max(new_bytes, min_bytes)
        new_bytes = min(new_bytes, max_bytes)

        return _bytes_to_memory_string(new_bytes)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _get_containers(manifest: dict) -> list:
    """Locate the containers list in various manifest structures.

    Supports plain Pod specs, Deployment/StatefulSet/DaemonSet templates, and
    Argo Workflow templates (used by KFP).

    Args:
        manifest: Full manifest dict.

    Returns:
        Mutable list of container dicts (may be empty).
    """
    # Direct pod spec
    spec = manifest.get("spec", {})
    if "containers" in spec:
        return spec["containers"]

    # Deployment / StatefulSet / DaemonSet
    template = spec.get("template", {}).get("spec", {})
    if "containers" in template:
        return template["containers"]

    # Argo Workflow
    templates = spec.get("templates", [])
    for tmpl in templates:
        if "container" in tmpl:
            return [tmpl["container"]]

    return []


def _get_pod_spec(manifest: dict) -> dict:
    """Return the pod spec dict for timeout patching."""
    spec = manifest.get("spec", {})
    if "template" in spec:
        return spec["template"].get("spec", spec)
    return spec


def _parse_cpu_millis(cpu_str: str) -> int:
    """Parse a CPU string like ``"500m"`` or ``"2"`` into millicores.

    Args:
        cpu_str: Kubernetes CPU string.

    Returns:
        Integer millicores.
    """
    cpu_str = cpu_str.strip()
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1])
    try:
        return int(float(cpu_str) * 1000)
    except ValueError:
        return 100  # safe default
