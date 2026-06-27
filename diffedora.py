#!/usr/bin/env python3
"""
diffedora — show package diffs between Fedora Silverblue releases
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

COMPOSE_REPO = "https://kojipkgs.fedoraproject.org/compose/ostree/repo/"

_SUMMARY_PROMPT = """\
You are summarizing a Fedora Silverblue OS update for end users.
Write a single short sentence (under 15 words) summarizing the theme of these changes.
Focus on notable packages like the kernel, GNOME components, Firefox, systemd, etc.
If there are security updates, mention that. Be concise and plain — no markdown, no leading label.\
"""


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


def _get_bodhi_update(name, new_evr):
    nvr = f"{name}-{_strip_epoch(new_evr)}"
    url = f"https://bodhi.fedoraproject.org/updates/?builds={nvr}&rows_per_page=1"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        updates = data.get("updates", [])
        if not updates:
            return False, None
        update = updates[0]
        notes = update.get("notes", "").strip() or None
        return update.get("type") == "security", notes
    except Exception:
        return False, None


def _get_koji_changelog(name, new_evr):
    nvr = f"{name}-{_strip_epoch(new_evr)}"
    try:
        proxy = xmlrpc.client.ServerProxy("https://koji.fedoraproject.org/kojihub")
        entries = proxy.getChangelogEntries(build=nvr)
        return entries[0]["text"].strip() if entries else None
    except Exception:
        return None


def load_toc(cache_dir):
    p = Path(cache_dir) / "summary.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_toc(cache_dir, toc):
    (Path(cache_dir) / "summary.json").write_text(json.dumps(toc, indent=2))


def load_release(cache_dir, release_id):
    p = Path(cache_dir) / "releases" / f"{release_id}.json"
    return json.loads(p.read_text()) if p.exists() else None


def save_release(cache_dir, release):
    d = Path(cache_dir) / "releases"
    d.mkdir(exist_ok=True)
    (d / f"{release['id']}.json").write_text(json.dumps(release, indent=2))


def build_release(old_ver, new_ver, diff, security, notes, summary):
    changes = []
    for pkg in diff["upgraded"]:
        parts = pkg.split(" -> ", 1)
        if len(parts) == 2:
            old_parts = parts[0].rsplit(" ", 1)
            name = old_parts[0] if len(old_parts) == 2 else parts[0]
            from_evr = old_parts[1] if len(old_parts) == 2 else ""
            changes.append({
                "type": "upgrade",
                "package": name,
                "url": f"https://packages.fedoraproject.org/pkgs/{name}/",
                "from": from_evr,
                "to": parts[1],
                "security": name in security,
                "note": notes.get(name) if notes else None,
            })
    for pkg in diff["added"]:
        name, _, ver = pkg.rpartition(" ")
        changes.append({
            "type": "added",
            "package": name,
            "url": f"https://packages.fedoraproject.org/pkgs/{name}/",
            "from": None,
            "to": ver,
        })
    for pkg in diff["removed"]:
        name, _, ver = pkg.rpartition(" ")
        changes.append({
            "type": "removed",
            "package": name,
            "url": f"https://packages.fedoraproject.org/pkgs/{name}/",
            "from": ver,
            "to": None,
        })
    release_id = f"{old_ver}-{new_ver}"
    return {
        "id": release_id,
        "old_version": old_ver,
        "new_version": new_ver,
        "summary": summary,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    }


def toc_entry(release):
    return {
        "id": release["id"],
        "old_version": release["old_version"],
        "new_version": release["new_version"],
        "summary": release["summary"],
        "total_changes": len(release["changes"]),
        "critical": any(c.get("security") for c in release["changes"]),
        "fetched_at": release["fetched_at"],
    }


def release_to_diff(release):
    diff = {"upgraded": [], "added": [], "removed": []}
    for c in release["changes"]:
        if c["type"] == "upgrade":
            diff["upgraded"].append(f"{c['package']} {c['from']} -> {c['to']}")
        elif c["type"] == "added":
            diff["added"].append(f"{c['package']} {c['to']}")
        elif c["type"] == "removed":
            diff["removed"].append(f"{c['package']} {c['from']}")
    return diff


def release_to_security(release):
    return {c["package"] for c in release["changes"] if c.get("security")}


def release_to_notes(release):
    return {c["package"]: c["note"] for c in release["changes"] if c.get("note")}


def summarize_release(diff, security, notes, api_key):
    if not api_key:
        return None
    lines = []
    for pkg in diff.get("upgraded", []):
        parts = pkg.split(" -> ", 1)
        name = parts[0].rsplit(" ", 1)[0] if len(parts) == 2 else pkg.split()[0]
        sec = "[security] " if name in security else ""
        lines.append(f"  - {sec}upgraded: {pkg}")
        if notes and name in notes:
            lines.append(f"    note: {notes[name]}")
    for pkg in diff.get("added", []):
        lines.append(f"  - added: {pkg}")
    for pkg in diff.get("removed", []):
        lines.append(f"  - removed: {pkg}")
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 80,
        "system": _SUMMARY_PROMPT,
        "messages": [{"role": "user", "content": "\n".join(lines)}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)["content"][0]["text"].strip()
    except Exception as e:
        print(f"warning: summary generation failed: {e}", file=sys.stderr)
        return None


def _upgraded_candidates(diff):
    for pkg in diff["upgraded"]:
        parts = pkg.split(" -> ", 1)
        if len(parts) == 2:
            old_parts = parts[0].rsplit(" ", 1)
            if len(old_parts) == 2:
                yield old_parts[0], parts[1]


def get_security_packages(diff):
    security = set()
    bodhi_notes = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_get_bodhi_update, name, evr): name
                   for name, evr in _upgraded_candidates(diff)}
        for f in as_completed(futures):
            is_sec, notes = f.result()
            name = futures[f]
            if is_sec:
                security.add(name)
            if notes:
                bodhi_notes[name] = notes
    return security, bodhi_notes


def get_changelogs(diff):
    changelogs = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_get_koji_changelog, name, evr): name
                   for name, evr in _upgraded_candidates(diff)}
        for f in as_completed(futures):
            result = f.result()
            if result:
                changelogs[futures[f]] = result
    return changelogs


def format_markdown(old_ver, new_ver, diff, security=frozenset(), summary=None, notes=None):
    total = sum(len(v) for v in diff.values())
    label = "change" if total == 1 else "changes"
    lines = [f"## {old_ver} → {new_ver} ({total} {label})"]
    if summary:
        lines.append(summary)
    lines.append("")

    if not any(diff.values()):
        lines.append("*No package changes.*\n")
        return "\n".join(lines)

    def link(name):
        return f"[**{name}**](https://packages.fedoraproject.org/pkgs/{name}/)"

    def append_note(name):
        if notes and name in notes:
            for note_line in notes[name].splitlines():
                if note_line.strip():
                    lines.append(f"  {note_line.strip()}")

    for pkg in diff["upgraded"]:
        parts = pkg.split(" -> ", 1)
        if len(parts) == 2:
            old_parts = parts[0].rsplit(" ", 1)
            name = old_parts[0] if len(old_parts) == 2 else parts[0]
            old_evr = old_parts[1] if len(old_parts) == 2 else ""
            sec = "[!] " if name in security else ""
            lines.append(f"- {sec}{link(name)} ({old_evr} → {parts[1]})")
            append_note(name)

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
    parser.add_argument("--changelogs", action="store_true",
                        help="show Bodhi notes and Koji changelogs per package")
    parser.add_argument("--cache-dir", metavar="PATH",
                        help="directory for persistent summary cache")
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

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        cache_dir = args.cache_dir
        toc = load_toc(cache_dir) if cache_dir else {}

        for i in range(actual):
            new_c = commits[i]
            old_c = commits[i + 1]
            toc_key = f"{old_c['version']}→{new_c['version']}"
            release_id = f"{old_c['version']}-{new_c['version']}"

            release = load_release(cache_dir, release_id) if cache_dir else None

            if release:
                if not release.get("summary") and api_key:
                    release["summary"] = summarize_release(
                        release_to_diff(release), release_to_security(release),
                        release_to_notes(release), api_key)
                    if release["summary"] and cache_dir:
                        save_release(cache_dir, release)
                        toc[toc_key] = toc_entry(release)
                        save_toc(cache_dir, toc)
            else:
                print(f"Diffing {old_c['version']} → {new_c['version']}...", file=sys.stderr)
                diff = diff_commits(repo_dir, old_c["hash"], new_c["hash"])
                security, bodhi_notes = get_security_packages(diff)
                all_notes = {**get_changelogs(diff), **bodhi_notes}
                summary = summarize_release(diff, security, all_notes, api_key)
                release = build_release(old_c["version"], new_c["version"], diff, security, all_notes, summary)
                if cache_dir:
                    save_release(cache_dir, release)
                    toc[toc_key] = toc_entry(release)
                    save_toc(cache_dir, toc)

            # Render phase: flags only affect output, not what was fetched/cached
            print(format_markdown(
                old_c["version"], new_c["version"],
                release_to_diff(release),
                release_to_security(release) if not args.no_security else set(),
                release.get("summary"),
                release_to_notes(release) if args.changelogs else None,
            ))


if __name__ == "__main__":
    main()
