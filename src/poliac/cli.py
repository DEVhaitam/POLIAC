from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from poliac import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poliac")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--stdio", action="store_true", help="run as an LSP server over stdio")
    subparsers = parser.add_subparsers(dest="command")

    inspect_parser = subparsers.add_parser(
        "inspect", help="parse Terraform + metrics, print the inventory (M1)"
    )
    inspect_parser.add_argument("files", nargs="+", type=Path, help="one or more .tf files")
    inspect_parser.add_argument("--metrics-csv", type=Path, help="metrics CSV path")
    inspect_parser.add_argument("--max-rows", type=int, default=300)
    inspect_parser.add_argument("--max-bytes", type=int, default=100_000)

    analyse_parser = subparsers.add_parser(
        "analyse", help="run the LLM advisor and print recommendations (M2/M3)"
    )
    analyse_parser.add_argument("files", nargs="+", type=Path, help="one or more .tf files")
    analyse_parser.add_argument("--metrics-csv", type=Path, help="metrics CSV path")
    analyse_parser.add_argument("--contract", type=Path, help="contract file path (required)")
    analyse_parser.add_argument(
        "--llm-provider", help="llm vendor, e.g. gemini/anthropic/openai/deepseek"
    )
    analyse_parser.add_argument("--llm-model", help="llm model id")
    analyse_parser.add_argument("--temperature", type=float)
    analyse_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the assembled prompt and exit -- no API call, no cost",
    )
    return parser


def _cmd_inspect(args: argparse.Namespace) -> int:
    from poliac.metrics.table import MetricsError, load_metrics
    from poliac.terraform.parser import TerraformParseError, parse_files

    try:
        resources = parse_files(args.files)
    except TerraformParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not resources:
        print("no resources found")
    for r in resources:
        print(f"\n{r.address}  (provider={r.provider}, file={r.file})")
        br = r.block_range
        print(f"  block_range: L{br.start_line}:{br.start_char} - L{br.end_line}:{br.end_char}")
        if not r.attributes:
            print("  (no attributes)")
        for name, attr in r.attributes.items():
            vr = attr.value_range
            literal = "literal" if attr.is_literal else "non-literal"
            print(
                f"  {name} = {attr.raw_value}  [{literal}]  "
                f"(L{vr.start_line}:{vr.start_char} - L{vr.end_line}:{vr.end_char})"
            )

    if args.metrics_csv is not None:
        try:
            table = load_metrics(
                args.metrics_csv, max_rows=args.max_rows, max_bytes=args.max_bytes
            )
        except MetricsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"\nmetrics: {table.path}")
        print(f"  {len(table.rows)} rows x {len(table.headers)} columns")
        print(f"  columns: {table.headers}")

    return 0


def _cmd_analyse(args: argparse.Namespace) -> int:
    from poliac.config import load_config
    from poliac.llm.client import LlmError, create_client
    from poliac.llm.context import TASK_INSTRUCTIONS, build_context
    from poliac.metrics.table import MetricsError, load_metrics
    from poliac.providers.registry import load_providers_for_resources
    from poliac.terraform.parser import TerraformParseError, parse_files

    workspace_root = Path.cwd()
    load_result = load_config(workspace_root)
    config = load_result.config
    for note in load_result.assumptions:
        print(f"poliac: {note}", file=sys.stderr)

    metrics_path = args.metrics_csv or (
        Path(config.metrics.path) if config.metrics.path else None
    )
    contract_path = args.contract or (Path(config.contract.path) if config.contract.path else None)
    provider_name = args.llm_provider or config.llm.provider
    model_name = args.llm_model or config.llm.model
    temperature = args.temperature if args.temperature is not None else config.llm.temperature

    if metrics_path is None:
        print(
            "error: --metrics-csv is required (or set metrics.path in .poliac.yaml)",
            file=sys.stderr,
        )
        return 1
    if contract_path is None:
        # SPEC §3.3: the contract is required, there is nothing sensible to default it to.
        print(
            "error: --contract is required (or set contract.path in .poliac.yaml)",
            file=sys.stderr,
        )
        return 1
    if not contract_path.is_file():
        print(f"error: {contract_path}: no such file", file=sys.stderr)
        return 1

    try:
        resources = parse_files(args.files)
    except TerraformParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        metrics = load_metrics(
            metrics_path, max_rows=config.metrics.max_rows, max_bytes=config.metrics.max_bytes
        )
    except MetricsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    provider_vocab, missing_providers = load_providers_for_resources(resources)
    if missing_providers:
        print(
            f"poliac: no provider vocabulary for {missing_providers!r}; type/catalog checks "
            "(V6) won't run for those resources' attributes",
            file=sys.stderr,
        )

    contract_text = contract_path.read_text()
    sources = {str(p): p.read_text() for p in args.files}
    prompt = build_context(sources, resources, metrics, contract_text, provider_vocab)

    if args.dry_run:
        print(TASK_INSTRUCTIONS)
        print(prompt)
        return 0

    if not provider_name or not model_name:
        print(
            "error: llm.provider and llm.model must be set "
            "(.poliac.yaml, or --llm-provider/--llm-model)",
            file=sys.stderr,
        )
        return 1

    from poliac.validate import format_errors_for_retry, log_rejections, validate

    try:
        client = create_client(provider_name, model_name)
        result = client.complete(TASK_INSTRUCTIONS, prompt, temperature=temperature)
    except LlmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    outcome = validate(result.recommendations, resources, metrics, provider_vocab)

    # SPEC §6.2: on any V1-V7 failure, retry once with the specific errors appended.
    if outcome.rejected and config.llm.max_retries > 0:
        errors_text = format_errors_for_retry(outcome.rejected)
        try:
            corrected = client.complete_with_correction(
                TASK_INSTRUCTIONS, prompt, result, errors_text, temperature=temperature
            )
        except LlmError as e:
            print(f"warning: retry call failed ({e}); keeping first-pass results", file=sys.stderr)
        else:
            result = corrected
            outcome = validate(result.recommendations, resources, metrics, provider_vocab)

    log_rejections(workspace_root, outcome.rejected)

    output = {
        "recommendations": [
            {**vr.recommendation.model_dump(), "applicable": vr.applicable}
            for vr in outcome.valid
        ],
        "overall_assessment": result.overall_assessment,
    }
    print(json.dumps(output, indent=2))
    if outcome.rejected:
        print(
            f"\n{len(outcome.rejected)} recommendation(s) failed validation and were dropped "
            f"-- see .poliac/rejected.jsonl",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"poliac {__version__}")
        return 0

    if args.stdio:
        from poliac.server import run_stdio

        run_stdio()
        return 0

    if args.command == "inspect":
        return _cmd_inspect(args)

    if args.command == "analyse":
        return _cmd_analyse(args)

    if args.command is None:
        parser.print_help()
        return 1

    print(f"'{args.command}' is not implemented yet", file=sys.stderr)
    return 1
