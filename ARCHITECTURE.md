# Architecture

diffedora pulls commit history from Fedora's compose ostree repository, compares adjacent commits with `rpm-ostree db diff`, enriches each package change with metadata from Bodhi, mdapi, and Koji, then generates an AI summary via the Claude API. Output is static HTML deployed to GitHub Pages.

## Pipeline

For each release pair (old → new):

1. **Diff** — `rpm-ostree db diff` produces a list of upgraded, added, and removed packages with old/new EVRs.
2. **Descriptions + source packages** — mdapi is queried for each upgraded package to get its human-readable `summary` and its source package name (needed for URLs and Bodhi lookups).
3. **Bodhi metadata** — Bodhi is queried (using the source NVR) for update type, severity, notes, CVE IDs, and the update alias (used for linking).
4. **Changelogs** — Koji is queried for the first `%changelog` entry of each build.
5. **Summarize** — the enriched diff is sent to Claude, which returns a one-line changelog headline.
6. **Cache** — the result is written to `data/releases/{id}.json` and skipped on subsequent runs.

Steps 2–4 run in parallel via `ThreadPoolExecutor` (8 workers each).

## Data sources

### Compose ostree repository (primary)

**URL:** `https://kojipkgs.fedoraproject.org/compose/ostree/repo/`

This is the canonical source for all Silverblue (and Kinoite, Sericea, etc.) release history. Key quirks:

- **No summary file.** `ostree remote refs` fails with "server has no summary file". Individual refs are still readable via plain HTTP at `refs/heads/fedora/{version}/{arch}/{variant}` — the script reads the ref directly with `urllib.request` to resolve the current HEAD commit hash.
- **Objects are accessible.** Despite the missing summary, individual commit objects at `objects/{prefix}/{suffix}.commit` return HTTP 200. `ostree pull --commit-metadata-only --depth=N` works and is fast (~1.4 MB for 20+ commits).
- **Commit metadata includes the compose version.** Each commit's `Version:` field contains the compose ID (e.g. `44.20260627.0`), so there's no need to cross-reference the compose listing at `kojipkgs.fedoraproject.org/compose/updates/`.
- **`rpm-ostree db diff` fetches RPM data on-demand.** Even with a metadata-only pull, `rpm-ostree db diff --repo=<local>` works — it fetches just the RPM database objects it needs from the remote at diff time, without downloading the full OS image.

The ref `fedora/44/x86_64/silverblue` is updated with every daily compose, not just GA releases.

### Bodhi API

**URL pattern:** `https://bodhi.fedoraproject.org/updates/?builds={source_nvr}&rows_per_page=1`

Bodhi is queried using the **source** package NVR (e.g. `bind-9.18.50-1.fc44`), not the binary package name. Sub-packages like `bind-libs` and `bind-utils` share the same Bodhi update as their source `bind`, so using the source NVR is required to find the update. Source package names come from the mdapi lookup (see below).

Fields used from the response:
- `type` — `security`, `bugfix`, `enhancement`, or `newpackage`
- `severity` — e.g. `critical`, `high`, `medium`, `low`, `unspecified`
- `notes` — maintainer-written release notes (often rich markdown for bugfix/security updates; auto-generated for enhancements)
- `cves` — list of CVE IDs
- `alias` — e.g. `FEDORA-2026-abc123`, used to construct the Bodhi update URL shown in the HTML

**Epoch must be stripped.** `rpm-ostree db diff` includes epochs in EVR strings (e.g. `2:1.43.2-1.fc44`). Bodhi build NVRs never include the epoch.

Queries run in parallel (up to 8 concurrent). Pass `--no-security` to skip entirely.

### Fedora mdapi

**URL patterns:**
- `https://mdapi.fedoraproject.org/rawhide/pkg/{name}` — binary package metadata
- `https://mdapi.fedoraproject.org/rawhide/srcpkg/{name}` — source package metadata (used as a probe)

mdapi serves RPM repodata over HTTP. Two things are fetched per package:

**1. Human-readable description (`summary` field)**
The RPM `Summary:` tag — e.g. `hplip` → `"HP Linux Imaging and Printing Project"`. This is passed to the summarizer so it can write "HP printer drivers" instead of "hplip".

**2. Source package name (via `co-packages` + `srcpkg` probe)**
`co-packages` lists all binary packages built from the same source (e.g. `kernel-core`'s co-packages include `kernel`, `kernel-debug`, `python3-perf`, etc.). To identify which co-package is the source, each candidate is probed against the `srcpkg` endpoint — a 200 response means it's a source package, an error means it's binary-only. Candidates are checked shortest-name-first, so the source is found on the first try in almost all cases.

This handles cases that name-prefix heuristics miss: `python3-perf → kernel`, `libsane-hpaio → hplip`.

### Koji XML-RPC

**URL:** `https://koji.fedoraproject.org/kojihub`

Koji is queried for the most recent `%changelog` entry of each build via `getChangelogEntries('{name}-{version}-{release}')`. The first entry's text is stored as the `note` on the change object and included in the summarizer prompt.

Note: Koji builds are indexed by **source** NVR. Queries for binary package NVRs (e.g. `kernel-core-7.0.13-200.fc44`) return nothing; the source NVR (`kernel-7.0.13-200.fc44`) must be used.

### Fedora CoreOS Builds API

**URL pattern:** `https://builds.coreos.fedoraproject.org/prod/streams/{stream}/builds/builds.json`

CoreOS uses a different mechanism than Silverblue — release history is fetched from the CoreOS builds API, and individual release metadata at `.../builds/{version}/{arch}/meta.json` contains the OSTree commit hash. Diffs then use the same `rpm-ostree db diff` path.

### Sources we investigated and ruled out

| Source | Why not used |
|--------|-------------|
| `ostree.fedoraproject.org` (production remote) | Refs are listable but object fetches return HTTP 404 — presumably CDN-gated |
| Per-compose ostree repos at kojipkgs | Don't exist; there's only the shared accumulated repo above |
| `compose/metadata/rpms.json` | Lists 0 packages for Silverblue — it's image-based, not a traditional RPM repo |
| `.ociarchive` files at kojipkgs | 2.4 GB each; `index.json` is at the *end* of the TAR (not the beginning), so HTTP range requests can't cheaply fetch the manifest |
| quay.io (`fedora/fedora-silverblue`) | Only carries major-version tags (`44`, `44-aarch64`) for Fedora 44 — no per-compose tags |
| `mdapi.fedoraproject.org/fedora-44/pkg/{name}` | Returns HTTP 400 — use `rawhide` endpoint instead |

## AI summarization

**Model:** `claude-sonnet-4-6`, 80 `max_tokens`

The summarizer receives an enriched diff (up to 40 lines, capped to avoid prompt bloat) and returns a single changelog headline — a short noun phrase, not a sentence. The prompt instructs the model to:

- Use parenthesized descriptions to write human-readable names ("HP printer drivers" not "hplip")
- Include version numbers only for well-known packages (kernel, Mesa, Firefox)
- Use CVE IDs for security updates
- Avoid verbs like "receives", "gets", filler like "and version updates"

Sub-packages are collapsed before the prompt is built: `kernel-core`, `kernel-modules`, etc. are grouped under `kernel` with an `(also: ...)` suffix, reducing 7+ kernel lines to 1.

Summaries are cached in the release JSON and never regenerated unless the file is deleted.

## Output and deployment

**HTML output** (`--output html`) renders all releases as a single static page with dark-mode CSS. Each release shows the AI summary, a package list with Bodhi-linked version strings, and security/update-type badges.

**GitHub Actions** runs hourly, builds Silverblue (last 20 releases) and CoreOS (last 5 releases), and force-pushes the result to the `gh-pages` branch. The data cache (`data/releases/*.json`) is preserved between runs by checking it out from `gh-pages` at the start of each CI run, so Claude is only called for genuinely new releases.

**Cache format:** each release is a JSON file at `data/releases/{variant}-{arch}-{old}-{new}.json` with a `changes` array (one object per package change) and a `summary` string. `data/summary.json` is a table of contents used to build the nav without loading all release files.
