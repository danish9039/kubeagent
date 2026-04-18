"""
Test suite for KubeAgent.

Covers:
- MemoryManager: load, save, is_processed, mark_processed, update_stats
- FailureClassifier: all 7 failure types + UNKNOWN
- YAMLPatchGenerator: OOM, WRONG_IMAGE, MISSING_ENV, CRASH_LOOP patches
- KFPConnector: mocked API responses
- Integration test placeholder
"""
import json
import os
import tempfile
import textwrap
from datetime import datetime, timezone
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# MemoryManager tests
# ---------------------------------------------------------------------------


class TestMemoryManager:
    """Tests for the persistent JSON memory manager."""

    @pytest.fixture
    def mem(self, tmp_path):
        from kubeagent.agent.memory import MemoryManager

        return MemoryManager(str(tmp_path / "test_memory.json"))

    def test_load_returns_default_when_file_missing(self, mem):
        """A fresh MemoryManager should return the default schema."""
        data = mem.load()
        assert "processed_runs" in data
        assert data["stats"]["total_processed"] == 0
        assert data["incidents"] == []

    def test_save_and_reload(self, mem):
        """Data written via save() must be readable back."""
        data = mem.load()
        data["processed_runs"]["run-001"] = {"status": "pr_created"}
        mem.save(data)

        reloaded = mem.load()
        assert "run-001" in reloaded["processed_runs"]
        assert reloaded["processed_runs"]["run-001"]["status"] == "pr_created"

    def test_save_sets_timestamps(self, mem):
        """save() must set last_updated; created_at set on first save."""
        data = mem.load()
        mem.save(data)
        reloaded = mem.load()
        assert reloaded["last_updated"] is not None
        assert reloaded["created_at"] is not None

    def test_atomic_write_uses_rename(self, mem):
        """A .tmp file must not persist after a successful save."""
        data = mem.load()
        mem.save(data)
        tmp_files = [
            f for f in os.listdir(os.path.dirname(mem.memory_path))
            if f.endswith(".tmp")
        ]
        assert tmp_files == [], "Temporary files should be cleaned up after save."

    def test_is_run_processed_false_for_new_run(self, mem):
        assert mem.is_run_processed("run-abc") is False

    def test_mark_run_processed_marks_and_persists(self, mem):
        mem.mark_run_processed("run-xyz", "pr_created", {"pr_url": "https://example.com/pr/1"})
        assert mem.is_run_processed("run-xyz") is True

    def test_mark_run_processed_increments_counter(self, mem):
        mem.mark_run_processed("run-1", "done", {})
        mem.mark_run_processed("run-2", "done", {})
        data = mem.load()
        assert data["stats"]["total_processed"] == 2

    def test_update_stats_increments_failure_type(self, mem):
        mem.update_stats("OOM_KILL")
        mem.update_stats("OOM_KILL")
        mem.update_stats("TIMEOUT")
        data = mem.load()
        assert data["stats"]["failures_by_type"]["OOM_KILL"] == 2
        assert data["stats"]["failures_by_type"]["TIMEOUT"] == 1

    def test_get_recent_incidents_empty(self, mem):
        assert mem.get_recent_incidents() == []

    def test_get_recent_incidents_returns_most_recent_first(self, mem):
        for i in range(5):
            mem.append_incident({"run_id": f"run-{i}", "type": "OOM"})
        incidents = mem.get_recent_incidents(limit=3)
        assert len(incidents) == 3
        # Most recent (index 4) should come first
        assert incidents[0]["run_id"] == "run-4"

    def test_load_survives_corrupt_file(self, mem):
        """A corrupt JSON file should fall back to the default schema."""
        with open(mem.memory_path, "w") as fh:
            fh.write("NOT_VALID_JSON!!!")
        data = mem.load()
        assert data["stats"]["total_processed"] == 0


# ---------------------------------------------------------------------------
# FailureClassifier tests
# ---------------------------------------------------------------------------


class TestFailureClassifier:
    """Tests for the rule-based failure classifier."""

    @pytest.fixture
    def clf(self):
        from kubeagent.reasoning.classifier import FailureClassifier

        return FailureClassifier()

    def test_classify_oom(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(
            logs="Container was OOMKilled, exit code 137",
            events=[],
            error_message="memory limit exceeded",
        )
        assert result.failure_type == FailureType.OOM_KILL
        assert result.confidence > 0.3
        assert result.fix_strategy == "increase_memory_limit"

    def test_classify_wrong_image(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(
            logs="ErrImagePull: repository does not exist",
            events=[{"type": "Warning", "reason": "Failed", "message": "ImagePullBackOff"}],
            error_message="",
        )
        assert result.failure_type == FailureType.WRONG_IMAGE
        assert "image" in result.patch_target

    def test_classify_missing_env(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(
            logs="KeyError: 'DATABASE_URL'\nenvironment variable not set",
            events=[],
            error_message="",
        )
        assert result.failure_type == FailureType.MISSING_ENV

    def test_classify_crash_loop(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(
            logs="CrashLoopBackOff detected, panic: runtime error",
            events=[{"type": "Warning", "reason": "BackOff", "message": "Back-off restarting"}],
            error_message="",
        )
        assert result.failure_type == FailureType.CRASH_LOOP

    def test_classify_resource_quota(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(
            logs="",
            events=[{"type": "Warning", "reason": "FailedScheduling", "message": "exceeded quota for pods"}],
            error_message="ResourceQuota exceeded",
        )
        assert result.failure_type == FailureType.RESOURCE_QUOTA

    def test_classify_timeout(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(
            logs="context deadline exceeded after 3600s",
            events=[],
            error_message="step timeout",
        )
        assert result.failure_type == FailureType.TIMEOUT

    def test_classify_dependency_fail(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(
            logs="upstream task failed\nModuleNotFoundError: No module named 'tensorflow'",
            events=[],
            error_message="dependency failed",
        )
        assert result.failure_type == FailureType.DEPENDENCY_FAIL

    def test_classify_unknown(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        result = clf.classify(logs="", events=[], error_message="")
        assert result.failure_type == FailureType.UNKNOWN
        assert result.confidence == 0.0

    def test_confidence_scales_with_match_count(self, clf):
        from kubeagent.reasoning.classifier import FailureType

        # One match should give lower confidence than many matches
        r1 = clf.classify(logs="OOMKilled", events=[], error_message="")
        r2 = clf.classify(
            logs="OOMKilled exit code 137 out of memory Cannot allocate memory Killed",
            events=[],
            error_message="memory limit exceeded",
        )
        assert r2.confidence >= r1.confidence


# ---------------------------------------------------------------------------
# YAMLPatchGenerator tests
# ---------------------------------------------------------------------------

SAMPLE_POD_YAML = textwrap.dedent("""
    apiVersion: v1
    kind: Pod
    metadata:
      name: training-pod
    spec:
      containers:
        - name: trainer
          image: my-registry/trainer:v1.2.3
          resources:
            limits:
              memory: "128Mi"
              cpu: "500m"
            requests:
              memory: "64Mi"
              cpu: "100m"
""").strip()

SAMPLE_DEPLOYMENT_YAML = textwrap.dedent("""
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: model-server
    spec:
      template:
        spec:
          containers:
            - name: server
              image: my-registry/server:latest
              resources:
                requests:
                  memory: "256Mi"
                  cpu: "500m"
""").strip()


class TestYAMLPatchGenerator:
    """Tests for the YAML patch generator."""

    @pytest.fixture
    def gen(self):
        from kubeagent.reasoning.patch_gen import YAMLPatchGenerator

        return YAMLPatchGenerator()

    def test_oom_patch_increases_memory_limit(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        patched_yaml = gen.generate_patch(FailureType.OOM_KILL, SAMPLE_POD_YAML)
        manifest = yaml.safe_load(patched_yaml)
        container = manifest["spec"]["containers"][0]
        new_limit = container["resources"]["limits"]["memory"]
        # 128Mi * 4 = 512Mi
        assert new_limit == "512Mi"

    def test_oom_patch_with_custom_multiplier(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        patched_yaml = gen.generate_patch(
            FailureType.OOM_KILL,
            SAMPLE_POD_YAML,
            suggested_fix={"memory_multiplier": 2.0, "min_memory": "256Mi"},
        )
        manifest = yaml.safe_load(patched_yaml)
        limit = manifest["spec"]["containers"][0]["resources"]["limits"]["memory"]
        assert limit == "256Mi"

    def test_wrong_image_replaces_tag(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        patched_yaml = gen.generate_patch(FailureType.WRONG_IMAGE, SAMPLE_POD_YAML)
        manifest = yaml.safe_load(patched_yaml)
        image = manifest["spec"]["containers"][0]["image"]
        assert image.endswith(":latest")
        assert "my-registry/trainer" in image

    def test_missing_env_adds_placeholder(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        patched_yaml = gen.generate_patch(
            FailureType.MISSING_ENV,
            SAMPLE_POD_YAML,
            suggested_fix={"env_key": "DATABASE_URL", "placeholder_value": "REPLACE_ME"},
        )
        manifest = yaml.safe_load(patched_yaml)
        container = manifest["spec"]["containers"][0]
        env_keys = [e["name"] for e in container.get("env", [])]
        assert "DATABASE_URL" in env_keys

    def test_crash_loop_adds_liveness_probe(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        patched_yaml = gen.generate_patch(FailureType.CRASH_LOOP, SAMPLE_POD_YAML)
        manifest = yaml.safe_load(patched_yaml)
        container = manifest["spec"]["containers"][0]
        assert "livenessProbe" in container
        assert container["livenessProbe"]["initialDelaySeconds"] >= 30

    def test_resource_quota_reduces_requests(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        patched_yaml = gen.generate_patch(
            FailureType.RESOURCE_QUOTA,
            SAMPLE_DEPLOYMENT_YAML,
            suggested_fix={"request_reduction_factor": 0.5},
        )
        manifest = yaml.safe_load(patched_yaml)
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        # 256Mi * 0.5 = 128Mi
        assert container["resources"]["requests"]["memory"] == "128Mi"

    def test_invalid_yaml_returns_original(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        original = "THIS IS NOT YAML: [[[["
        result = gen.generate_patch(FailureType.OOM_KILL, original)
        assert result == original

    def test_unknown_failure_returns_valid_yaml(self, gen):
        from kubeagent.reasoning.classifier import FailureType

        # UNKNOWN should not crash and should return parseable YAML
        result = gen.generate_patch(FailureType.UNKNOWN, SAMPLE_POD_YAML)
        manifest = yaml.safe_load(result)
        assert manifest["kind"] == "Pod"

    def test_increase_memory_small_value(self, gen):
        """50Mi * 4 = 200Mi (above 512Mi min → returns 512Mi)."""
        result = gen._increase_memory("50Mi", multiplier=4.0, minimum="512Mi")
        assert result == "512Mi"

    def test_increase_memory_large_value(self, gen):
        """2Gi * 4 = 8Gi."""
        result = gen._increase_memory("2Gi", multiplier=4.0, minimum="512Mi")
        assert result == "8Gi"

    def test_increase_memory_invalid_string(self, gen):
        """Invalid string should return the minimum."""
        result = gen._increase_memory("INVALID", minimum="512Mi")
        assert result == "512Mi"


# ---------------------------------------------------------------------------
# KFPConnector mock tests
# ---------------------------------------------------------------------------


class TestKFPConnectorMocked:
    """Tests for KFPConnector using mocked KFP API responses."""

    @pytest.fixture
    def mock_kfp_client(self):
        """Create a KFPConnector with a fully mocked kfp.Client.

        Mocks the kfp and kfp_server_api modules at import time so the test
        works even if those packages are not installed in the environment.
        """
        mock_kfp_module = MagicMock()
        mock_kfp_server_api = MagicMock()
        mock_client_instance = MagicMock()
        mock_kfp_module.Client.return_value = mock_client_instance

        with patch.dict(
            "sys.modules",
            {
                "kfp": mock_kfp_module,
                "kfp_server_api": mock_kfp_server_api,
            },
        ):
            import importlib
            import sys

            # Remove cached module if already loaded without mocks
            sys.modules.pop("kubeagent.connectors.kfp_client", None)

            from kubeagent.connectors.kfp_client import KFPConnector

            connector = KFPConnector(endpoint="http://localhost:8888")
            connector._client = mock_client_instance
            yield connector, mock_client_instance

            # Clean up so other tests get a fresh import
            sys.modules.pop("kubeagent.connectors.kfp_client", None)

    def test_get_failed_runs_returns_list(self, mock_kfp_client):
        connector, mock_client = mock_kfp_client

        # Build mock run
        mock_run = MagicMock()
        mock_run.run_id = "run-123"
        mock_run.display_name = "training-pipeline"
        mock_run.state = "FAILED"
        mock_run.created_at = datetime.now(tz=timezone.utc)
        mock_run.finished_at = datetime.now(tz=timezone.utc)
        mock_run.error = MagicMock()
        mock_run.error.message = "OOMKilled"
        mock_run.runtime_details = None

        mock_response = MagicMock()
        mock_response.runs = [mock_run]
        mock_client._run_api.list_runs.return_value = mock_response

        with patch("kubeagent.connectors.kfp_client.kfp_server_api") as mock_api:
            mock_api.V2beta1Predicate = MagicMock()
            mock_api.V2beta1Filter = MagicMock()
            mock_api.V2beta1PredicateOperation = MagicMock()

            runs = connector.get_failed_runs(last_n_hours=1)

        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-123"

    def test_retry_run_returns_true_on_success(self, mock_kfp_client):
        connector, mock_client = mock_kfp_client
        mock_client._run_api.retry_run.return_value = None
        result = connector.retry_run("run-abc")
        assert result is True

    def test_retry_run_returns_false_on_exception(self, mock_kfp_client):
        connector, mock_client = mock_kfp_client
        import kfp_server_api

        mock_client._run_api.retry_run.side_effect = Exception("API Error")
        result = connector.retry_run("run-abc")
        assert result is False

    def test_get_run_details_returns_dict(self, mock_kfp_client):
        connector, mock_client = mock_kfp_client

        mock_run = MagicMock()
        mock_run.run_id = "run-detail-1"
        mock_run.display_name = "test-pipeline"
        mock_run.state = "FAILED"
        mock_run.created_at = datetime.now(tz=timezone.utc)
        mock_run.finished_at = datetime.now(tz=timezone.utc)
        mock_run.error = None
        mock_run.runtime_details = None

        mock_client._run_api.get_run.return_value = mock_run
        result = connector.get_run_details("run-detail-1")
        assert result["run_id"] == "run-detail-1"


# ---------------------------------------------------------------------------
# IncidentReporter tests
# ---------------------------------------------------------------------------


class TestIncidentReporter:
    """Tests for the Markdown incident report generator."""

    @pytest.fixture
    def reporter(self, tmp_path):
        from kubeagent.reasoning.reporter import IncidentReporter

        return IncidentReporter(reports_dir=str(tmp_path / "reports"))

    def test_generate_report_creates_file(self, reporter):
        path = reporter.generate_report(
            run_id="run-abc-123",
            failure_type="OOM_KILL",
            root_cause="Container exceeded 128Mi memory limit.",
            fix_applied="Increased memory limit to 512Mi.",
            pr_url="https://github.com/org/repo/pull/42",
            logs_excerpt="OOMKilled exit code 137",
        )
        assert os.path.exists(path)
        assert path.endswith(".md")

    def test_report_contains_run_id(self, reporter):
        path = reporter.generate_report(
            run_id="run-unique-999",
            failure_type="TIMEOUT",
            root_cause="Step timed out after 3600s.",
            fix_applied="Doubled activeDeadlineSeconds.",
        )
        with open(path) as fh:
            content = fh.read()
        assert "run-unique-999" in content

    def test_report_includes_pr_link(self, reporter):
        pr_url = "https://github.com/org/repo/pull/7"
        path = reporter.generate_report(
            run_id="run-pr-test",
            failure_type="OOM_KILL",
            root_cause="OOM",
            fix_applied="Memory increased",
            pr_url=pr_url,
        )
        with open(path) as fh:
            content = fh.read()
        assert pr_url in content

    def test_report_includes_k8s_events_table(self, reporter):
        events = [
            {"type": "Warning", "reason": "OOMKilling", "message": "Memory limit exceeded", "timestamp": "2024-01-01T00:00:00Z", "count": 3},
        ]
        path = reporter.generate_report(
            run_id="run-events",
            failure_type="OOM_KILL",
            root_cause="OOM",
            fix_applied="Memory increased",
            k8s_events=events,
        )
        with open(path) as fh:
            content = fh.read()
        assert "OOMKilling" in content

    def test_long_logs_are_truncated(self, reporter):
        long_logs = "x" * 10_000
        path = reporter.generate_report(
            run_id="run-long-logs",
            failure_type="UNKNOWN",
            root_cause="unknown",
            fix_applied="manual",
            logs_excerpt=long_logs,
        )
        with open(path) as fh:
            content = fh.read()
        # Report should not contain 10k x's
        assert content.count("x") <= 3010  # 3000 + a small buffer


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------


class TestSettings:
    """Tests for the Settings dataclass."""

    def test_default_settings_have_expected_values(self):
        from kubeagent.config.settings import Settings

        s = Settings()
        assert s.kfp_endpoint == "http://localhost:8888"
        assert s.claude_model == "claude-sonnet-4-5"
        assert s.poll_interval_seconds == 300
        assert s.max_tool_iterations == 10

    def test_validate_raises_on_missing_api_key(self):
        from kubeagent.config.settings import Settings

        s = Settings()
        s.anthropic_api_key = ""
        s.github_token = "token"
        s.github_repo = "org/repo"
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            s.validate()

    def test_validate_raises_on_placeholder_repo(self):
        from kubeagent.config.settings import Settings

        s = Settings()
        s.anthropic_api_key = "sk-ant-test"
        s.github_token = "ghp_test"
        s.github_repo = "owner/repo"  # still the placeholder
        with pytest.raises(ValueError, match="GITHUB_REPO"):
            s.validate()

    def test_validate_passes_with_valid_config(self):
        from kubeagent.config.settings import Settings

        s = Settings()
        s.anthropic_api_key = "sk-ant-real"
        s.github_token = "ghp_real"
        s.github_repo = "my-org/ml-pipelines"
        # Should not raise
        s.validate()


# ---------------------------------------------------------------------------
# Integration test placeholder
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end integration tests (require live services – skipped in CI)."""

    @pytest.mark.skip(reason="Requires live KFP, MLflow, Kubernetes, and GitHub services.")
    def test_full_oom_remediation_flow(self):
        """
        Integration test outline:
        1. Inject a failing KFP run with OOM error into a test cluster.
        2. Run KubeAgent._process_failed_run() against it.
        3. Assert that a GitHub PR was created with increased memory limits.
        4. Assert that an incident report was written.
        5. Assert that the run is marked as processed in memory.
        """
        pass

    @pytest.mark.skip(reason="Requires live services.")
    def test_mlflow_metric_correlation(self):
        """
        Integration test outline:
        1. Create an MLflow run with a degraded accuracy metric.
        2. Run the MLflow connector's detect_metric_degradation().
        3. Assert it returns True for the degraded metric.
        """
        pass
