#!/usr/bin/env python3
"""
diffedora — show package diffs between Fedora Silverblue releases
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

COMPOSE_REPO = "https://kojipkgs.fedoraproject.org/compose/ostree/repo/"


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def setup_repo(repo_dir):
    run(["ostree", "init", "--repo", repo_dir, "--mode=archive"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(["ostree", "remote", "add", "--no-gpg-verify", "compose", COMPOSE_REPO,
         "--repo", repo_dir],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def resolve_ref(version, arch, variant):
    url = f"{COMPOSE_REPO}refs/heads/fedora/{version}/{arch}/{variant}"
    try:
        with urllib.request.urlopen(url) as r:
            return r.read().decode().strip()
    except Exception as e:
        sys.exit(f"error: could not resolve ref at {url}: {e}")


def pull_history(repo_dir, commit, depth):
    result = subprocess.run(
        ["ostree", "pull", "--commit-metadata-only", f"--depth={depth}",
         "compose", commit, "--repo", repo_dir],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"error: ostree pull failed:\n{result.stderr}")


def get_commits(repo_dir, head_commit, n):
    result = subprocess.run(
        ["ostree", "log", head_commit, "--repo", repo_dir],
        capture_output=True, text=True, check=True
    )
    commits = []
    current_hash = None
    for line in result.stdout.splitlines():
        m = re.match(r'^commit ([0-9a-f]{64})$', line.strip())
        if m:
            current_hash = m.group(1)
        elif current_hash and line.strip().startswith("Version:"):
            version = line.split(":", 1)[1].strip()
            commits.append({"hash": current_hash, "version": version})
            current_hash = None
    return commits[:n]


def diff_commits(repo_dir, old_hash, new_hash):
    result = subprocess.run(
        ["rpm-ostree", "db", "diff", f"--repo={repo_dir}", old_hash, new_hash],
        capture_output=True, text=True
    )
    return parse_diff(result.stdout)


def parse_diff(output):
    sections = {"upgraded": [], "added": [], "removed": []}
    current = None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Upgraded:":
            current = "upgraded"
        elif stripped == "Added:":
            current = "added"
        elif stripped == "Removed:":
            current = "removed"
        elif current and line.startswith("  "):
            sections[current].append(stripped)
    return sections


def _strip_epoch(evr):
    return evr.split(":", 1)[1] if ":" in evr else evr


def _is_security(name, new_evr):
    nvr = f"{name}-{_strip_epoch(new_evr)}"
    url = f"https://bodhi.fedoraproject.org/updates/?builds={nvr}&rows_per_page=1"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        updates = data.get("updates", [])
        return bool(updates) and updates[0].get("type") == "security"
    except Exception:
        return False


def get_security_packages(diff):
    candidates = []
    for pkg in diff["upgraded"]:
        parts = pkg.split(" -> ", 1)
        if len(parts) == 2:
            old_parts = parts[0].rsplit(" ", 1)
            if len(old_parts) == 2:
                candidates.append((old_parts[0], parts[1]))
    security = set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_is_security, name, evr): name for name, evr in candidates}
        for f in as_completed(futures):
            if f.result():
                security.add(futures[f])
    return security


def format_markdown(old_ver, new_ver, diff, security=frozenset()):
    total = sum(len(v) for v in diff.values())
    label = "change" if total == 1 else "changes"
    lines = [f"## {old_ver} → {new_ver} ({total} {label})\n"]

    if not any(diff.values()):
        lines.append("*No package changes.*\n")
        return "\n".join(lines)

    def link(name):
        return f"[**{name}**](https://packages.fedoraproject.org/pkgs/{name}/)"

    for pkg in diff["upgraded"]:
        parts = pkg.split(" -> ", 1)
        if len(parts) == 2:
            old_parts = parts[0].rsplit(" ", 1)
            name = old_parts[0] if len(old_parts) == 2 else parts[0]
            old_evr = old_parts[1] if len(old_parts) == 2 else ""
            sec = "[!] " if name in security else ""
            lines.append(f"- {sec}{link(name)} ({old_evr} → {parts[1]})")

    for pkg in diff["added"]:
        name, _, evr = pkg.rpartition(" ")
        lines.append(f"- [New!] {link(name)} ({evr})")

    for pkg in diff["removed"]:
        name = pkg.rsplit(" ", 1)[0]
        lines.append(f"- [Removed] {link(name)}")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Diff Fedora Silverblue releases",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--releases", type=int, default=20, metavar="N",
                        help="number of release pairs to show")
    parser.add_argument("--version", default="44", help="Fedora version")
    parser.add_argument("--arch", default="x86_64", help="architecture")
    parser.add_argument("--variant", default="silverblue", help="OS variant")
    parser.add_argument("--no-security", action="store_true",
                        help="skip Bodhi security annotations")
    args = parser.parse_args()

    n = args.releases

    print(f"Resolving {args.variant} {args.version}/{args.arch}...", file=sys.stderr)
    head_commit = resolve_ref(args.version, args.arch, args.variant)
    print(f"HEAD: {head_commit[:12]}...", file=sys.stderr)

    with tempfile.TemporaryDirectory() as repo_dir:
        setup_repo(repo_dir)

        print(f"Fetching {n + 2} commits of history...", file=sys.stderr)
        pull_history(repo_dir, head_commit, n + 2)

        commits = get_commits(repo_dir, head_commit, n + 1)
        if len(commits) < 2:
            sys.exit("error: fewer than 2 commits found in history")

        actual = min(n, len(commits) - 1)
        variant_label = args.variant.capitalize()
        print(f"\n# Fedora {variant_label} {args.arch} — Last {actual} Releases\n")

        for i in range(actual):
            new_c = commits[i]
            old_c = commits[i + 1]
            print(f"Diffing {old_c['version']} → {new_c['version']}...", file=sys.stderr)
            diff = diff_commits(repo_dir, old_c["hash"], new_c["hash"])
            security = set() if args.no_security else get_security_packages(diff)
            print(format_markdown(old_c["version"], new_c["version"], diff, security))


if __name__ == "__main__":
    main()
