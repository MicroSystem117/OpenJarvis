"""Tests for GitHub tools."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from openjarvis.core.registry import ToolRegistry
from openjarvis.tools.github_tool import (
    GithubIssueManageTool,
    GithubPrManageTool,
    GithubRepoCloneTool,
    GithubRepoCreateTool,
    GithubRepoListTool,
    GithubRepoViewTool,
)


def test_tools_registered() -> None:
    import importlib
    import openjarvis.tools.github_tool
    importlib.reload(openjarvis.tools.github_tool)
    assert ToolRegistry.get("github_repo_list") is not None
    assert ToolRegistry.get("github_repo_create") is not None
    assert ToolRegistry.get("github_repo_clone") is not None
    assert ToolRegistry.get("github_repo_view") is not None
    assert ToolRegistry.get("github_issue_manage") is not None
    assert ToolRegistry.get("github_pr_manage") is not None


def test_repo_list_tool() -> None:
    tool = GithubRepoListTool()
    fake_repos = [
        {
            "name": "test-repo",
            "nameWithOwner": "user/test-repo",
            "description": "A test repo",
            "visibility": "PUBLIC",
            "primaryLanguage": {"name": "Python"},
            "url": "https://github.com/user/test-repo",
        }
    ]

    mock_proc = mock.MagicMock(
        returncode=0,
        stdout=json.dumps(fake_repos),
        stderr="",
    )

    with mock.patch("shutil.which", return_value="/usr/bin/gh"):
        with mock.patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = tool.execute(limit=10)
            assert res.success is True
            assert "test-repo" in res.content
            assert "PUBLIC" in res.content
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert "repo" in call_args
            assert "list" in call_args


def test_repo_create_tool_remote(tmp_path) -> None:
    tool = GithubRepoCreateTool()
    mock_proc = mock.MagicMock(
        returncode=0,
        stdout="https://github.com/user/new-repo\n",
        stderr="",
    )

    with mock.patch("shutil.which", return_value="/usr/bin/gh"):
        with mock.patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = tool.execute(name="new-repo", private=True, description="My new project")
            assert res.success is True
            assert "new-repo" in res.content
            call_args = mock_run.call_args[0][0]
            assert "repo" in call_args
            assert "create" in call_args
            assert "new-repo" in call_args
            assert "--private" in call_args


def test_repo_clone_tool() -> None:
    tool = GithubRepoCloneTool()
    mock_proc = mock.MagicMock(returncode=0, stdout="Cloning into 'repo'...", stderr="")

    with mock.patch("shutil.which", return_value="/usr/bin/gh"):
        with mock.patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = tool.execute(repo="user/repo", destination="/tmp/clone-dir")
            assert res.success is True
            call_args = mock_run.call_args[0][0]
            assert "clone" in call_args
            assert "user/repo" in call_args


def test_issue_manage_tool() -> None:
    tool = GithubIssueManageTool()
    mock_proc = mock.MagicMock(returncode=0, stdout="https://github.com/user/repo/issues/1\n", stderr="")

    with mock.patch("shutil.which", return_value="/usr/bin/gh"):
        with mock.patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = tool.execute(action="create", repo="user/repo", title="Bug found", body="Fix it")
            assert res.success is True
            call_args = mock_run.call_args[0][0]
            assert "issue" in call_args
            assert "create" in call_args


def test_pr_manage_tool() -> None:
    tool = GithubPrManageTool()
    mock_proc = mock.MagicMock(returncode=0, stdout="No open pull requests in user/repo\n", stderr="")

    with mock.patch("shutil.which", return_value="/usr/bin/gh"):
        with mock.patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = tool.execute(action="list", repo="user/repo")
            assert res.success is True
            call_args = mock_run.call_args[0][0]
            assert "pr" in call_args
            assert "list" in call_args
