# Architecture

diffedora pulls commit history from Fedora's compose ostree repository, uses `rpm-ostree db diff` to compare adjacent commits, and annotates security updates via the Bodhi API.

## Data sources

### Compose ostree repository (primary)

**URL:** `https://kojipkgs.fedoraproject.org/compose/ostree/repo/`

This is the canonical source for all Silverblue (and Kinoite, Sericea, etc.) release history. Key quirks:

- **No summary file.** `ostree remote refs` fails with "server has no summary file". Individual refs are still readable via plain HTTP at `refs/heads/fedora/{version}/{arch}/{variant}` — the script reads the ref directly with `urllib.request` to resolve the current HEAD commit hash.
- **Objects are accessible.** Despite the missing summary, individual commit objects at `objects/{prefix}/{suffix}.commit` return HTTP 200. `ostree pull --commit-metadata-only --depth=N` works and is fast (~1.4 MB for 20+ commits).
- **Commit metadata includes the compose version.** Each commit's `Version:` field contains the compose ID (e.g. `44.20260627.0`), so there's no need to cross-reference the compose listing at `kojipkgs.fedoraproject.org/compose/updates/`.
- **`rpm-ostree db diff` fetches RPM data on-demand.** Even with a metadata-only pull, `rpm-ostree db diff --repo=<local>` works — it fetches just the RPM database objects it needs from the remote at diff time, without downloading the full OS image.

The ref `fedora/44/x86_64/silverblue` is updated with every daily compose, not just GA releases.

### Sources we investigated and ruled out

| Source | Why not used |
|--------|-------------|
| `ostree.fedoraproject.org` (production remote) | Refs are listable but object fetches return HTTP 404 — presumably CDN-gated |
| Per-compose ostree repos at kojipkgs | Don't exist; there's only the shared accumulated repo above |
| `compose/metadata/rpms.json` | Lists 0 packages for Silverblue — it's image-based, not a traditional RPM repo |
| `.ociarchive` files at kojipkgs | 2.4 GB each; `index.json` is at the *end* of the TAR (not the beginning), so HTTP range requests can't cheaply fetch the manifest |
| quay.io (`fedora/fedora-silverblue`) | Only carries major-version tags (`44`, `44-aarch64`) for Fedora 44 — no per-compose tags |

### Bodhi API (security annotations)

**URL pattern:** `https://bodhi.fedoraproject.org/updates/?builds={nvr}&rows_per_page=1`

where `nvr` is `{name}-{version}-{release}` (e.g. `kernel-7.0.13-200.fc44`).

- **Epoch must be stripped.** `rpm-ostree db diff` includes epochs in EVR strings (e.g. `2:1.43.2-1.fc44`). Bodhi build NVRs never include the epoch; strip it before constructing the query.
- The `type` field in the response indicates `security`, `bugfix`, `enhancement`, or `newpackage`.
- Queries run in parallel (up to 8 concurrent) via `ThreadPoolExecutor` to keep latency reasonable. Pass `--no-security` to skip entirely.
