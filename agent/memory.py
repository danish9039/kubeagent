"""
Persistent JSON memory manager for KubeAgent.

Uses atomic writes (write to .tmp then os.rename) to avoid corruption
if the process is killed mid-write.
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default in-memory schema
# ---------------------------------------------------------------------------
DEFAULT_MEMORY: Dict = {
    "version": "1.0",
    "created_at": None,         # ISO-8601 timestamp, set on first save
    "last_updated": None,       # ISO-8601 timestamp, updated each cycle
    "processed_runs": {},       # run_id → {status, details, processed_at}
    "stats": {
        "total_processed": 0,
        "failures_by_type": {},  # FailureType value → count
        "prs_created": 0,
        "reports_generated": 0,
    },
    "incidents": [],            # Recent incident summaries (capped at 100)
}

MAX_INCIDENTS = 100


class MemoryManager:
    """Manages persistent agent memory stored as a JSON file on disk."""

    def __init__(self, memory_path: str) -> None:
        """Initialise the manager.

        Args:
            memory_path: Absolute or relative path to the JSON memory file.
                         Parent directories are created automatically.
        """
        self.memory_path = os.path.abspath(memory_path)
        self._ensure_directory()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> Dict:
        """Load memory from disk, returning the default schema if the file
        does not yet exist or is corrupt.

        Returns:
            A dict following DEFAULT_MEMORY's schema.
        """
        if not os.path.exists(self.memory_path):
            logger.info("Memory file not found at %s – starting fresh.", self.memory_path)
            return self._default()

        try:
            with open(self.memory_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Merge missing top-level keys from the default schema
            merged = self._default()
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read memory file (%s). Starting with empty memory.", exc
            )
            return self._default()

    def save(self, memory: Dict) -> None:
        """Atomically write memory to disk.

        Writes to a temporary file first, then renames it to the target path
        so that a crash mid-write cannot leave a corrupt file behind.

        Args:
            memory: The memory dict to persist.
        """
        memory["last_updated"] = _now_iso()
        if memory.get("created_at") is None:
            memory["created_at"] = memory["last_updated"]

        dir_name = os.path.dirname(self.memory_path)
        try:
            # NamedTemporaryFile in the same directory so rename is atomic
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=dir_name,
                suffix=".tmp",
                delete=False,
            ) as tmp:
                json.dump(memory, tmp, indent=2, default=str)
                tmp_path = tmp.name

            os.replace(tmp_path, self.memory_path)
            logger.debug("Memory saved to %s.", self.memory_path)
        except OSError as exc:
            logger.error("Could not save memory: %s", exc)
            raise

    def is_run_processed(self, run_id: str) -> bool:
        """Return True if this run has already been handled in a previous cycle.

        Args:
            run_id: The KFP run ID to check.
        """
        memory = self.load()
        return run_id in memory.get("processed_runs", {})

    def mark_run_processed(
        self,
        run_id: str,
        status: str,
        details: Dict,
    ) -> None:
        """Record that a run has been processed and persist to disk.

        Args:
            run_id:  The KFP run ID.
            status:  A short outcome string, e.g. "pr_created", "reported", "skipped".
            details: Arbitrary dict with additional context (PR URL, failure type, …).
        """
        memory = self.load()
        memory.setdefault("processed_runs", {})[run_id] = {
            "status": status,
            "processed_at": _now_iso(),
            "details": details,
        }
        memory["stats"]["total_processed"] = memory["stats"].get("total_processed", 0) + 1
        self.save(memory)

    def update_stats(self, failure_type: str) -> None:
        """Increment the counter for a given failure type.

        Args:
            failure_type: String value of the FailureType enum, e.g. "OOM_KILL".
        """
        memory = self.load()
        counters = memory.setdefault("stats", {}).setdefault("failures_by_type", {})
        counters[failure_type] = counters.get(failure_type, 0) + 1
        self.save(memory)

    def get_recent_incidents(self, limit: int = 10) -> List[Dict]:
        """Return the most recent incidents recorded in memory.

        Args:
            limit: Maximum number of incidents to return.

        Returns:
            A list of incident dicts, most recent first.
        """
        memory = self.load()
        incidents = memory.get("incidents", [])
        return incidents[-limit:][::-1]  # most-recent first

    def append_incident(self, incident: Dict) -> None:
        """Append an incident summary and persist, capping at MAX_INCIDENTS.

        Args:
            incident: Arbitrary dict summarising the incident.
        """
        memory = self.load()
        incidents = memory.setdefault("incidents", [])
        incidents.append({**incident, "recorded_at": _now_iso()})
        # Keep only the most recent MAX_INCIDENTS entries
        if len(incidents) > MAX_INCIDENTS:
            memory["incidents"] = incidents[-MAX_INCIDENTS:]
        self.save(memory)

    def increment_pr_count(self) -> None:
        """Increment the PR-created counter in stats."""
        memory = self.load()
        memory.setdefault("stats", {})["prs_created"] = (
            memory["stats"].get("prs_created", 0) + 1
        )
        self.save(memory)

    def increment_report_count(self) -> None:
        """Increment the reports-generated counter in stats."""
        memory = self.load()
        memory.setdefault("stats", {})["reports_generated"] = (
            memory["stats"].get("reports_generated", 0) + 1
        )
        self.save(memory)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_directory(self) -> None:
        """Create parent directories for the memory file if they do not exist."""
        dir_name = os.path.dirname(self.memory_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

    @staticmethod
    def _default() -> Dict:
        """Return a fresh copy of the default memory schema."""
        import copy
        return copy.deepcopy(DEFAULT_MEMORY)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()
