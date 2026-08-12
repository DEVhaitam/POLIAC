# poliac

A Terraform linter, delivered over LSP, whose recommendations are grounded in production
metrics instead of hardcoded rules.

Give poliac a Terraform file, a CSV of production/runtime metrics, and a short statement of what
you're trying to achieve — it asks an LLM to recommend changes to the infrastructure code, with
a rationale, and with every claim checked against the real data before it's ever shown to you.

```
main.tf        (your infrastructure code)   ┐
metrics.csv    (production/runtime data)    ├──▶  LLM  ──▶  validator  ──▶  diagnostics,
specification.md    (your stated intent)         ┘                                hover, quick-fix
```

## What it supports today

- **Infrastructure**: `libvirt_domain` (vcpu/memory) and `aws_instance` (instance type, checked
  against a real catalog so the model can't invent one). Adding a resource type is a config
  file, not a code change.
- **LLM providers**: Gemini, Claude (Anthropic), ChatGPT (OpenAI), and DeepSeek — pick per run,
  no code changes.
- **Interfaces**: a CLI (`poliac inspect`, `poliac analyse`) and an LSP server (`poliac --stdio`)
  with diagnostics, hover, quick-fix, and code lens. No packaged editor extension yet — any
  LSP-capable client configured to launch `poliac --stdio` works.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,gemini]"          # swap/add: anthropic, openai, deepseek
```

Set the API key for whichever provider you're using (never in a config file):

```bash
export GEMINI_API_KEY=...        # or ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY
```

## Quickstart

```bash
poliac inspect examples/libvirt/main.tf --metrics-csv examples/libvirt/analysis.csv
```
Parses your Terraform and metrics, prints the resource inventory. Free — no LLM call.

```bash
cd examples/libvirt
poliac analyse main.tf --dry-run     # see the exact prompt that would be sent, still free
poliac analyse main.tf               # the real thing
```
Prints validated recommendations as JSON, each with a rationale and the exact metrics cells it
cites, plus an overall assessment. Anything the model claimed that didn't check out against your
actual files is silently dropped from the output and logged to `.poliac/rejected.jsonl` instead.

Every setting above can be overridden per-run instead of coming from `.poliac.yaml` — useful for
trying a different contract, metrics snapshot, or LLM without touching the config file:

```bash
poliac analyse main.tf \
  --metrics-csv examples/libvirt/analysis.csv \
  --contract examples/libvirt/specification-cost-cutting.md \
  --llm-provider anthropic --llm-model claude-opus-5
```

## Configuration

Optional `.poliac.yaml` at your project root — everything here can also be passed as a CLI flag
for a one-off run:

```yaml
metrics:
  path: ./analysis.csv
contract:
  path: ./specification.md      # what are you trying to achieve?
llm:
  provider: gemini          # gemini | anthropic | openai | deepseek
  model: gemini-3.6-flash
```

Run `poliac analyse --help` / `poliac inspect --help` for the full flag list.

## As an LSP server

```bash
poliac --stdio
```
Point an editor at this for `*.tf` files (namespaced `poliac.*`, source `"poliac"`, so it runs
alongside `terraform-ls`). Trigger analysis via the `poliac.analyse` command; results show up as
diagnostics, hover text, and quick-fixes.
