"""
Rule-based failure classifier for KFP pipeline runs.

Each failure type has a set of regex patterns matched against logs, events,
and the KFP error message.  The classifier picks the type with the most
pattern hits, breaking ties by the order of FAILURE_PATTERNS.
"""
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class FailureType(Enum):
    OOM_KILL = "OOM_KILL"
    WRONG_IMAGE = "WRONG_IMAGE"
    MISSING_ENV = "MISSING_ENV"
    CRASH_LOOP = "CRASH_LOOP"
    RESOURCE_QUOTA = "RESOURCE_QUOTA"
    TIMEOUT = "TIMEOUT"
    DEPENDENCY_FAIL = "DEPENDENCY_FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass
class ClassificationResult:
    """Result of classifying a pipeline failure."""

    failure_type: FailureType
    confidence: float                  # 0.0 – 1.0
    matched_patterns: List[str]        # human-readable list of what matched
    fix_strategy: str                  # e.g. "increase_memory_limit"
    patch_target: str                  # YAML field to change, e.g. "resources.limits.memory"
    suggested_fix: Dict = field(default_factory=dict)  # type-specific hints


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

FAILURE_PATTERNS: Dict[FailureType, Dict] = {
    FailureType.OOM_KILL: {
        "patterns": [
            r"OOMKilled",
            r"memory\s+limit",
            r"exit\s+code\s+137",
            r"\bKilled\b",
            r"out\s+of\s+memory",
            r"Cannot\s+allocate\s+memory",
            r"MemoryError",
            r"memory\s+exceeded",
            r"SIGKILL",
            r"container\s+was\s+killed",
        ],
        "fix_strategy": "increase_memory_limit",
        "patch_target": "resources.limits.memory",
        "suggested_fix": {"memory_multiplier": 4.0, "min_memory": "512Mi"},
    },
    FailureType.WRONG_IMAGE: {
        "patterns": [
            r"ErrImagePull",
            r"ImagePullBackOff",
            r"image\s+not\s+found",
            r"repository\s+does\s+not\s+exist",
            r"manifest\s+unknown",
            r"unauthorized.*registry",
            r"pull\s+access\s+denied",
            r"no\s+such\s+image",
            r"invalid\s+image\s+name",
            r"toomanyrequests.*pull\s+rate",
        ],
        "fix_strategy": "fix_image_reference",
        "patch_target": "image",
        "suggested_fix": {"fallback_tag": "latest"},
    },
    FailureType.MISSING_ENV: {
        "patterns": [
            r"KeyError.*['\"]([A-Z_]{3,})['\"]",
            r"environment\s+variable.*not\s+set",
            r"env.*variable.*missing",
            r"getenv.*None",
            r"\$\{[A-Z_]+\}",
            r"required.*environment.*variable",
            r"No\s+such\s+file.*\.env",
            r"cannot\s+find.*secret",
            r"secret.*not\s+found",
            r"ConfigMap.*not\s+found",
        ],
        "fix_strategy": "add_missing_env_var",
        "patch_target": "env",
        "suggested_fix": {"placeholder_value": "REPLACE_ME"},
    },
    FailureType.CRASH_LOOP: {
        "patterns": [
            r"CrashLoopBackOff",
            r"Back-off\s+restarting",
            r"restart\s+count.*[5-9]\d*",
            r"container\s+keeps\s+crashing",
            r"exit\s+code\s+[1-9]\d*",
            r"segmentation\s+fault",
            r"SIGSEGV",
            r"core\s+dumped",
            r"panic:",
            r"fatal\s+error:",
        ],
        "fix_strategy": "add_resource_limits_and_probe",
        "patch_target": "resources,livenessProbe",
        "suggested_fix": {
            "initial_delay_seconds": 30,
            "failure_threshold": 5,
            "default_memory_limit": "512Mi",
            "default_cpu_limit": "500m",
        },
    },
    FailureType.RESOURCE_QUOTA: {
        "patterns": [
            r"exceeded\s+quota",
            r"resource\s+quota",
            r"ResourceQuota",
            r"LimitRange",
            r"forbidden.*quota",
            r"pods.*forbidden",
            r"insufficient\s+(cpu|memory|pods)",
            r"Unschedulable",
            r"0/\d+\s+nodes\s+are\s+available",
            r"FailedScheduling",
        ],
        "fix_strategy": "reduce_resource_requests",
        "patch_target": "resources.requests",
        "suggested_fix": {"request_reduction_factor": 0.5},
    },
    FailureType.TIMEOUT: {
        "patterns": [
            r"deadline\s+exceeded",
            r"context\s+deadline",
            r"timeout\s+after",
            r"timed?\s*out",
            r"execution\s+time.*exceeded",
            r"max_run_duration",
            r"step\s+timeout",
            r"connection\s+timed?\s+out",
            r"read\s+timeout",
            r"request\s+timeout",
        ],
        "fix_strategy": "increase_timeout",
        "patch_target": "timeout",
        "suggested_fix": {"timeout_multiplier": 2.0, "min_timeout_seconds": 3600},
    },
    FailureType.DEPENDENCY_FAIL: {
        "patterns": [
            r"upstream\s+task\s+failed",
            r"dependency\s+failed",
            r"parent\s+task.*failed",
            r"predecessor.*not.*succeed",
            r"module\s+not\s+found",
            r"ModuleNotFoundError",
            r"ImportError",
            r"cannot\s+import",
            r"connection\s+refused",
            r"service.*unavailable",
        ],
        "fix_strategy": "fix_dependency",
        "patch_target": "dependencies",
        "suggested_fix": {},
    },
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class FailureClassifier:
    """Classifies a pipeline failure into a FailureType using regex pattern matching."""

    def classify(
        self,
        logs: str,
        events: List[Dict],
        error_message: str = "",
    ) -> ClassificationResult:
        """Classify a pipeline failure from available text signals.

        The method concatenates logs, serialised events, and the KFP error
        message into one corpus and runs each failure type's regex patterns
        against it.  The type with the most matches wins; ties are broken by
        the order in FAILURE_PATTERNS.

        Args:
            logs:          Raw log text from Kubernetes pods or KFP.
            events:        List of Kubernetes event dicts, each with at least
                           a ``message`` key.
            error_message: The ``error`` field from the KFP run object.

        Returns:
            A :class:`ClassificationResult` describing the detected failure.
        """
        # Build one searchable corpus
        event_text = " ".join(
            e.get("message", "") + " " + e.get("reason", "")
            for e in events
        )
        corpus = "\n".join([logs or "", event_text, error_message or ""])

        best_type: Optional[FailureType] = None
        best_matches: List[str] = []
        best_score: int = 0

        for failure_type, spec in FAILURE_PATTERNS.items():
            matched = self._match_patterns(corpus, spec["patterns"])
            score = len(matched)
            if score > best_score:
                best_score = score
                best_type = failure_type
                best_matches = matched

        if best_type is None or best_score == 0:
            return ClassificationResult(
                failure_type=FailureType.UNKNOWN,
                confidence=0.0,
                matched_patterns=[],
                fix_strategy="manual_investigation",
                patch_target="",
                suggested_fix={},
            )

        spec = FAILURE_PATTERNS[best_type]
        confidence = self._calculate_confidence(best_matches, len(spec["patterns"]))

        return ClassificationResult(
            failure_type=best_type,
            confidence=confidence,
            matched_patterns=best_matches,
            fix_strategy=spec["fix_strategy"],
            patch_target=spec["patch_target"],
            suggested_fix=spec.get("suggested_fix", {}),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _match_patterns(text: str, patterns: List[str]) -> List[str]:
        """Return the subset of patterns that match anywhere in ``text``.

        Args:
            text:     The corpus to search.
            patterns: List of regex patterns.

        Returns:
            List of patterns that produced at least one match.
        """
        matched = []
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                matched.append(pattern)
        return matched

    @staticmethod
    def _calculate_confidence(matched: List[str], total_patterns: int) -> float:
        """Calculate a confidence score based on how many patterns matched.

        Uses a log-scaled formula so that matching even one pattern already
        gives reasonable confidence (0.4), while matching all patterns yields
        1.0.

        Args:
            matched:        The list of matched patterns.
            total_patterns: Total number of patterns for this failure type.

        Returns:
            A float in [0.0, 1.0].
        """
        if total_patterns == 0:
            return 0.0
        ratio = len(matched) / total_patterns
        # Logarithmic scale: 1 match → ~0.4, 50% → ~0.7, 100% → 1.0
        import math
        if ratio <= 0:
            return 0.0
        # Scale so that ratio=1 → 1.0 and ratio→0 → 0
        confidence = math.log1p(ratio * (math.e - 1))
        return round(min(confidence, 1.0), 3)
