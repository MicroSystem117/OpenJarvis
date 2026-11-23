"""GitHub tools — repository and workflow management via GitHub CLI (gh)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 50 * 1024


def _check_gh() -> str | None:
    """Return an error message if gh CLI is not available."""
    if shutil.which("gh") is None:
        return "GitHub CLI (gh) is not installed on PATH. Run 'sudo pacman -S github-cli' to install it."
    return None


def _run_gh(args: list[str], cwd: str | Path = ".") -> ToolResult:
    """Run a gh command and return a ToolResult."""
    err = _check_gh()
    if err:
        return ToolResult(tool_name="github", content=err, success=False)

    try:
        res = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=60,
        )
        output = res.stdout
        if res.stderr:
            output += ("\n" if output else "") + res.stderr
        output = output.strip() or "(no output)"
        if len(output.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
            output = output[:_MAX_OUTPUT_BYTES] + "\n... (output truncated)"
        return ToolResult(
            tool_name="github",
            content=output,
            success=res.returncode == 0,
            metadata={"returncode": res.returncode},
        )
    except Exception as exc:
        return ToolResult(
            tool_name="github",
            content=f"Error executing GitHub CLI: {exc}",
            success=False,
        )


# ---------------------------------------------------------------------------
# GithubRepoListTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("github_repo_list")
class GithubRepoListTool(BaseTool):
    """List GitHub repositories owned by the user or an organization."""

    tool_id = "github_repo_list"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_repo_list",
            description=(
                "List GitHub repositories for the authenticated user or a specific owner."
                " Returns repository names, visibility (public/private), primary language, description and URLs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of repositories to return (default 30).",
                    },
                    "visibility": {
                        "type": "string",
                        "enum": ["all", "public", "private"],
                        "description": "Filter by visibility: 'all', 'public', or 'private'.",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Optional user or organization name. Defaults to authenticated user.",
                    },
                },
                "required": [],
            },
            category="vcs",
            required_capabilities=["file:read"],
        )

    def execute(self, **params: Any) -> ToolResult:
        limit = params.get("limit", 30)
        visibility = params.get("visibility", "all")
        owner = params.get("owner", "")

        args = [
            "repo",
            "list",
            "--limit",
            str(limit),
            "--json",
            "name,nameWithOwner,description,visibility,updatedAt,url,primaryLanguage",
        ]
        if owner:
            args.insert(2, owner)
        if visibility in ("public", "private"):
            args.extend(["--visibility", visibility])

        raw_res = _run_gh(args)
        if not raw_res.success:
            return raw_res

        try:
            data = json.loads(raw_res.content)
            if not data:
                return ToolResult(
                    tool_name="github_repo_list",
                    content="No se encontraron repositorios con los filtros indicados.",
                    success=True,
                )

            lines = ["### Repositorios de GitHub:\n"]
            lines.append("| Repositorio | Visibilidad | Lenguaje | Descripción | URL |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for r in data:
                name = r.get("nameWithOwner") or r.get("name", "N/A")
                vis = r.get("visibility", "PUBLIC")
                lang = (r.get("primaryLanguage") or {}).get("name", "N/A")
                desc = (r.get("description") or "Sin descripción").replace("\n", " ")
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                url = r.get("url", "")
                lines.append(f"| **{name}** | `{vis}` | {lang} | {desc} | {url} |")

            return ToolResult(
                tool_name="github_repo_list",
                content="\n".join(lines),
                success=True,
                metadata={"count": len(data)},
            )
        except Exception:
            return raw_res


# ---------------------------------------------------------------------------
# GithubRepoCreateTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("github_repo_create")
class GithubRepoCreateTool(BaseTool):
    """Create a new GitHub repository and optionally upload/push local code."""

    tool_id = "github_repo_create"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_repo_create",
            description=(
                "Create a new repository on GitHub (public or private)."
                " Can optionally initialize and publish an existing local project folder directly to GitHub in one step."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for the new repository (e.g. 'mi-proyecto' or 'org/repo').",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of the repository.",
                    },
                    "private": {
                        "type": "boolean",
                        "description": "Whether the repository should be private. Default is True.",
                    },
                    "source_path": {
                        "type": "string",
                        "description": (
                            "Optional local directory path to publish to the new repository."
                            " If provided, initializes git if needed, creates initial commit and pushes to GitHub."
                        ),
                    },
                    "clone": {
                        "type": "boolean",
                        "description": "Clone the newly created empty remote repository locally. Default is False.",
                    },
                },
                "required": ["name"],
            },
            category="vcs",
            required_capabilities=["file:read", "file:write"],
        )

    def execute(self, **params: Any) -> ToolResult:
        name = params.get("name", "").strip()
        if not name:
            return ToolResult(
                tool_name="github_repo_create",
                content="Error: Debes especificar el nombre del repositorio.",
                success=False,
            )

        description = params.get("description", "").strip()
        is_private = params.get("private", True)
        source_path = params.get("source_path", "").strip()
        clone = params.get("clone", False)

        args = ["repo", "create", name]
        if is_private:
            args.append("--private")
        else:
            args.append("--public")

        if description:
            args.extend(["--description", description])

        # Case 1: Uploading an existing local folder
        if source_path:
            p = Path(source_path).expanduser().resolve()
            if not p.is_dir():
                return ToolResult(
                    tool_name="github_repo_create",
                    content=f"Error: La ruta local especificada no existe o no es un directorio: {source_path}",
                    success=False,
                )

            # Ensure git is initialized in the local folder
            git_dir = p / ".git"
            if not git_dir.exists():
                init_res = subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=str(p),
                    capture_output=True,
                    text=True,
                )
                if init_res.returncode != 0:
                    return ToolResult(
                        tool_name="github_repo_create",
                        content=f"Error al inicializar git en {p}: {init_res.stderr}",
                        success=False,
                    )

            # Check if there are commits; if not, create initial commit
            rev_res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(p),
                capture_output=True,
                text=True,
            )
            if rev_res.returncode != 0:
                subprocess.run(["git", "add", "-A"], cwd=str(p), check=False)
                subprocess.run(
                    ["git", "commit", "-m", "Initial commit from OpenJarvis"],
                    cwd=str(p),
                    check=False,
                )

            args.extend(["--source", str(p), "--remote", "origin", "--push"])
            res = _run_gh(args, cwd=p)
            if res.success:
                return ToolResult(
                    tool_name="github_repo_create",
                    content=(
                        f"✅ Repositorio '{name}' creado y subido exitosamente a GitHub.\n"
                        f"- Directorio local origen: `{p}`\n"
                        f"- Visibilidad: `{'Privado' if is_private else 'Público'}`\n"
                        f"- Salida:\n{res.content}"
                    ),
                    success=True,
                )
            return res

        # Case 2: Creating remote repository without local source
        if clone:
            args.append("--clone")
        else:
            args.append("--add-readme")

        res = _run_gh(args)
        if res.success:
            return ToolResult(
                tool_name="github_repo_create",
                content=(
                    f"✅ Repositorio '{name}' creado exitosamente en GitHub.\n"
                    f"- Visibilidad: `{'Privado' if is_private else 'Público'}`\n"
                    f"- Salida:\n{res.content}"
                ),
                success=True,
            )
        return res


# ---------------------------------------------------------------------------
# GithubRepoCloneTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("github_repo_clone")
class GithubRepoCloneTool(BaseTool):
    """Clone a GitHub repository to local disk."""

    tool_id = "github_repo_clone"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_repo_clone",
            description="Clone a GitHub repository to a local directory using the GitHub CLI.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (e.g. 'MicroSystem117/mi-repo' or full URL).",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Optional local directory path to clone into.",
                    },
                },
                "required": ["repo"],
            },
            category="vcs",
            required_capabilities=["file:read", "file:write"],
        )

    def execute(self, **params: Any) -> ToolResult:
        repo = params.get("repo", "").strip()
        if not repo:
            return ToolResult(
                tool_name="github_repo_clone",
                content="Error: Debes especificar el repositorio a clonar.",
                success=False,
            )

        destination = params.get("destination", "").strip()
        args = ["repo", "clone", repo]
        if destination:
            args.append(destination)

        return _run_gh(args)


# ---------------------------------------------------------------------------
# GithubRepoViewTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("github_repo_view")
class GithubRepoViewTool(BaseTool):
    """View details of a GitHub repository."""

    tool_id = "github_repo_view"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_repo_view",
            description="View details, README, and metadata of a GitHub repository.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (e.g. 'owner/repo'). Defaults to current repository if omitted.",
                    },
                },
                "required": [],
            },
            category="vcs",
            required_capabilities=["file:read"],
        )

    def execute(self, **params: Any) -> ToolResult:
        repo = params.get("repo", "").strip()
        args = ["repo", "view"]
        if repo:
            args.append(repo)
        return _run_gh(args)


# ---------------------------------------------------------------------------
# GithubIssueManageTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("github_issue_manage")
class GithubIssueManageTool(BaseTool):
    """List, view, create, or close GitHub issues in a repository."""

    tool_id = "github_issue_manage"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_issue_manage",
            description="Manage issues in a GitHub repository: list, view, create, or close issues.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "view", "create", "close"],
                        "description": "The action to perform: 'list', 'view', 'create', or 'close'.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (e.g. 'MicroSystem117/mi-repo').",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title of the issue (required for 'create').",
                    },
                    "body": {
                        "type": "string",
                        "description": "Body / description of the issue.",
                    },
                    "issue_number": {
                        "type": "integer",
                        "description": "Issue number (required for 'view' and 'close').",
                    },
                },
                "required": ["action"],
            },
            category="vcs",
            required_capabilities=["file:read", "file:write"],
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action", "list")
        repo = params.get("repo", "").strip()

        args = ["issue"]
        if action == "list":
            args.append("list")
            if repo:
                args.extend(["--repo", repo])
        elif action == "view":
            num = params.get("issue_number")
            if not num:
                return ToolResult(tool_name="github_issue_manage", content="Error: Falta issue_number.", success=False)
            args.extend(["view", str(num)])
            if repo:
                args.extend(["--repo", repo])
        elif action == "create":
            title = params.get("title", "").strip()
            if not title:
                return ToolResult(tool_name="github_issue_manage", content="Error: Falta el título del issue.", success=False)
            body = params.get("body", "").strip()
            args.extend(["create", "--title", title, "--body", body or title])
            if repo:
                args.extend(["--repo", repo])
        elif action == "close":
            num = params.get("issue_number")
            if not num:
                return ToolResult(tool_name="github_issue_manage", content="Error: Falta issue_number.", success=False)
            args.extend(["close", str(num)])
            if repo:
                args.extend(["--repo", repo])
        else:
            return ToolResult(tool_name="github_issue_manage", content=f"Acción '{action}' no soportada.", success=False)

        return _run_gh(args)


# ---------------------------------------------------------------------------
# GithubPrManageTool
# ---------------------------------------------------------------------------


@ToolRegistry.register("github_pr_manage")
class GithubPrManageTool(BaseTool):
    """List, view, or create GitHub pull requests."""

    tool_id = "github_pr_manage"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_pr_manage",
            description="Manage pull requests in a GitHub repository: list, view, status, or create.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "view", "status", "create"],
                        "description": "Action: 'list', 'view', 'status', or 'create'.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (e.g. 'MicroSystem117/mi-repo').",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title of the PR (for 'create').",
                    },
                    "body": {
                        "type": "string",
                        "description": "Description of the PR (for 'create').",
                    },
                    "pr_number": {
                        "type": "integer",
                        "description": "PR number (for 'view').",
                    },
                },
                "required": ["action"],
            },
            category="vcs",
            required_capabilities=["file:read", "file:write"],
        )

    def execute(self, **params: Any) -> ToolResult:
        action = params.get("action", "list")
        repo = params.get("repo", "").strip()

        args = ["pr"]
        if action == "list":
            args.append("list")
            if repo:
                args.extend(["--repo", repo])
        elif action == "status":
            args.append("status")
            if repo:
                args.extend(["--repo", repo])
        elif action == "view":
            num = params.get("pr_number")
            if not num:
                return ToolResult(tool_name="github_pr_manage", content="Error: Falta pr_number.", success=False)
            args.extend(["view", str(num)])
            if repo:
                args.extend(["--repo", repo])
        elif action == "create":
            title = params.get("title", "").strip()
            if not title:
                return ToolResult(tool_name="github_pr_manage", content="Error: Falta el título del PR.", success=False)
            body = params.get("body", "").strip()
            args.extend(["create", "--title", title, "--body", body or title])
            if repo:
                args.extend(["--repo", repo])
        else:
            return ToolResult(tool_name="github_pr_manage", content=f"Acción '{action}' no soportada.", success=False)

        return _run_gh(args)


__all__ = [
    "GithubRepoListTool",
    "GithubRepoCreateTool",
    "GithubRepoCloneTool",
    "GithubRepoViewTool",
    "GithubIssueManageTool",
    "GithubPrManageTool",
]
