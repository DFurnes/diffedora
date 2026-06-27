# diffedora

Shows package diffs between Fedora Silverblue daily releases, printed as Markdown.

## Usage

```
podman build -t diffedora . && podman run --rm diffedora [options]
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--releases N` | `20` | Number of release pairs to show |
| `--version VER` | `44` | Fedora version |
| `--arch ARCH` | `x86_64` | Architecture |
| `--variant VAR` | `silverblue` | OS variant (e.g. `kinoite`, `sericea`) |

## Examples

```
# Last 20 Silverblue releases (default)
podman build -t diffedora . && podman run --rm diffedora

# Last 5 releases
podman build -t diffedora . && podman run --rm diffedora --releases 5

# Kinoite instead of Silverblue
podman build -t diffedora . && podman run --rm diffedora --variant kinoite

# Fedora 43
podman build -t diffedora . && podman run --rm diffedora --version 43

# Save to a file
podman build -t diffedora . && podman run --rm diffedora > releases.md
```

## Sample output

```markdown
# Fedora Silverblue x86_64 — Last 20 Releases

## 44.20260626.0 → 44.20260627.0 (14 changes)

- **kernel** (7.0.12-201.fc44 → 7.0.13-200.fc44)
- **gnome-control-center** (50.2-1.fc44 → 50.3-1.fc44)
- [New!] **some-new-pkg** (1.0-1.fc44)
- [Removed] **old-pkg**
...

## 44.20260625.0 → 44.20260626.0 (3 changes)
...
```

Progress messages are printed to stderr; the Markdown goes to stdout.
