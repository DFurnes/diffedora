#!/usr/bin/env python3
"""
diffedora — show package diffs between Fedora Silverblue releases
"""

import argparse
import contextlib
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
COREOS_BUILDS_BASE = "https://builds.coreos.fedoraproject.org/prod/streams"

VARIANTS = {
    "silverblue": {"type": "ostree", "label": "Silverblue", "ref": "fedora/{version}/{arch}/silverblue"},
    "coreos":     {"type": "coreos", "label": "CoreOS",     "stream": "stable"},
}

# ANSI escape codes
_R  = "\033[0m"   # reset
_B  = "\033[1m"   # bold
_D  = "\033[2m"   # dim
_I  = "\033[3m"   # italic
_RD = "\033[31m"  # red
_GN = "\033[32m"  # green
_CY = "\033[36m"  # cyan

_SUMMARY_PROMPT = """\
You are summarizing a Fedora OS update for end users.
Write a single short sentence (under 20 words) summarizing the theme of these changes.
Name specific packages — prefer "curl, glibc, and bind" over "core libraries" or "library updates".
If there are security updates, name the affected packages specifically.
Avoid vague filler like "important fixes", "various improvements", "and more", or "across the system".
Be concise and plain — no markdown, no leading label.
Start directly with the main topic. Never begin with "This update", "This Fedora update", or similar preambles.
Good: "Kernel 7.0.13, curl, and bind security fixes with GNOME Control Center update."
Bad: "Kernel and library security updates with important bug fixes."\
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
    ref = VARIANTS[variant]["ref"].format(version=version, arch=arch)
    url = f"{COMPOSE_REPO}refs/heads/{ref}"
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


def get_coreos_builds(n, arch, stream="stable"):
    url = f"{COREOS_BUILDS_BASE}/{stream}/builds/builds.json"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        sys.exit(f"error: could not fetch CoreOS build list: {e}")
    builds = [b for b in data["builds"] if arch in b.get("arches", [])]
    if len(builds) < 2:
        sys.exit(f"error: fewer than 2 CoreOS {stream} builds found for arch {arch}")
    return builds[:n]


def diff_coreos(new_ver, arch, stream="stable"):
    url = f"{COREOS_BUILDS_BASE}/{stream}/builds/{new_ver}/{arch}/meta.json"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            meta = json.load(r)
    except Exception as e:
        sys.exit(f"error: could not fetch CoreOS metadata for {new_ver}: {e}")
    diff = {"upgraded": [], "added": [], "removed": []}
    for entry in meta.get("pkgdiff", []):
        name, change_type, details = entry[0], entry[1], entry[2]
        if change_type == 2:  # upgrade
            prev_ver = details["PreviousPackage"][1]
            new_pkg_ver = details["NewPackage"][1]
            diff["upgraded"].append(f"{name} {prev_ver} -> {new_pkg_ver}")
        elif change_type == 0:  # addition
            new_pkg_ver = details["NewPackage"][1]
            diff["added"].append(f"{name} {new_pkg_ver}")
        elif change_type == 1:  # removal
            prev_ver = details["PreviousPackage"][1]
            diff["removed"].append(f"{name} {prev_ver}")
    return diff


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


def build_release(old_ver, new_ver, diff, security, notes, summary, variant, arch):
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
    release_id = f"{variant}-{arch}-{old_ver}-{new_ver}"
    return {
        "id": release_id,
        "variant": variant,
        "arch": arch,
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

    # Collect all entries; sort security packages first so they survive any cap
    entries = []
    for pkg in diff.get("upgraded", []):
        parts = pkg.split(" -> ", 1)
        name = parts[0].rsplit(" ", 1)[0] if len(parts) == 2 else pkg.split()[0]
        entries.append((name not in security, "upgraded", name, pkg))
    for pkg in diff.get("added", []):
        name = pkg.rsplit(" ", 1)[0]
        entries.append((name not in security, "added", name, pkg))
    for pkg in diff.get("removed", []):
        name = pkg.rsplit(" ", 1)[0]
        entries.append((name not in security, "removed", name, pkg))
    entries.sort(key=lambda e: e[0])  # False (security) sorts before True

    cap = 40
    shown = entries[:cap]
    extra = len(entries) - len(shown)

    lines = []
    for _, kind, name, pkg in shown:
        sec = "[security] " if name in security else ""
        lines.append(f"  - {sec}{kind}: {pkg}")
        if kind == "upgraded" and notes and name in notes:
            lines.append(f"    note: {notes[name]}")
    if extra:
        lines.append(f"  (... and {extra} more package changes)")
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


_FC_RE = re.compile(r'(\.fc\d+)$')


def _trim_evr_pair(old_evr, new_evr):
    def split(evr):
        idx = evr.rfind('-')
        if idx == -1:
            return evr, None, None
        ver, rel = evr[:idx], evr[idx + 1:]
        m = _FC_RE.search(rel)
        return ver, rel[:m.start()] if m else rel, m.group(1) if m else None

    ov, or_, ofc = split(old_evr)
    nv, nr, nfc = split(new_evr)
    if ofc is not None and ofc == nfc:
        ofc = nfc = None
    # Keep release only when version is unchanged but release differs (e.g. 3.26.4-2 → 3.26.4-6)
    if not (ov == nv and or_ != nr):
        or_ = nr = None

    def fmt(v, r, fc):
        return v + (f'-{r}' if r is not None else '') + (fc if fc is not None else '')

    return fmt(ov, or_, ofc), fmt(nv, nr, nfc)


def format_markdown(old_ver, new_ver, diff, security=frozenset(), summary=None, notes=None, verbose=False):
    total = sum(len(v) for v in diff.values())
    label = "change" if total == 1 else "changes"
    lines = [f"## {new_ver} ({total} {label})"]
    if summary:
        lines.append(summary)
    lines.append("")

    if not verbose:
        return "\n".join(lines)

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
            old_disp, new_disp = _trim_evr_pair(old_evr, parts[1])
            lines.append(f"- {sec}{link(name)} ({old_disp} → {new_disp})")
            append_note(name)

    for pkg in diff["added"]:
        name, _, evr = pkg.rpartition(" ")
        lines.append(f"- [New!] {link(name)} ({evr})")

    for pkg in diff["removed"]:
        name = pkg.rsplit(" ", 1)[0]
        lines.append(f"- [Removed] {link(name)}")

    lines.append("")
    return "\n".join(lines)


def format_ansi(old_ver, new_ver, diff, security=frozenset(), summary=None, notes=None, verbose=False):
    total = sum(len(v) for v in diff.values())
    label = "change" if total == 1 else "changes"
    lines = [f"{_B}{_CY}{new_ver}{_R}  {_D}({total} {label}){_R}"]
    if summary:
        lines.append(f"{_I}{summary}{_R}")
    lines.append("")

    if not verbose:
        return "\n".join(lines)

    if not any(diff.values()):
        lines.append(f"  {_D}No package changes.{_R}\n")
        return "\n".join(lines)

    def append_note(name):
        if notes and name in notes:
            for note_line in notes[name].splitlines():
                if note_line.strip():
                    lines.append(f"    {_D}{note_line.strip()}{_R}")

    for pkg in diff["upgraded"]:
        parts = pkg.split(" -> ", 1)
        if len(parts) == 2:
            old_parts = parts[0].rsplit(" ", 1)
            name = old_parts[0] if len(old_parts) == 2 else parts[0]
            old_evr = old_parts[1] if len(old_parts) == 2 else ""
            sec = f"{_B}{_RD}[!]{_R} " if name in security else ""
            old_disp, new_disp = _trim_evr_pair(old_evr, parts[1])
            lines.append(f"  {sec}{_B}{name}{_R}  {_D}{old_disp} → {new_disp}{_R}")
            append_note(name)

    for pkg in diff["added"]:
        name, _, evr = pkg.rpartition(" ")
        lines.append(f"  {_GN}[New!]{_R} {_B}{name}{_R}  {_D}{evr}{_R}")

    for pkg in diff["removed"]:
        name = pkg.rsplit(" ", 1)[0]
        lines.append(f"  {_D}[Removed] {name}{_R}")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Diff Fedora Silverblue/CoreOS releases",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--releases", type=int, default=20, metavar="N",
                        help="number of release pairs to show")
    parser.add_argument("--version", default="44",
                        help="Fedora version (Silverblue only)")
    parser.add_argument("--arch", default="x86_64", help="architecture")
    parser.add_argument("--variant", default="silverblue",
                        help=f"OS variant ({', '.join(VARIANTS)})")
    parser.add_argument("--verbose", action="store_true",
                        help="show full package list for each release")
    parser.add_argument("--no-security", action="store_true",
                        help="skip Bodhi security annotations")
    parser.add_argument("--changelogs", action="store_true",
                        help="show Bodhi notes and Koji changelogs per package")
    parser.add_argument("--cache-dir", metavar="PATH",
                        help="directory for persistent summary cache")
    parser.add_argument("--output", choices=["markdown", "ansi"],
                        default="ansi" if sys.stdout.isatty() else "markdown",
                        help="output format (default: ansi if terminal, markdown if piped)")
    args = parser.parse_args()

    variant_info = VARIANTS.get(args.variant)
    if not variant_info:
        sys.exit(f"error: unknown variant '{args.variant}' (known: {', '.join(VARIANTS)})")

    n = args.releases
    variant_label = variant_info["label"]
    variant_type = variant_info["type"]
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    cache_dir = args.cache_dir
    toc = load_toc(cache_dir) if cache_dir else {}
    formatter = format_ansi if args.output == "ansi" else format_markdown

    if variant_type == "ostree":
        print(f"Resolving {args.variant} {args.version}/{args.arch}...", file=sys.stderr)
        head_commit = resolve_ref(args.version, args.arch, args.variant)
        print(f"HEAD: {head_commit[:12]}...", file=sys.stderr)

    ctx = tempfile.TemporaryDirectory() if variant_type == "ostree" else contextlib.nullcontext()
    with ctx as repo_dir:
        if variant_type == "ostree":
            setup_repo(repo_dir)
            print(f"Fetching {n + 2} commits of history...", file=sys.stderr)
            pull_history(repo_dir, head_commit, n + 2)
            commits = get_commits(repo_dir, head_commit, n + 1)
            if len(commits) < 2:
                sys.exit("error: fewer than 2 commits found in history")
            actual = min(n, len(commits) - 1)
            pairs = [(commits[i + 1]["version"], commits[i]["version"]) for i in range(actual)]
            hashes = {c["version"]: c["hash"] for c in commits}

            def get_diff(old_ver, new_ver):
                print(f"Diffing {old_ver} → {new_ver}...", file=sys.stderr)
                return diff_commits(repo_dir, hashes[old_ver], hashes[new_ver])

        else:  # coreos
            stream = variant_info["stream"]
            print(f"Fetching CoreOS {stream} build list...", file=sys.stderr)
            builds = get_coreos_builds(n + 1, args.arch, stream)
            actual = min(n, len(builds) - 1)
            pairs = [(builds[i + 1]["id"], builds[i]["id"]) for i in range(actual)]

            def get_diff(old_ver, new_ver):
                print(f"Fetching {new_ver} metadata...", file=sys.stderr)
                return diff_coreos(new_ver, args.arch, stream)

        if args.output == "ansi":
            print(f"\n{_B}Fedora {variant_label} {args.arch} — Last {actual} Releases{_R}\n")
        else:
            print(f"\n# Fedora {variant_label} {args.arch} — Last {actual} Releases\n")

        for old_ver, new_ver in pairs:
            toc_key = f"{args.variant}-{args.arch}-{old_ver}→{new_ver}"
            release_id = f"{args.variant}-{args.arch}-{old_ver}-{new_ver}"

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
                diff = get_diff(old_ver, new_ver)
                security, bodhi_notes = get_security_packages(diff)
                all_notes = {**get_changelogs(diff), **bodhi_notes}
                summary = summarize_release(diff, security, all_notes, api_key)
                release = build_release(old_ver, new_ver, diff, security, all_notes, summary, args.variant, args.arch)
                if cache_dir:
                    save_release(cache_dir, release)
                    toc[toc_key] = toc_entry(release)
                    save_toc(cache_dir, toc)

            # Render phase: flags only affect output, not what was fetched/cached
            print(formatter(
                old_ver, new_ver,
                release_to_diff(release),
                release_to_security(release) if not args.no_security else set(),
                release.get("summary"),
                release_to_notes(release) if args.changelogs else None,
                verbose=args.verbose,
            ))


if __name__ == "__main__":
    main()
