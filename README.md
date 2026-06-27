# diffedora

Shows package diffs between Fedora Silverblue and CoreOS daily releases.

## Usage

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

Optionally, `$ANTHROPIC_API_KEY` enables generated one-line summaries for each release.

## Examples

```sh
# Last 20 Silverblue releases (default)
./run.sh

# Last 5 CoreOS releases
./run.sh --variant coreos --releases 5

# Show changelogs for each package change
./run.sh --changelogs --releases 3
```

## Hosted
The hosted [Diffedora](https://dfurnes.github.io/diffedora/) pages are updated hourly.
