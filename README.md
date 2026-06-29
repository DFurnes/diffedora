# diffedora

Shows package diffs between Fedora Silverblue and CoreOS daily releases, with AI-generated summaries and security annotations.

## Usage

```
python3 diffedora.py [options]
```

Or via the container wrapper:

```
./run.sh [options]
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--releases N` | `20` | Number of release pairs to show |
| `--version VER` | `44` | Fedora version (Silverblue only) |
| `--arch ARCH` | `x86_64` | Architecture |
| `--variant VAR` | `silverblue` | OS variant (`silverblue`, `coreos`) |
| `--output FORMAT` | auto | Output format: `ansi`, `markdown`, or `html` |
| `--cache-dir PATH` | — | Directory for persistent release cache |
| `--changelogs` | off | Show Bodhi notes and Koji changelogs per package |
| `--no-security` | off | Skip Bodhi metadata lookups |
| `--verbose` | off | Show full package list (not just summary) |

An `ANTHROPIC_API_KEY` environment variable enables AI-generated one-line summaries for each release. Without it, summaries are omitted.

## Examples

```sh
# Last 20 Silverblue releases (default)
python3 diffedora.py

# Last 5 CoreOS releases
python3 diffedora.py --variant coreos --releases 5

# HTML output with cached results (skips API calls for already-seen releases)
python3 diffedora.py --output html --cache-dir data > index.html

# Show changelogs for each package change
python3 diffedora.py --changelogs --releases 3

# Fedora 43 Silverblue
python3 diffedora.py --version 43
```

## Sample output

```
# Fedora Silverblue x86_64 — Last 3 Releases

## 44.20260628.0 (9 changes)

curl security fix (CVE-2024-9681) and bind DNS update.

- bind-libs        32:9.18.49-1.fc44 → 32:9.18.50-1.fc44  [bugfix]
- bind-utils       32:9.18.49-1.fc44 → 32:9.18.50-1.fc44  [bugfix]
- curl             8.13.0-3.fc44 → 8.13.0-4.fc44           [security]
- gnome-software   50.2-1.fc44 → 50.3-1.fc44               [bugfix]
...
```

Progress output goes to stderr; the formatted result goes to stdout.
