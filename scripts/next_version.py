#!/usr/bin/env python3
"""Choose the next Chatbox release from commits since the latest tag.

The current release is the highest vMAJOR.MINOR.PATCH tag. Commits after that
tag pick the bump:

- BREAKING in the message, or !: in the subject (feat!:) -> major
- a subject that is feat:, starts with Add, or says "new feature" -> minor
- every other subject -> patch

The highest bump wins. A commit that only touches markdown, docs/, or
.github/ does not count. The version-bump commit itself ([skip ci], or
authored by github-actions[bot]) does not count. When nothing counts, there
is no release.

Prints the next MAJOR.MINOR.PATCH, or nothing when the push should not release.
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_FEAT = re.compile(r"feat(\([^)]*\))?:", re.IGNORECASE)
_NEW_FEATURE = re.compile(r"new feature", re.IGNORECASE)
_SKIP_CI = re.compile(r"\[(skip ci|ci skip|no ci|skip actions|actions skip)\]", re.IGNORECASE)
_RANK = {"major": 3, "minor": 2, "patch": 1}


def parse_version(text):
    """(major, minor, patch) from '1.2.3' or 'v1.2.3'."""
    raw = str(text or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    match = _VERSION.fullmatch(raw)
    if not match:
        raise ValueError("not a release version: %s" % text)
    return tuple(int(part) for part in match.groups())


def select_latest_tag(names):
    """Highest vMAJOR.MINOR.PATCH tag. Numeric order, so v1.0.10 beats v1.0.9."""
    found = []
    for name in names:
        text = str(name).strip()
        match = _TAG.fullmatch(text)
        if match:
            found.append((tuple(int(part) for part in match.groups()), text))
    if not found:
        return None
    found.sort()
    return found[-1][1]


def is_bump_commit(subject, body="", author_name="", author_email=""):
    """The commit this workflow makes when it tags a release. It must not tag again."""
    if _SKIP_CI.search("%s\n%s" % (subject or "", body or "")):
        return True
    name = (author_name or "").strip().lower()
    email = (author_email or "").strip().lower()
    if name == "github-actions[bot]":
        return True
    if "github-actions[bot]" in email or email.startswith("41898282+"):
        return True
    return False


def is_skippable_path(path):
    """Markdown, docs, and workflow files do not by themselves deserve a release."""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return False
    if text == "docs" or text.startswith("docs/"):
        return True
    if text == ".github" or text.startswith(".github/"):
        return True
    return text.rsplit("/", 1)[-1].endswith(".md")


def message_subject(subject, body):
    """Pull request title. A merge commit keeps that title on the first body line."""
    first = ""
    for line in (subject or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if first.startswith("Merge pull request ") or first.startswith("Merge branch "):
        for line in (body or "").splitlines():
            if line.strip():
                return line.strip()
    return first


def classify_message(subject, body=""):
    """'major', 'minor', or 'patch' from a commit or pull request title."""
    title = message_subject(subject, body)
    if "BREAKING" in (subject or "") or "BREAKING" in (body or "") or "!:" in title:
        return "major"
    if _FEAT.match(title):
        return "minor"
    if re.match(r"Add\b", title):
        return "minor"
    if _NEW_FEATURE.search(title):
        return "minor"
    return "patch"


def next_version(current, commits):
    """Next MAJOR.MINOR.PATCH, or None when the commits should not release."""
    counted = []
    for commit in commits:
        if is_bump_commit(commit.get("subject", ""), commit.get("body", ""),
                          commit.get("author_name", ""), commit.get("author_email", "")):
            continue
        paths = commit.get("paths") or []
        if any(not is_skippable_path(path) for path in paths):
            counted.append(commit)
    if not counted:
        return None
    kind = "patch"
    for commit in counted:
        found = classify_message(commit.get("subject", ""), commit.get("body", ""))
        if _RANK[found] > _RANK[kind]:
            kind = found
    major, minor, patch = parse_version(current)
    if kind == "major":
        return "%d.0.0" % (major + 1)
    if kind == "minor":
        return "%d.%d.0" % (major, minor + 1)
    return "%d.%d.%d" % (major, minor, patch + 1)


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)
    return proc.stdout


def list_tags(repo):
    return [line.strip() for line in _git(repo, "tag", "-l").splitlines() if line.strip()]


def commits_since(repo, tag):
    """Commits reachable from HEAD and not from tag, oldest first."""
    rev = "%s..HEAD" % tag if tag else "HEAD"
    shas = [line.strip() for line in _git(repo, "rev-list", "--reverse", rev).splitlines() if line.strip()]
    commits = []
    for sha in shas:
        parts = _git(repo, "show", "-s", "--format=%an%x1f%ae%x1f%s%x1f%b", sha).split("\x1f", 3)
        while len(parts) < 4:
            parts.append("")
        name, email, subject, body = [part.strip() for part in parts[:4]]
        parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()
        if len(parents) > 1:
            listed = _git(repo, "diff", "--name-only", parents[1], sha)
        else:
            listed = _git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha)
        paths = [line.strip() for line in listed.splitlines() if line.strip()]
        commits.append({"subject": subject, "body": body, "author_name": name,
                        "author_email": email, "paths": paths})
    return commits


def plan(repo):
    """Next version for this checkout, or None."""
    tag = select_latest_tag(list_tags(repo))
    current = tag[1:] if tag else "0.0.0"
    return next_version(current, commits_since(repo, tag))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print the next Chatbox release version.")
    parser.add_argument("--repo", default=ROOT)
    args = parser.parse_args(argv)
    version = plan(args.repo)
    if version:
        print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
