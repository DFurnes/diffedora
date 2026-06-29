#!/usr/bin/env python3
"""
diffedora — show package diffs between Fedora Silverblue releases
"""

import argparse
import contextlib
import html
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
You are writing a changelog headline for a Fedora OS update — a short noun phrase, not a sentence.
Under 20 words. No markdown, no leading label.

Format: list the key software, then what happened to it. Like a git commit subject or release note title.
Do NOT write full sentences with verbs like "receives", "gets", "is updated", or "has been fixed".
Do NOT write filler like "and version updates", "various improvements", "and more".

Version numbers: only include them for well-known packages users track by version (kernel, Mesa, Firefox, GNOME Shell).
Skip version numbers for everything else — the change type (bug fix, security fix, new feature) matters more.

Package names: use the description in parentheses to write human-readable names.
Prefer "HP printer drivers" over "hplip", "spell checker" over "hunspell", "DNS utilities" over "bind-utils".
Well-known names (kernel, vim, curl, Firefox) can stay as-is.

Security updates: name the affected software and include CVE IDs when listed.

Good: "Kernel 7.0.13, HP printer driver, and GNOME Control Center bug fixes."
Good: "curl security fix (CVE-2024-9681) and spell checker update."
Good: "Flatpak portal library gains clipboard support."
Bad: "GNOME Software 50.3, hunspell 1.7.3, and bind-utils receive bug fixes and version updates."\
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


def _get_bodhi_update(name, new_evr, source_name=None):
    # Bodhi indexes by source build NVR; use source_name when available.
    lookup = source_name if source_name and source_name != name else name
    nvr = f"{lookup}-{_strip_epoch(new_evr)}"
    url = f"https://bodhi.fedoraproject.org/updates/?builds={nvr}&rows_per_page=1"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        updates = data.get("updates", [])
        if not updates:
            return None, None, None, None, None
        update = updates[0]
        notes = update.get("notes", "").strip() or None
        update_type = update.get("type") or None
        severity = update.get("severity") or None
        cves = [c["cve_id"] for c in update.get("cves", []) if c.get("cve_id")] or None
        alias = update.get("alias") or None
        return update_type, severity, notes, cves, alias
    except Exception:
        return None, None, None, None, None


_srcpkg_cache: dict = {}


def _is_source_package(name):
    if name in _srcpkg_cache:
        return _srcpkg_cache[name]
    url = f"https://mdapi.fedoraproject.org/rawhide/srcpkg/{name}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            json.load(r)
        _srcpkg_cache[name] = True
        return True
    except Exception:
        _srcpkg_cache[name] = False
        return False


def _get_package_summary(name):
    url = f"https://mdapi.fedoraproject.org/rawhide/pkg/{name}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        summary = data.get("summary")
        co_packages = data.get("co-packages") or []
        # Find source package: check each co-package (plus self) against srcpkg endpoint.
        # Sort by length so the source (typically the shortest name) is checked first.
        source = next(
            (c for c in sorted(set(co_packages + [name]), key=len) if _is_source_package(c)),
            None,
        )
        return summary, source
    except Exception:
        return None, None


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
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{release['id']}.json").write_text(json.dumps(release, indent=2))


def build_release(old_ver, new_ver, diff, security, bodhi_data, notes, descriptions, sources, summary, variant, arch):
    def pkg_url(name):
        source = sources.get(name) if sources else None
        if source and source != name:
            return f"https://packages.fedoraproject.org/pkgs/{source}/{name}/"
        return f"https://packages.fedoraproject.org/pkgs/{name}/"

    changes = []
    for pkg in diff["upgraded"]:
        parts = pkg.split(" -> ", 1)
        if len(parts) == 2:
            old_parts = parts[0].rsplit(" ", 1)
            name = old_parts[0] if len(old_parts) == 2 else parts[0]
            from_evr = old_parts[1] if len(old_parts) == 2 else ""
            bd = bodhi_data.get(name, {}) if bodhi_data else {}
            src = sources.get(name) if sources else None
            alias = bd.get("alias")
            changes.append({
                "type": "upgrade",
                "package": name,
                "url": pkg_url(name),
                "source_package": src,
                "from": from_evr,
                "to": parts[1],
                "security": name in security,
                "update_type": bd.get("type"),
                "severity": bd.get("severity"),
                "cves": bd.get("cves"),
                "bodhi_url": f"https://bodhi.fedoraproject.org/updates/{alias}" if alias else None,
                "description": descriptions.get(name) if descriptions else None,
                "note": notes.get(name) if notes else None,
            })
    for pkg in diff["added"]:
        parts = pkg.rsplit(" ", 1)
        name, ver = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
        changes.append({
            "type": "added",
            "package": name,
            "url": pkg_url(name),
            "from": None,
            "to": ver,
        })
    for pkg in diff["removed"]:
        parts = pkg.rsplit(" ", 1)
        name, ver = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
        changes.append({
            "type": "removed",
            "package": name,
            "url": pkg_url(name),
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


def release_to_bodhi_data(release):
    return {c["package"]: {"type": c.get("update_type"), "severity": c.get("severity"),
                            "cves": c.get("cves"), "notes": c.get("note")}
            for c in release["changes"]
            if any([c.get("update_type"), c.get("severity"), c.get("cves")])}


def release_to_descriptions(release):
    return {c["package"]: c["description"]
            for c in release["changes"] if c.get("description")}


def release_to_sources(release):
    return {c["package"]: c["source_package"]
            for c in release["changes"] if c.get("source_package")}


def _bodhi_tag(name, bodhi_data):
    """Return a bracketed tag like [security/high|CVE-2024-1] or [bugfix] for the summary prompt."""
    bd = bodhi_data.get(name, {}) if bodhi_data else {}
    update_type = bd.get("type")
    if not update_type:
        return ""
    if update_type == "security":
        parts = ["security"]
        severity = bd.get("severity")
        if severity and severity != "unspecified":
            parts[0] = f"security/{severity}"
        cves = bd.get("cves")
        if cves:
            parts.append(",".join(cves[:3]))  # cap at 3 CVEs to avoid very long lines
        return f"[{'|'.join(parts)}] "
    return f"[{update_type}] "


def _group_entries(entries):
    """Collapse sub-packages (e.g. kernel-core) under their primary package.

    A package B is a sub-package of A when B.name starts with A.name + '-' and
    they share the exact same version transition string.
    """
    def vpair(pkg_str):
        parts = pkg_str.split(" -> ", 1)
        if len(parts) == 2:
            fp = parts[0].rsplit(" ", 1)
            return (fp[1] if len(fp) == 2 else "", parts[1])
        return ("", "")

    vmap = {name: vpair(pkg) for _, _, name, pkg in entries}
    assigned = set()
    result = []
    for sort_key, kind, name, pkg in sorted(entries, key=lambda e: len(e[2])):
        if name in assigned:
            continue
        subs = sorted(
            other for _, _, other, _ in entries
            if other != name and other.startswith(name + "-") and vmap.get(other) == vmap[name]
        )
        for s in subs:
            assigned.add(s)
        assigned.add(name)
        result.append((sort_key, kind, name, pkg, subs))
    result.sort(key=lambda e: e[0])
    return result


def summarize_release(diff, bodhi_data, notes, descriptions, api_key):
    if not api_key:
        return None

    # Sort: security first, then bugfix, then others; within each group preserve order
    _type_order = {"security": 0, "bugfix": 1, "enhancement": 2, "newpackage": 3}

    def _sort_key(name):
        bd = (bodhi_data or {}).get(name, {})
        return _type_order.get(bd.get("type"), 4)

    entries = []
    for pkg in diff.get("upgraded", []):
        parts = pkg.split(" -> ", 1)
        name = parts[0].rsplit(" ", 1)[0] if len(parts) == 2 else pkg.split()[0]
        entries.append((_sort_key(name), "upgraded", name, pkg))
    for pkg in diff.get("added", []):
        name = pkg.rsplit(" ", 1)[0]
        entries.append((_sort_key(name), "added", name, pkg))
    for pkg in diff.get("removed", []):
        name = pkg.rsplit(" ", 1)[0]
        entries.append((_sort_key(name), "removed", name, pkg))

    grouped = _group_entries(entries)

    cap = 40
    shown = grouped[:cap]
    extra = len(grouped) - len(shown)

    lines = []
    for _, kind, name, pkg, subs in shown:
        tag = _bodhi_tag(name, bodhi_data)
        desc = f" ({descriptions[name]})" if descriptions and name in descriptions else ""
        if subs:
            shown_subs = subs[:3]
            rest = len(subs) - len(shown_subs)
            sub_str = ", ".join(shown_subs) + (f", +{rest} more" if rest else "")
            sub_note = f" (also: {sub_str})"
        else:
            sub_note = ""
        lines.append(f"  - {tag}{kind}: {name}{desc}{sub_note} {pkg.split(' ', 1)[1] if ' ' in pkg else pkg}")
        if kind == "upgraded" and notes and name in notes:
            lines.append(f"    note: {notes[name]}")
    if extra:
        lines.append(f"  (... and {extra} more package changes)")
    body = json.dumps({
        "model": "claude-sonnet-4-6",
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


def get_bodhi_metadata(diff, sources=None):
    bodhi_data = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_get_bodhi_update, name, evr,
                               sources.get(name) if sources else None): name
                   for name, evr in _upgraded_candidates(diff)}
        for f in as_completed(futures):
            update_type, severity, notes, cves, alias = f.result()
            name = futures[f]
            if any([update_type, severity, notes, cves, alias]):
                bodhi_data[name] = {"type": update_type, "severity": severity,
                                    "notes": notes, "cves": cves, "alias": alias}
    security = {name for name, d in bodhi_data.items() if d.get("type") == "security"}
    return security, bodhi_data


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


def get_package_descriptions(diff):
    descriptions = {}
    sources = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_get_package_summary, name): name
                   for name, _ in _upgraded_candidates(diff)}
        for f in as_completed(futures):
            summary, source = f.result()
            name = futures[f]
            if summary:
                descriptions[name] = summary
            if source:
                sources[name] = source
    return descriptions, sources


_FC_RE = re.compile(r'(\.fc\d+)$')


_EPOCH_RE = re.compile(r'^\d+:')


def _trim_evr_pair(old_evr, new_evr):
    def split(evr):
        idx = evr.rfind('-')
        if idx == -1:
            return evr, None, None
        ver, rel = evr[:idx], evr[idx + 1:]
        m = _FC_RE.search(rel)
        return ver, rel[:m.start()] if m else rel, m.group(1) if m else None

    def normalize(ver):
        ver = _EPOCH_RE.sub('', ver)  # strip epoch (e.g. "1:")
        if ver.startswith('0^'):      # strip snapshot base (e.g. "0^20260526..." → "20260526...")
            ver = ver[2:]
        return ver

    ov, or_, ofc = split(old_evr)
    nv, nr, nfc = split(new_evr)
    ov, nv = normalize(ov), normalize(nv)
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


def _html_change(change):
    name = html.escape(change["package"])
    url  = html.escape(change["url"])
    if change["type"] == "upgrade":
        sec = "<strong>[!]</strong> " if change.get("security") else ""
        old_disp, new_disp = _trim_evr_pair(change.get("from") or "", change.get("to") or "")
        ver_text = f"{html.escape(old_disp)} → {html.escape(new_disp)}"
        bodhi_url = change.get("bodhi_url")
        if bodhi_url:
            ver = f'<small><a href="{html.escape(bodhi_url)}">{ver_text}</a></small>'
        else:
            ver = f"<small>{ver_text}</small>"
        return f'    <li>{sec}<a href="{url}">{name}</a>  {ver}</li>'
    elif change["type"] == "added":
        ver = html.escape(change.get("to") or "")
        return f'    <li><b>[New!]</b> <a href="{url}">{name}</a>  <small>{ver}</small></li>'
    else:
        return f'    <li><span class="dim">[Removed] {name}</span></li>'


def _html_release(release):
    new_ver = html.escape(release["new_version"])
    total   = len(release["changes"])
    label   = "change" if total == 1 else "changes"
    parts   = [f'  <article>',
               f'    <h2>{new_ver}  <small>({total} {label})</small></h2>']
    if release.get("summary"):
        parts.append(f'    <em>{html.escape(release["summary"])}</em>')
    if release["changes"]:
        parts.append('    <ul>')
        parts.extend(_html_change(c) for c in release["changes"])
        parts.append('    </ul>')
    parts.append('  </article>')
    return "\n".join(parts)


def format_html(variant, arch, releases):
    _LABELS = {"silverblue": "Silverblue", "coreos": "CoreOS"}
    _PAGES  = {"silverblue": "index.html",  "coreos": "coreos.html"}
    nav = " · ".join(
        f'<a{"" if v != variant else " class=\"active\""} href="{_PAGES[v]}">{label}</a>'
        for v, label in _LABELS.items()
    )
    blocks = "\n".join(_html_release(r) for r in releases)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>diffedora</title>
  <style>
    :root {{
      --bg:    #1a1a1a;
      --fg:    #cccccc;
      --cyan:  #4ec9b0;
      --red:   #f14c4c;
      --green: #4ec94e;
      --dim:   #666666;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--fg);
      font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, Consolas, monospace;
      font-size: 14px;
      line-height: 1.6;
      padding: 2.5rem 2rem;
    }}
    main    {{ max-width: 480px; margin: 0 auto; }}
    header  {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 2rem; }}
    h1      {{ font-size: 1.05em; }}
    nav     {{ font-size: 0.95em; }}
    nav a   {{ color: var(--dim); text-decoration: none; }}
    nav a.active           {{ color: var(--fg); font-weight: bold; }}
    nav a:not(.active):hover {{ color: var(--cyan); }}
    article {{ margin-bottom: 1.75rem; }}
    h2      {{ color: var(--cyan); font-size: 1em; font-weight: bold; margin-bottom: 0.2rem; }}
    h2 small {{ color: var(--dim); font-weight: normal; }}
    em      {{ display: block; margin-bottom: 0.6rem; }}
    ul      {{ list-style: none; padding-left: 2ch; }}
    li      {{ line-height: 1.5; }}
    ul a    {{ color: var(--fg); font-weight: bold; text-decoration: none; }}
    ul a:hover {{ text-decoration: underline; }}
    strong  {{ color: var(--red); }}
    b       {{ color: var(--green); }}
    small   {{ font-size: 1em; color: var(--dim); }}
    .cyan   {{ color: var(--cyan); }}
    .dim    {{ color: var(--dim); }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1><span class="cyan">diff</span>edora</h1>
      <nav>{nav}</nav>
    </header>
{blocks}
  </main>
</body>
</html>"""


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
    parser.add_argument("--output", choices=["markdown", "ansi", "html"],
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
    if args.output == "ansi":
        formatter = format_ansi
    elif args.output == "markdown":
        formatter = format_markdown
    else:
        formatter = None  # html: collected and rendered after loop

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
        elif args.output == "markdown":
            print(f"\n# Fedora {variant_label} {args.arch} — Last {actual} Releases\n")

        collected = []
        for old_ver, new_ver in pairs:
            toc_key = f"{args.variant}-{args.arch}-{old_ver}→{new_ver}"
            release_id = f"{args.variant}-{args.arch}-{old_ver}-{new_ver}"

            release = load_release(cache_dir, release_id) if cache_dir else None

            if release:
                if not release.get("summary") and api_key:
                    release["summary"] = summarize_release(
                        release_to_diff(release), release_to_bodhi_data(release),
                        release_to_notes(release), release_to_descriptions(release), api_key)
                    if release["summary"] and cache_dir:
                        save_release(cache_dir, release)
                        toc[toc_key] = toc_entry(release)
                        save_toc(cache_dir, toc)
            else:
                diff = get_diff(old_ver, new_ver)
                descriptions, sources = get_package_descriptions(diff)
                security, bodhi_data = get_bodhi_metadata(diff, sources)
                bodhi_notes = {name: d["notes"] for name, d in bodhi_data.items() if d.get("notes")}
                all_notes = {**get_changelogs(diff), **bodhi_notes}
                summary = summarize_release(diff, bodhi_data, all_notes, descriptions, api_key)
                release = build_release(old_ver, new_ver, diff, security, bodhi_data, all_notes, descriptions, sources, summary, args.variant, args.arch)
                if cache_dir:
                    save_release(cache_dir, release)
                    toc[toc_key] = toc_entry(release)
                    save_toc(cache_dir, toc)

            if args.output == "html":
                collected.append(release)
            else:
                print(formatter(
                    old_ver, new_ver,
                    release_to_diff(release),
                    release_to_security(release) if not args.no_security else set(),
                    release.get("summary"),
                    release_to_notes(release) if args.changelogs else None,
                    verbose=args.verbose,
                ))

        if args.output == "html":
            print(format_html(args.variant, args.arch, collected))


if __name__ == "__main__":
    main()
