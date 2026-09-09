# poliac (VS Code client)

Minimal client that launches `poliac --stdio` and wires it up as a language server for
`*.tf` files. It adds no logic of its own — every diagnostic, hover, quick-fix, and code lens
comes from the server (`src/poliac/server.py`); this extension just starts it and exposes its
three commands in the Command Palette.

## Requirements

`poliac` must be installed and resolvable as a command (`pip install -e .` from the repo root,
inside whatever environment VS Code's integrated terminal / the extension host process sees on
`PATH`). If it isn't on `PATH`, set `poliac.serverCommand` to an absolute path (e.g. the
`poliac` binary inside a venv).

## Commands

| Command | Effect |
|---|---|
| `poliac: Analyse Current File` | Runs the full pipeline against the active `.tf` file. Real, paid LLM call — never automatic. |
| `poliac: Set Metrics CSV File` | File picker; overrides `metrics.path` for this session without editing `.poliac.yaml`. |
| `poliac: Set Contract File` | Same, for `contract.path`. |

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `poliac.serverCommand` | `"poliac"` | Command used to launch the server. Must support `--stdio`. |

## Development

```bash
cd client-vscode
npm install
npm run compile        # or `npm run watch`
```

Open the **`poliac` repo root** (not this folder alone) in VS Code and press **F5** — the
debug config lives at `<repo root>/.vscode/launch.json` (not here), because VS Code only
auto-discovers `.vscode/launch.json` relative to whatever folder is actually open as the
workspace root. It builds the extension and opens an Extension Development Host with
`examples/libvirt` (which already ships a `.poliac.yaml`, `main.tf`, `analysis.csv`, and
`specification.md`) as its workspace, so you can run `poliac: Analyse Current File` on
`main.tf` immediately.

If you'd rather open `client-vscode/` as its own standalone window instead, F5 won't find a
launch config there — use **Run → Start Debugging Without Debugging** with
`--extensionDevelopmentPath=<path to this folder>` set manually, or just work from the repo
root as above.

Not implemented: packaging (`vsce package`) / marketplace publishing — out of scope for the PoC.
