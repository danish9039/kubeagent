"""
GitHub connector for KubeAgent.

Uses PyGithub to create branches, commit fix files, and open pull requests
for automated remediation patches.
"""
import base64
import logging
from typing import Optional, Tuple

from github import Github, GithubException

logger = logging.getLogger(__name__)


class GitHubConnector:
    """Creates fix branches and pull requests on GitHub via the PyGithub library."""

    def __init__(self, token: str, default_repo: str) -> None:
        """Initialise the GitHub client.

        Args:
            token:        A personal access token (or app token) with
                          ``repo`` (full control of private repositories) scope.
            default_repo: Repository in ``owner/repo`` format,
                          e.g. ``"my-org/ml-pipelines"``.
        """
        self._gh = Github(token)
        self._repo_name = default_repo
        try:
            self._repo = self._gh.get_repo(default_repo)
            logger.info("GitHub connector initialised for repo %s.", default_repo)
        except GithubException as exc:
            logger.error(
                "Failed to access GitHub repo %s: %s", default_repo, exc
            )
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_fix_pr(
        self,
        branch_name: str,
        file_path: str,
        file_content: str,
        pr_title: str,
        pr_body: str,
        base_branch: str = "main",
    ) -> str:
        """Create a branch, commit the fix file, and open a pull request.

        If the target branch already exists, it is reused.  If the file
        already exists in the branch, the existing file is updated; otherwise
        a new file is created.

        Args:
            branch_name:   Name for the new branch, e.g.
                           ``"kubeagent/fix-oom-run-abc123"``.
            file_path:     Repository-relative path of the file to create/update,
                           e.g. ``"pipelines/training.yaml"``.
            file_content:  Full text content of the file.
            pr_title:      Title of the pull request.
            pr_body:       Markdown body of the pull request.
            base_branch:   Branch to merge into (default ``"main"``).

        Returns:
            URL of the created pull request.

        Raises:
            GithubException: If the GitHub API call fails.
        """
        # Ensure the branch exists
        self._get_or_create_branch(self._repo, branch_name, base_branch)

        # Commit the file (create or update)
        exists, sha = self._file_exists(self._repo, file_path, branch_name)
        commit_message = f"kubeagent: automated fix – {pr_title}"
        encoded_content = file_content.encode("utf-8")

        try:
            if exists and sha:
                self._repo.update_file(
                    path=file_path,
                    message=commit_message,
                    content=encoded_content,
                    sha=sha,
                    branch=branch_name,
                )
                logger.info("Updated file %s on branch %s.", file_path, branch_name)
            else:
                self._repo.create_file(
                    path=file_path,
                    message=commit_message,
                    content=encoded_content,
                    branch=branch_name,
                )
                logger.info("Created file %s on branch %s.", file_path, branch_name)
        except GithubException as exc:
            logger.error("Failed to commit file %s: %s", file_path, exc)
            raise

        # Check if a PR already exists for this branch
        if self.pr_exists_for_branch(branch_name):
            # Return the existing PR URL
            for pr in self._repo.get_pulls(
                state="open", base=base_branch, head=f"{self._repo.owner.login}:{branch_name}"
            ):
                logger.info("PR already exists for branch %s: %s", branch_name, pr.html_url)
                return pr.html_url

        # Open the pull request
        try:
            pr = self._repo.create_pull(
                title=pr_title,
                body=pr_body,
                head=branch_name,
                base=base_branch,
            )
            logger.info("Pull request created: %s", pr.html_url)
            return pr.html_url
        except GithubException as exc:
            logger.error("Failed to create pull request: %s", exc)
            raise

    def pr_exists_for_branch(self, branch_name: str) -> bool:
        """Return True if an open PR already exists for the given branch.

        Args:
            branch_name: The head branch name to check.

        Returns:
            True if at least one open PR targets this branch.
        """
        try:
            head_ref = f"{self._repo.owner.login}:{branch_name}"
            prs = list(self._repo.get_pulls(state="open", head=head_ref))
            return len(prs) > 0
        except GithubException as exc:
            logger.warning(
                "Could not check for existing PRs on branch %s: %s", branch_name, exc
            )
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_branch(
        self, repo, branch_name: str, base_branch: str
    ) -> str:
        """Return ``branch_name`` if it exists, else create it from ``base_branch``.

        Args:
            repo:        PyGithub Repository object.
            branch_name: Desired new (or existing) branch name.
            base_branch: Source branch to fork from.

        Returns:
            The branch name (always ``branch_name``).
        """
        ref_name = f"refs/heads/{branch_name}"

        # Check if branch already exists
        try:
            repo.get_git_ref(f"heads/{branch_name}")
            logger.debug("Branch %s already exists – reusing.", branch_name)
            return branch_name
        except GithubException as exc:
            if exc.status != 404:
                raise  # unexpected error

        # Branch does not exist – create it
        try:
            base_ref = repo.get_branch(base_branch)
            sha = base_ref.commit.sha
            repo.create_git_ref(ref=ref_name, sha=sha)
            logger.info(
                "Created branch %s from %s (SHA %s).", branch_name, base_branch, sha[:7]
            )
        except GithubException as exc:
            logger.error(
                "Failed to create branch %s from %s: %s", branch_name, base_branch, exc
            )
            raise

        return branch_name

    def _file_exists(
        self, repo, file_path: str, branch: str
    ) -> Tuple[bool, Optional[str]]:
        """Check whether a file exists in a branch and return its SHA if so.

        Args:
            repo:      PyGithub Repository object.
            file_path: Repository-relative file path.
            branch:    Branch name.

        Returns:
            A tuple ``(exists, sha)`` where ``sha`` is the blob SHA required
            by the GitHub update-file API, or ``None`` if the file is absent.
        """
        try:
            contents = repo.get_contents(file_path, ref=branch)
            # get_contents can return a list for directories
            if isinstance(contents, list):
                return False, None
            return True, contents.sha
        except GithubException as exc:
            if exc.status == 404:
                return False, None
            logger.warning(
                "Unexpected error checking file %s on branch %s: %s",
                file_path, branch, exc,
            )
            return False, None
