#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║        BULK REPOSITORY UPDATER — GitHub Copilot Agent        ║
║  Automates code updates across 200+ GitHub repositories      ║
║                                                              ║
║  Phases:                                                     ║
║    1. Q&A      — Agent asks clarifying questions             ║
║    2. Planning — Generates a full execution plan             ║
║    3. Execution— Creates branches, applies changes, PRs      ║
║    4. Summary  — Lists all PRs raised + saves reports        ║
║                                                              ║
║  Every destructive step requires human confirmation (yes/no) ║
╚══════════════════════════════════════════════════════════════╝

Usage:
    python bulk_updater.py
    python bulk_updater.py --config my_config.yaml
    python bulk_updater.py --dry-run        (simulate, no GitHub changes)
    python bulk_updater.py --skip-confirm   (auto-approve all prompts)

Requirements:
    pip install PyGithub PyYAML
"""

import os
import re
import sys
import json
import yaml
import argparse
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from pathlib import Path

# ── Dependency check ────────────────────────────────────────────────────────
try:
    from github import Github, GithubException
except ImportError:
    print("\n  ✗ ERROR: PyGithub is not installed.")
    print("    Run:  pip install PyGithub PyYAML\n")
    sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# Data Models
# ════════════════════════════════════════════════════════════════════════════

class GlobalConfig:
    """Top-level settings from repos_config.yaml"""

    def __init__(self, data: dict):
        self.github_token: str = data.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")
        self.organization: str = data.get("organization", "")
        self.branch_prefix: str = data.get("branch_prefix", "auto-update/")
        self.instructions_dir: str = data.get("instructions_dir", "instructions")
        self.pr_reviewers: List[str] = data.get("pr_reviewers", [])
        self.pr_assignees: List[str] = data.get("pr_assignees", [])
        self.dry_run: bool = data.get("dry_run", False)
        self.repositories: List[dict] = data.get("repositories", [])


class Instruction:
    """One instruction set loaded from instructions/<ID>.yaml"""

    def __init__(self, inst_id: str, data: dict):
        self.id: str = inst_id
        self.description: str = data.get("description", inst_id)
        self.pr_title: str = data.get("pr_title", f"[Auto] {self.description}")
        self.pr_description: str = data.get("pr_description", "")
        self.check_exists: dict = data.get("check_exists", {})
        self.changes: List[dict] = data.get("changes", [])
        self.skip_if_branch_exists: bool = data.get("skip_if_branch_exists", True)
        self.labels: List[str] = data.get("labels", [])


# ════════════════════════════════════════════════════════════════════════════
# Main Agent
# ════════════════════════════════════════════════════════════════════════════

class BulkRepoUpdater:

    BANNER = """
╔══════════════════════════════════════════════════════════════╗
║        BULK REPOSITORY UPDATER — GitHub Copilot Agent        ║
╚══════════════════════════════════════════════════════════════╝"""

    def __init__(self, config_file: str = "repos_config.yaml",
                 dry_run_override: bool = False,
                 skip_confirm: bool = False):
        self.config_file = config_file
        self.dry_run_override = dry_run_override
        self.skip_confirm = skip_confirm

        self.config: Optional[GlobalConfig] = None
        self.instructions: Dict[str, Instruction] = {}
        self.g: Optional[Github] = None

        self.results = {
            "timestamp": datetime.now().isoformat(),
            "prs_created": [],
            "skipped": [],
            "failed": [],
            "dry_run_actions": [],
        }

    # ── Loaders ─────────────────────────────────────────────────────────────

    def load_config(self) -> bool:
        p = Path(self.config_file)
        if not p.exists():
            self._err(f"Config file '{self.config_file}' not found.")
            self._info("Copy repos_config.yaml from the template and fill it in.")
            return False

        with open(p) as f:
            raw = yaml.safe_load(f)

        self.config = GlobalConfig(raw)

        if self.dry_run_override:
            self.config.dry_run = True

        return True

    def load_instructions(self) -> bool:
        inst_dir = Path(self.config.instructions_dir)
        if not inst_dir.exists():
            self._err(f"Instructions directory '{inst_dir}' not found.")
            return False

        files = sorted(list(inst_dir.glob("*.yaml")) + list(inst_dir.glob("*.yml")))
        if not files:
            self._err(f"No YAML instruction files found in '{inst_dir}/'")
            return False

        for f in files:
            inst_id = f.stem.upper()
            with open(f) as fp:
                data = yaml.safe_load(fp) or {}
            self.instructions[inst_id] = Instruction(inst_id, data)
            print(f"  ✓ Loaded: {inst_id:12s} — {self.instructions[inst_id].description}")

        return True

    def connect_github(self) -> bool:
        token = self.config.github_token
        if not token:
            print()
            token = input("  Enter your GitHub Personal Access Token: ").strip()
            if not token:
                self._err("Token is required. Cannot proceed.")
                return False

        try:
            self.g = Github(token, per_page=100)
            user = self.g.get_user()
            print(f"  ✓ Authenticated as: {user.login}")
            return True
        except GithubException as e:
            self._err(f"GitHub authentication failed: {_gh_msg(e)}")
            return False

    # ── Phase 1: Questions ──────────────────────────────────────────────────

    def gather_questions(self) -> List[str]:
        """
        Inspect the loaded config and instruction sets.
        Return a list of human-readable questions that need answers
        before execution can begin safely.
        """
        qs = []

        # Token
        if not self.config.github_token and not os.environ.get("GITHUB_TOKEN"):
            qs.append(
                "Q1 [TOKEN] No GitHub token found in repos_config.yaml or GITHUB_TOKEN env var.\n"
                "      Please add 'github_token: ghp_yourtoken' to repos_config.yaml.\n"
                "      Required scopes: repo (full), workflow (if updating .github/workflows/)"
            )

        # Branch prefix
        if not self.config.branch_prefix:
            qs.append(
                "Q2 [BRANCH PREFIX] 'branch_prefix' is empty in repos_config.yaml.\n"
                "      What should branches be called? (e.g., 'auto-update/' → auto-update/inst-001)\n"
                "      Add 'branch_prefix: auto-update/' to repos_config.yaml."
            )

        # Repos without base_branch
        no_branch = [r["name"] for r in self.config.repositories if "base_branch" not in r]
        if no_branch:
            preview = ", ".join(no_branch[:5]) + (f" ... +{len(no_branch)-5} more" if len(no_branch) > 5 else "")
            qs.append(
                f"Q3 [BASE BRANCH] {len(no_branch)} repo(s) have no 'base_branch' specified "
                f"(will default to 'main'):\n"
                f"      {preview}\n"
                f"      Is 'main' correct for all of these? If not, add 'base_branch: <name>' per repo."
            )

        # Missing instruction files
        referenced = set()
        for r in self.config.repositories:
            for i in r.get("instruction_sets", []):
                referenced.add(i.upper())

        missing = referenced - set(self.instructions.keys())
        if missing:
            qs.append(
                f"Q4 [MISSING INSTRUCTIONS] These instruction set IDs are referenced in\n"
                f"      repos_config.yaml but no matching YAML files exist in '{self.config.instructions_dir}/':\n"
                f"      {', '.join(sorted(missing))}\n"
                f"      Create a YAML file for each one (e.g., instructions/INST-004.yaml)."
            )

        # Repos with no instruction sets
        no_insts = [r["name"] for r in self.config.repositories if not r.get("instruction_sets")]
        if no_insts:
            preview = ", ".join(no_insts[:3]) + (f" ... +{len(no_insts)-3} more" if len(no_insts) > 3 else "")
            qs.append(
                f"Q5 [EMPTY REPOS] {len(no_insts)} repo(s) have no 'instruction_sets' listed "
                f"and will be skipped:\n"
                f"      {preview}\n"
                f"      Intentional? If not, add instruction set IDs to those repo entries."
            )

        # PR reviewers
        if not self.config.pr_reviewers:
            qs.append(
                "Q6 [REVIEWERS] No 'pr_reviewers' are set. PRs will be created with no reviewers.\n"
                "      Add GitHub usernames to 'pr_reviewers' in repos_config.yaml if needed."
            )

        return qs

    # ── Phase 2: Plan ───────────────────────────────────────────────────────

    def build_plan(self) -> str:
        lines = []
        W = 66

        def bar(char="═"): return char * W
        def section(title):
            lines.append("")
            lines.append(f"┌─ {title} {'─' * (W - len(title) - 4)}┐")

        repos = self.config.repositories
        total_repos = len(repos)
        total_ops = sum(len(r.get("instruction_sets", [])) for r in repos)

        lines.append(bar())
        lines.append("  EXECUTION PLAN")
        if self.config.dry_run:
            lines.append("  ⚠️  DRY RUN — No changes will be made to GitHub")
        lines.append(bar())
        lines.append(f"  Repositories        : {total_repos}")
        lines.append(f"  Total operations    : {total_ops}  (repo × instruction combos)")
        lines.append(f"  Branch prefix       : {self.config.branch_prefix}")
        lines.append(f"  Instructions dir    : {self.config.instructions_dir}/")
        lines.append(f"  PR Reviewers        : {', '.join(self.config.pr_reviewers) or 'None'}")

        section("INSTRUCTION SETS")
        for inst_id, inst in self.instructions.items():
            lines.append(f"│  [{inst_id}]")
            lines.append(f"│    Description : {inst.description}")
            lines.append(f"│    PR Title    : {inst.pr_title}")
            if inst.check_exists:
                pat = inst.check_exists.get("pattern", "")
                fil = inst.check_exists.get("in_file", "any file")
                lines.append(f"│    Skip if     : '{pat}' found in {fil}")
            lines.append(f"│    # Changes   : {len(inst.changes)}")
            for i, ch in enumerate(inst.changes, 1):
                ctype = ch.get("type", "?").upper()
                cpath = ch.get("path", ch.get("description", ""))
                lines.append(f"│      {i}. [{ctype}] {cpath}")
        lines.append("└" + "─" * W + "┘")

        section("REPOSITORIES — WHAT WILL HAPPEN")
        shown = min(15, total_repos)
        for idx, r in enumerate(repos[:shown], 1):
            rname = r.get("name", "")
            base = r.get("base_branch", "main")
            inst_ids = [i.upper() for i in r.get("instruction_sets", [])]
            lines.append(f"│  {idx:3d}. {rname}")
            lines.append(f"│       Base branch : {base}")
            for iid in inst_ids:
                bname = f"{self.config.branch_prefix}{iid.lower()}"
                lines.append(f"│       [{iid}] → branch: {bname}")
        if total_repos > shown:
            lines.append(f"│       ... and {total_repos - shown} more repositories")
        lines.append("└" + "─" * W + "┘")

        section("EXECUTION STEPS (per repo × instruction)")
        steps = [
            "Verify repository is accessible on GitHub",
            "Check if update already exists (skip if yes)",
            "Check if PR is already open for this branch (skip if yes)",
            "Check if branch already exists (skip or continue per config)",
            "⏸ PAUSE — ask human: 'Create branch in this repo?'",
            "Create new branch from base branch",
            "Apply code changes as defined in instruction YAML",
            "⏸ PAUSE — ask human: 'Raise PR for this repo?'",
            "Create Pull Request with generated title + body",
            "Add reviewers to PR (if configured)",
            "Move to next repo",
        ]
        for i, s in enumerate(steps, 1):
            lines.append(f"│  {i:2d}. {s}")
        lines.append("└" + "─" * W + "┘")

        lines.append("")
        lines.append(bar())
        lines.append(f"  Maximum PRs that could be created: {total_ops}")
        lines.append(f"  (Actual count will be lower after skip checks)")
        lines.append(bar())

        return "\n".join(lines)

    # ── Phase 3: Execution ──────────────────────────────────────────────────

    def run_all(self):
        total = len(self.config.repositories)
        print(f"\n🚀  Starting — {total} repositories to process\n")

        for idx, repo_data in enumerate(self.config.repositories, 1):
            repo_name = repo_data.get("name", "").strip()
            inst_ids = [i.upper() for i in repo_data.get("instruction_sets", [])]

            print(f"\n{'═' * 66}")
            print(f"  [{idx}/{total}]  {repo_name}")
            print(f"{'═' * 66}")

            if not inst_ids:
                print("  ⏭  No instruction sets assigned — skipping")
                continue

            if not self._confirm(f"Process repository '{repo_name}'?"):
                print("  ↩  Skipped by user")
                continue

            # Load repo
            try:
                repo = self.g.get_repo(repo_name)
            except GithubException as e:
                msg = _gh_msg(e)
                self._err(f"Cannot access repo: {msg}")
                self.results["failed"].append({"repo": repo_name, "error": msg})
                continue

            for inst_id in inst_ids:
                inst = self.instructions.get(inst_id)
                if not inst:
                    print(f"  ⚠️  Instruction '{inst_id}' not found — skipping")
                    continue
                self._process_one(repo, repo_data, inst)

        print(f"\n{'═' * 66}")
        print("  ✅  All repositories processed")
        print(f"{'═' * 66}")

    def _process_one(self, repo, repo_data: dict, inst: Instruction):
        """Process one instruction against one repository."""
        repo_name = repo.full_name
        base_branch = repo_data.get("base_branch", "main")
        branch_name = f"{self.config.branch_prefix}{inst.id.lower()}"

        print(f"\n  ┌─ Instruction: {inst.id}")
        print(f"  │  {inst.description}")
        print(f"  │  Branch     : {branch_name}  ←  {base_branch}")

        # ── Check 1: Update already exists? ──
        already, reason = self._check_exists(repo, inst, base_branch)
        if already:
            print(f"  │  ⏭  SKIP — update already exists: {reason}")
            self._record_skip(repo_name, inst.id, f"Already exists: {reason}")
            return

        # ── Check 2: PR already open? ──
        pr_url = self._find_open_pr(repo, branch_name, base_branch)
        if pr_url:
            print(f"  │  ⏭  SKIP — open PR exists: {pr_url}")
            self._record_skip(repo_name, inst.id, f"PR already open: {pr_url}")
            return

        # ── Check 3: Branch exists? ──
        branch_exists = self._branch_exists(repo, branch_name)
        if branch_exists and inst.skip_if_branch_exists:
            print(f"  │  ⏭  SKIP — branch already exists: {branch_name}")
            self._record_skip(repo_name, inst.id, f"Branch already exists: {branch_name}")
            return

        # ── Confirm: create branch ──
        print(f"  └─────────────────────────────────────────")
        if not self._confirm(
            f"  Create branch '{branch_name}' in [{repo_name}] "
            f"from '{base_branch}' and apply '{inst.id}'?"
        ):
            print("  ↩  Skipped by user")
            return

        # ── Create branch ──
        if not self.config.dry_run:
            try:
                sha = repo.get_branch(base_branch).commit.sha
                if not branch_exists:
                    repo.create_git_ref(f"refs/heads/{branch_name}", sha)
                    print(f"  ✓ Branch created: {branch_name}")
                else:
                    print(f"  ℹ  Branch already exists, using it: {branch_name}")
            except GithubException as e:
                msg = _gh_msg(e)
                self._err(f"Branch creation failed: {msg}")
                self.results["failed"].append(
                    {"repo": repo_name, "instruction": inst.id, "error": msg}
                )
                return
        else:
            print(f"  [DRY RUN] Would create branch: {branch_name}")

        # ── Apply changes ──
        print(f"  🔧 Applying changes...")
        any_change, log_lines = self._apply_changes(repo, inst, branch_name)
        for line in log_lines:
            print(line)

        if not any_change:
            print("  ⏭  No changes applied — skipping PR (branch will be deleted)")
            if not self.config.dry_run:
                try:
                    repo.get_git_ref(f"heads/{branch_name}").delete()
                    print("  🗑  Empty branch deleted")
                except Exception:
                    pass
            self._record_skip(repo_name, inst.id, "No changes applied (patterns not matched)")
            return

        # ── Confirm: raise PR ──
        if not self._confirm(
            f"  Raise Pull Request: '{branch_name}' → '{base_branch}' in [{repo_name}]?"
        ):
            print("  ↩  PR creation skipped (branch kept)")
            return

        # ── Create PR ──
        if not self.config.dry_run:
            try:
                reviewers = repo_data.get("pr_reviewers", self.config.pr_reviewers)
                body = self._pr_body(inst)

                pr = repo.create_pull(
                    title=inst.pr_title,
                    body=body,
                    head=branch_name,
                    base=base_branch,
                )

                if reviewers:
                    try:
                        pr.create_review_request(reviewers=reviewers)
                    except GithubException:
                        print("  ⚠️  Could not add reviewers (check they are collaborators)")

                if inst.labels:
                    try:
                        pr.set_labels(*inst.labels)
                    except GithubException:
                        pass

                print(f"  ✅ PR #{pr.number}: {pr.html_url}")
                self.results["prs_created"].append({
                    "repo": repo_name,
                    "instruction": inst.id,
                    "branch": branch_name,
                    "base_branch": base_branch,
                    "pr_number": pr.number,
                    "pr_url": pr.html_url,
                    "pr_title": pr.title,
                })
            except GithubException as e:
                msg = _gh_msg(e)
                self._err(f"PR creation failed: {msg}")
                self.results["failed"].append(
                    {"repo": repo_name, "instruction": inst.id, "error": msg}
                )
        else:
            print(f"  [DRY RUN] Would create PR: '{inst.pr_title}'")
            self.results["dry_run_actions"].append({
                "repo": repo_name, "instruction": inst.id, "branch": branch_name
            })

    # ── Change Applicators ──────────────────────────────────────────────────

    def _apply_changes(
        self, repo, inst: Instruction, branch: str
    ) -> Tuple[bool, List[str]]:
        """Run all changes in the instruction set. Return (any_change, log_lines)."""
        log = []
        any_change = False

        for ch in inst.changes:
            ctype = ch.get("type", "")
            try:
                if ctype == "file_create":
                    ok, msg = self._do_file_create(repo, ch, branch)
                elif ctype == "file_update":
                    ok, msg = self._do_file_update(repo, ch, branch)
                elif ctype == "file_insert":
                    ok, msg = self._do_file_insert(repo, ch, branch)
                elif ctype == "file_append":
                    ok, msg = self._do_file_append(repo, ch, branch)
                elif ctype == "file_delete":
                    ok, msg = self._do_file_delete(repo, ch, branch)
                else:
                    ok, msg = False, f"Unknown change type '{ctype}'"

                icon = "✓" if ok else "~"
                log.append(f"     {icon} [{ctype.upper()}] {msg}")
                if ok:
                    any_change = True

            except GithubException as e:
                log.append(f"     ✗ [{ctype.upper()}] GitHub error: {_gh_msg(e)}")
            except Exception as e:
                log.append(f"     ✗ [{ctype.upper()}] Error: {e}")

        return any_change, log

    def _get_file(self, repo, path: str, branch: str):
        """Return (ContentFile, decoded_str) or (None, None)."""
        try:
            cf = repo.get_contents(path, ref=branch)
            return cf, cf.decoded_content.decode("utf-8")
        except GithubException:
            return None, None

    def _do_file_create(self, repo, ch: dict, branch: str) -> Tuple[bool, str]:
        path = ch["path"]
        content = ch.get("content", "")
        msg = ch.get("commit_message", f"chore: add {path}")

        cf, _ = self._get_file(repo, path, branch)
        if cf:
            return False, f"{path} — already exists, skipped"

        if not self.config.dry_run:
            repo.create_file(path=path, message=msg, content=content, branch=branch)
        return True, f"{path} — created"

    def _do_file_update(self, repo, ch: dict, branch: str) -> Tuple[bool, str]:
        path = ch["path"]
        replacements = ch.get("replacements", [])
        msg = ch.get("commit_message", f"chore: update {path}")

        cf, current = self._get_file(repo, path, branch)
        if cf is None:
            return False, f"{path} — file not found"

        updated = current
        for rep in replacements:
            search = rep.get("search", "")
            replace = rep.get("replace", "")
            if rep.get("regex", False):
                updated = re.sub(search, replace, updated)
            else:
                updated = updated.replace(search, replace)

        if updated == current:
            return False, f"{path} — no match found, no changes needed"

        if not self.config.dry_run:
            repo.update_file(path=path, message=msg, content=updated,
                             sha=cf.sha, branch=branch)
        return True, f"{path} — updated ({len(replacements)} replacement(s))"

    def _do_file_insert(self, repo, ch: dict, branch: str) -> Tuple[bool, str]:
        path = ch["path"]
        to_insert = ch.get("content", "")
        after = ch.get("insert_after", "")
        before = ch.get("insert_before", "")
        skip_if = ch.get("skip_if_present", to_insert)
        msg = ch.get("commit_message", f"chore: update {path}")

        cf, current = self._get_file(repo, path, branch)
        if cf is None:
            return False, f"{path} — file not found"

        if skip_if and skip_if in current:
            return False, f"{path} — content already present, skipped"

        if after and after in current:
            updated = current.replace(after, after + "\n" + to_insert, 1)
        elif before and before in current:
            updated = current.replace(before, to_insert + "\n" + before, 1)
        else:
            updated = current.rstrip("\n") + "\n" + to_insert

        if not self.config.dry_run:
            repo.update_file(path=path, message=msg, content=updated,
                             sha=cf.sha, branch=branch)
        return True, f"{path} — content inserted"

    def _do_file_append(self, repo, ch: dict, branch: str) -> Tuple[bool, str]:
        path = ch["path"]
        to_append = ch.get("content", "")
        skip_if = ch.get("skip_if_present", to_append)
        msg = ch.get("commit_message", f"chore: update {path}")

        cf, current = self._get_file(repo, path, branch)
        if cf is None:
            return False, f"{path} — file not found"

        if skip_if and skip_if in current:
            return False, f"{path} — content already present, skipped"

        updated = current.rstrip("\n") + "\n\n" + to_append.strip() + "\n"

        if not self.config.dry_run:
            repo.update_file(path=path, message=msg, content=updated,
                             sha=cf.sha, branch=branch)
        return True, f"{path} — content appended"

    def _do_file_delete(self, repo, ch: dict, branch: str) -> Tuple[bool, str]:
        path = ch["path"]
        msg = ch.get("commit_message", f"chore: remove {path}")

        cf, _ = self._get_file(repo, path, branch)
        if cf is None:
            return False, f"{path} — file not found (already deleted?)"

        if not self.config.dry_run:
            repo.delete_file(path=path, message=msg, sha=cf.sha, branch=branch)
        return True, f"{path} — deleted"

    # ── Existence Checks ────────────────────────────────────────────────────

    def _check_exists(self, repo, inst: Instruction, base_branch: str) -> Tuple[bool, str]:
        check = inst.check_exists
        if not check:
            return False, ""

        pattern = check.get("pattern", "")
        in_file = check.get("in_file", "")

        if not pattern:
            return False, ""

        if in_file:
            _, content = self._get_file(repo, in_file, base_branch)
            if content is None:
                return False, ""
            if pattern in content:
                return True, f"'{pattern}' found in {in_file}"
        else:
            # Search across repo (uses GitHub code search — rate-limited)
            try:
                query = f"{pattern} repo:{repo.full_name}"
                results = self.g.search_code(query)
                if results.totalCount > 0:
                    return True, f"'{pattern}' found in repo"
            except GithubException:
                pass

        return False, ""

    def _branch_exists(self, repo, branch_name: str) -> bool:
        try:
            repo.get_branch(branch_name)
            return True
        except GithubException:
            return False

    def _find_open_pr(self, repo, head: str, base: str) -> Optional[str]:
        try:
            pulls = repo.get_pulls(
                state="open",
                head=f"{repo.owner.login}:{head}",
                base=base
            )
            for pr in pulls:
                return pr.html_url
        except GithubException:
            pass
        return None

    # ── PR Body ─────────────────────────────────────────────────────────────

    def _pr_body(self, inst: Instruction) -> str:
        extras = f"\n{inst.pr_description}" if inst.pr_description else ""
        return f"""## {inst.description}
{extras}

### Changes Applied

| # | Type | File / Description |
|---|------|--------------------|
""" + "\n".join(
            f"| {i} | `{ch.get('type','').upper()}` | `{ch.get('path', ch.get('description', ''))}` |"
            for i, ch in enumerate(inst.changes, 1)
        ) + f"""

### Review Checklist
- [ ] Code review completed
- [ ] Tests pass (if applicable)
- [ ] No breaking changes introduced

---
*Auto-generated by Bulk Repository Updater — instruction set `{inst.id}`*
"""

    # ── Phase 4: Summary ────────────────────────────────────────────────────

    def print_summary(self):
        prs = self.results["prs_created"]
        skipped = self.results["skipped"]
        failed = self.results["failed"]
        dry = self.results["dry_run_actions"]

        print(f"\n{'╔' + '═' * 64 + '╗'}")
        print("║  EXECUTION SUMMARY" + " " * 45 + "║")
        print(f"{'╚' + '═' * 64 + '╝'}")
        print(f"\n  ✅  PRs Created   : {len(prs)}")
        print(f"  ⏭   Skipped       : {len(skipped)}")
        print(f"  ✗   Failed        : {len(failed)}")
        if dry:
            print(f"  🔍  Dry Run       : {len(dry)}")

        if prs:
            print(f"\n{'─' * 66}")
            print("  PULL REQUESTS RAISED:")
            print(f"{'─' * 66}")
            for i, pr in enumerate(prs, 1):
                print(f"\n  {i:3d}. {pr['repo']}")
                print(f"        Instruction : {pr['instruction']}")
                print(f"        Branch      : {pr['branch']} → {pr['base_branch']}")
                print(f"        PR #{pr['pr_number']:5d}   : {pr['pr_url']}")

        if failed:
            print(f"\n{'─' * 66}")
            print("  FAILURES:")
            print(f"{'─' * 66}")
            for f in failed:
                inst_label = f" [{f.get('instruction','')}]" if f.get("instruction") else ""
                print(f"  ✗  {f['repo']}{inst_label}")
                print(f"     {f.get('error','Unknown error')}")

        self._save_reports()

    def _save_reports(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path("outputs")
        out.mkdir(exist_ok=True)

        # Full JSON
        json_path = out / f"results_{ts}.json"
        with open(json_path, "w") as f:
            json.dump(self.results, f, indent=2)

        # Markdown PR list
        md_path = out / f"pr_list_{ts}.md"
        prs = self.results["prs_created"]
        with open(md_path, "w") as f:
            f.write(f"# Pull Requests Raised\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n")
            f.write(f"**Total PRs:** {len(prs)}\n\n")

            if prs:
                f.write("| # | Repository | Instruction | Branch | PR |\n")
                f.write("|---|-----------|-------------|--------|----|  \n")
                for i, pr in enumerate(prs, 1):
                    f.write(
                        f"| {i} | {pr['repo']} | `{pr['instruction']}` "
                        f"| `{pr['branch']}` | [#{pr['pr_number']}]({pr['pr_url']}) |\n"
                    )
            else:
                f.write("_No PRs were created in this run._\n")

            skipped = self.results["skipped"]
            if skipped:
                f.write(f"\n## Skipped ({len(skipped)})\n\n")
                for s in skipped:
                    f.write(f"- `{s['repo']}` / `{s['instruction']}` — {s['reason']}\n")

            failed = self.results["failed"]
            if failed:
                f.write(f"\n## Failed ({len(failed)})\n\n")
                for fl in failed:
                    f.write(f"- `{fl['repo']}` — {fl.get('error', 'Unknown error')}\n")

        print(f"\n  📄 Full results : {json_path}")
        print(f"  📄 PR list      : {md_path}")

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _confirm(self, prompt: str) -> bool:
        if self.skip_confirm:
            print(f"\n  {prompt}")
            print("  [AUTO-CONFIRM: yes]")
            return True
        while True:
            answer = input(f"\n  {prompt}\n  → [yes / no / quit]: ").strip().lower()
            if answer in ("yes", "y"):
                return True
            elif answer in ("no", "n", "skip", "s"):
                return False
            elif answer in ("quit", "q", "exit", "stop"):
                print("\n  ⛔  Stopped by user.")
                self.print_summary()
                sys.exit(0)
            else:
                print("  Please enter  yes  /  no  /  quit")

    def _record_skip(self, repo: str, inst_id: str, reason: str):
        self.results["skipped"].append(
            {"repo": repo, "instruction": inst_id, "reason": reason}
        )

    @staticmethod
    def _err(msg: str):
        print(f"\n  ✗  ERROR: {msg}")

    @staticmethod
    def _info(msg: str):
        print(f"  ℹ  {msg}")

    # ── Entry Point ─────────────────────────────────────────────────────────

    def run(self):
        print(self.BANNER)

        # ── Load ──
        print("\n📂  Loading configuration...")
        if not self.load_config():
            sys.exit(1)

        print(f"\n📋  Loading instruction sets from '{self.config.instructions_dir}/'...")
        if not self.load_instructions():
            sys.exit(1)

        print("\n🔐  Connecting to GitHub...")
        if not self.connect_github():
            sys.exit(1)

        # ────────────────────────────────────────────────────────────────────
        # PHASE 1 — Questions
        # ────────────────────────────────────────────────────────────────────
        print(f"\n{'─' * 66}")
        print("  PHASE 1 — CLARIFYING QUESTIONS")
        print(f"{'─' * 66}")

        questions = self.gather_questions()
        if questions:
            print(
                "\n  ⚠️  The agent has questions before it can proceed safely.\n"
                "  Please read each one and update your config/instruction files,\n"
                "  then confirm you are ready.\n"
            )
            for q in questions:
                print(f"  {'─' * 60}")
                print(f"  {q}")
            print(f"  {'─' * 60}\n")

            if not self._confirm("Have you reviewed and addressed all questions above?"):
                print("\n  Please fix the issues and run again.")
                sys.exit(0)
        else:
            print("\n  ✓  No questions — configuration looks complete.")

        # ────────────────────────────────────────────────────────────────────
        # PHASE 2 — Plan
        # ────────────────────────────────────────────────────────────────────
        print(f"\n{'─' * 66}")
        print("  PHASE 2 — EXECUTION PLAN")
        print(f"{'─' * 66}")

        plan = self.build_plan()
        print("\n" + plan)

        if not self._confirm(
            "Does the plan look correct? Shall we proceed with execution?"
        ):
            print("\n  Cancelled. Update your config and try again.")
            sys.exit(0)

        # ────────────────────────────────────────────────────────────────────
        # PHASE 3 — Execute
        # ────────────────────────────────────────────────────────────────────
        print(f"\n{'─' * 66}")
        print("  PHASE 3 — EXECUTION")
        print(f"{'─' * 66}")

        self.run_all()

        # ────────────────────────────────────────────────────────────────────
        # PHASE 4 — Summary
        # ────────────────────────────────────────────────────────────────────
        print(f"\n{'─' * 66}")
        print("  PHASE 4 — SUMMARY & REPORTS")
        print(f"{'─' * 66}")

        self.print_summary()


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _gh_msg(e: GithubException) -> str:
    """Extract a readable message from a GithubException."""
    if hasattr(e, "data") and isinstance(e.data, dict):
        return e.data.get("message", str(e))
    return str(e)


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Bulk Repository Updater — GitHub Copilot Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="repos_config.yaml",
        help="Path to repos_config.yaml (default: repos_config.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate execution — no branches, commits, or PRs will be created",
    )
    parser.add_argument(
        "--skip-confirm", action="store_true",
        help="Auto-approve all confirmation prompts (use with caution!)",
    )
    args = parser.parse_args()

    agent = BulkRepoUpdater(
        config_file=args.config,
        dry_run_override=args.dry_run,
        skip_confirm=args.skip_confirm,
    )
    agent.run()


if __name__ == "__main__":
    main()
