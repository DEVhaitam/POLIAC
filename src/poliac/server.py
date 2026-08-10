"""pygls LSP server.

Analysis is never automatic on keystroke -- each run is a paid, slow API call. It's triggered
by `workspace/executeCommand` -> `poliac.analyse` (the primary path), optionally by
`textDocument/didSave` when `analyse_on_save: true`. `textDocument/didChange` clears our
diagnostics for that document rather than re-running anything -- stale advice against edited
code is misleading.

We only analyse the single document the command/save targets, not the whole workspace -- the
examples this PoC ships are single-file, and multi-file support is a straightforward extension
(parse every open `.tf` file instead of one) if/when it's actually needed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from poliac import __version__
from poliac.config import Config, load_config
from poliac.llm.client import LlmError, create_client
from poliac.llm.context import TASK_INSTRUCTIONS, build_context
from poliac.llm.schema import Recommendation
from poliac.metrics.table import MetricsError, load_metrics
from poliac.providers.registry import load_providers_for_resources
from poliac.terraform.model import Resource, SourceRange
from poliac.terraform.parser import TerraformParseError, parse_source
from poliac.validate import ValidationOutcome, format_errors_for_retry, log_rejections, validate

SERVER_NAME = "poliac"

server = LanguageServer(SERVER_NAME, __version__)


@dataclass
class ServerState:
    config: Config
    workspace_root: Path
    metrics_override: str | None = None
    contract_override: str | None = None


@dataclass
class DiagnosticEntry:
    range: lsp.Range
    block_range: lsp.Range
    message: str
    recommendation: Recommendation
    applicable: bool
    model: str


@dataclass
class DocumentAnalysis:
    entries: list[DiagnosticEntry]
    overall_assessment: str


_state: ServerState | None = None
_analyses: dict[str, DocumentAnalysis] = {}  # keyed by document URI


def _to_lsp_range(r: SourceRange) -> lsp.Range:
    return lsp.Range(
        start=lsp.Position(line=r.start_line, character=r.start_char),
        end=lsp.Position(line=r.end_line, character=r.end_char),
    )


def _uri_to_path(uri: str) -> Path:
    fs_path = to_fs_path(uri)
    if fs_path is None:
        raise ValueError(f"cannot resolve URI to a filesystem path: {uri}")
    return Path(fs_path)


def _workspace_root(params: lsp.InitializeParams) -> Path:
    if params.root_path:
        return Path(params.root_path)
    if params.root_uri:
        fs_path = to_fs_path(params.root_uri)
        if fs_path:
            return Path(fs_path)
    return Path.cwd()


def _info(ls: LanguageServer, message: str) -> None:
    ls.window_show_message(lsp.ShowMessageParams(type=lsp.MessageType.Info, message=f"poliac: {message}"))


def _error(ls: LanguageServer, message: str) -> None:
    ls.window_show_message(lsp.ShowMessageParams(type=lsp.MessageType.Error, message=f"poliac: {message}"))


@server.feature(lsp.INITIALIZE)
def on_initialize(ls: LanguageServer, params: lsp.InitializeParams) -> None:
    global _state
    root = _workspace_root(params)
    result = load_config(root)
    _state = ServerState(config=result.config, workspace_root=root)
    for note in result.assumptions:
        _info(ls, note)


def _first_sentence(text: str) -> str:
    text = text.strip()
    idx = text.find(". ")
    return text[: idx + 1] if idx != -1 else text


def _diagnostic_message(rec: Recommendation) -> str:
    if rec.action == "no_change":
        return f"no change recommended for {rec.attribute} -- {_first_sentence(rec.rationale)}"
    return f"consider {rec.attribute} = {rec.recommended_value} -- {_first_sentence(rec.rationale)}"


def _build_document_analysis(
    outcome: ValidationOutcome,
    resources: list[Resource],
    model: str,
    overall_assessment: str,
) -> DocumentAnalysis:
    resources_by_address = {r.address: r for r in resources}
    entries: list[DiagnosticEntry] = []
    for vr in outcome.valid:
        rec = vr.recommendation
        resource = resources_by_address.get(rec.resource_address)
        attr = resource.attributes.get(rec.attribute) if resource else None
        if resource is None or attr is None:
            continue
        entries.append(
            DiagnosticEntry(
                range=_to_lsp_range(attr.value_range),
                block_range=_to_lsp_range(resource.block_range),
                message=_diagnostic_message(rec),
                recommendation=rec,
                applicable=vr.applicable,
                model=model,
            )
        )
    return DocumentAnalysis(entries=entries, overall_assessment=overall_assessment)


def _publish_diagnostics(ls: LanguageServer, uri: str) -> None:
    analysis = _analyses.get(uri)
    diagnostics = (
        [
            lsp.Diagnostic(
                range=entry.range,
                message=entry.message,
                severity=lsp.DiagnosticSeverity.Information,
                source=SERVER_NAME,
            )
            for entry in analysis.entries
        ]
        if analysis
        else []
    )
    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def _run_analysis(ls: LanguageServer, uri: str) -> None:
    if _state is None:
        _error(ls, "server not initialised")
        return
    config = _state.config

    token = str(uuid.uuid4())
    ls.window_work_done_progress_create(lsp.WorkDoneProgressCreateParams(token=token))
    ls.progress(
        lsp.ProgressParams(
            token=token,
            value=lsp.WorkDoneProgressBegin(title="poliac: analysing", cancellable=False),
        )
    )
    try:
        _run_analysis_inner(ls, uri, config)
    finally:
        ls.progress(lsp.ProgressParams(token=token, value=lsp.WorkDoneProgressEnd()))


def _run_analysis_inner(ls: LanguageServer, uri: str, config: Config) -> None:
    assert _state is not None

    metrics_rel = _state.metrics_override or config.metrics.path
    contract_rel = _state.contract_override or config.contract.path
    if not metrics_rel:
        _error(ls, "metrics.path is not set (.poliac.yaml, or poliac.setMetricsFile)")
        return
    if not contract_rel:
        _error(ls, "contract.path is not set (.poliac.yaml, or poliac.setContractFile)")
        return
    if not config.llm.provider or not config.llm.model:
        _error(ls, "llm.provider / llm.model are not set in .poliac.yaml")
        return

    try:
        file_path = _uri_to_path(uri)
    except ValueError as e:
        _error(ls, str(e))
        return

    doc = ls.workspace.get_text_document(uri)
    source_text = doc.source

    try:
        resources = parse_source(source_text.encode(), file=str(file_path))
    except TerraformParseError as e:
        _error(ls, str(e))
        return

    metrics_path = (_state.workspace_root / metrics_rel).resolve()
    try:
        metrics = load_metrics(
            metrics_path, max_rows=config.metrics.max_rows, max_bytes=config.metrics.max_bytes
        )
    except MetricsError as e:
        _error(ls, str(e))
        return

    contract_path = (_state.workspace_root / contract_rel).resolve()
    if not contract_path.is_file():
        _error(ls, f"contract file not found: {contract_path}")
        return
    contract_text = contract_path.read_text()

    provider_vocab, missing_providers = load_providers_for_resources(resources)
    if missing_providers:
        _info(
            ls,
            f"no provider vocabulary for {missing_providers!r}; type/catalog checks (V6) "
            "won't run for those resources' attributes",
        )

    prompt = build_context(
        {str(file_path): source_text}, resources, metrics, contract_text, provider_vocab
    )

    try:
        client = create_client(config.llm.provider, config.llm.model)
        result = client.complete(TASK_INSTRUCTIONS, prompt, temperature=config.llm.temperature)
    except LlmError as e:
        _error(ls, str(e))
        return

    outcome = validate(result.recommendations, resources, metrics, provider_vocab)

    if outcome.rejected and config.llm.max_retries > 0:
        errors_text = format_errors_for_retry(outcome.rejected)
        try:
            corrected = client.complete_with_correction(
                TASK_INSTRUCTIONS, prompt, result, errors_text, temperature=config.llm.temperature
            )
        except LlmError:
            pass  # keep the first-pass result rather than fail the whole analysis
        else:
            result = corrected
            outcome = validate(result.recommendations, resources, metrics, provider_vocab)

    log_rejections(_state.workspace_root, outcome.rejected)

    _analyses[uri] = _build_document_analysis(
        outcome, resources, config.llm.model, result.overall_assessment
    )
    _publish_diagnostics(ls, uri)

    if outcome.rejected:
        _info(
            ls,
            f"{result.overall_assessment} "
            f"({len(outcome.rejected)} recommendation(s) dropped -- see .poliac/rejected.jsonl)",
        )
    else:
        _info(ls, result.overall_assessment)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(ls: LanguageServer, params: lsp.DidChangeTextDocumentParams) -> None:
    uri = params.text_document.uri
    if _analyses.pop(uri, None) is not None:
        _publish_diagnostics(ls, uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def on_did_save(ls: LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    if _state is not None and _state.config.analyse_on_save:
        _run_analysis(ls, params.text_document.uri)


@server.command("poliac.analyse")
def cmd_analyse(ls: LanguageServer, uri: str) -> None:
    # NB: pygls unpacks `ExecuteCommandParams.arguments` positionally into this handler's
    # named parameters (after `ls`) -- it does NOT pass `arguments` through as a list. One
    # argument in, one plain `str` parameter here, not `args: list`.
    _run_analysis(ls, uri)


@server.command("poliac.setMetricsFile")
def cmd_set_metrics_file(ls: LanguageServer, path: str) -> None:
    if _state is None:
        return
    _state.metrics_override = path
    _info(ls, f"metrics file set to {path}")


@server.command("poliac.setContractFile")
def cmd_set_contract_file(ls: LanguageServer, path: str) -> None:
    if _state is None:
        return
    _state.contract_override = path
    _info(ls, f"contract file set to {path}")


def _position_in_range(pos: lsp.Position, r: lsp.Range) -> bool:
    if (pos.line, pos.character) < (r.start.line, r.start.character):
        return False
    if (pos.line, pos.character) > (r.end.line, r.end.character):
        return False
    return True


def _hover_markdown(entry: DiagnosticEntry) -> str:
    rec = entry.recommendation
    header = (
        f"**poliac** -- no change recommended for `{rec.attribute}`"
        if rec.action == "no_change"
        else f"**poliac** -- {rec.action} `{rec.attribute}` to `{rec.recommended_value}`"
    )
    lines = [
        header,
        "",
        rec.rationale,
        "",
        f"Confidence: **{rec.confidence}** &nbsp;·&nbsp; Model: `{entry.model}`",
        "",
        "| row | column | value |",
        "|---|---|---|",
    ]
    lines += [f"| {c.row} | {c.column} | {c.value} |" for c in rec.citations]
    if not entry.applicable:
        lines += ["", "_Target is not a literal value -- advice only, no quick fix available._"]
    return "\n".join(lines)


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def on_hover(ls: LanguageServer, params: lsp.HoverParams) -> lsp.Hover | None:
    analysis = _analyses.get(params.text_document.uri)
    if analysis is None:
        return None
    for entry in analysis.entries:
        if _position_in_range(params.position, entry.range):
            return lsp.Hover(
                contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=_hover_markdown(entry)),
                range=entry.range,
            )
    return None


def _ranges_overlap(a: lsp.Range, b: lsp.Range) -> bool:
    a_start, a_end = (a.start.line, a.start.character), (a.end.line, a.end.character)
    b_start, b_end = (b.start.line, b.start.character), (b.end.line, b.end.character)
    return a_start <= b_end and b_start <= a_end


@server.feature(lsp.TEXT_DOCUMENT_CODE_ACTION)
def on_code_action(ls: LanguageServer, params: lsp.CodeActionParams) -> list[lsp.CodeAction]:
    analysis = _analyses.get(params.text_document.uri)
    if analysis is None:
        return []

    actions = []
    for entry in analysis.entries:
        rec = entry.recommendation
        if not entry.applicable or rec.action == "no_change":
            continue  # code action only when applicable=True
        if not _ranges_overlap(params.range, entry.range):
            continue
        edit = lsp.WorkspaceEdit(
            changes={
                params.text_document.uri: [
                    lsp.TextEdit(range=entry.range, new_text=rec.recommended_value)
                ]
            }
        )
        actions.append(
            lsp.CodeAction(
                title=f"Apply: {rec.attribute} = {rec.recommended_value}",
                kind=lsp.CodeActionKind.QuickFix,
                edit=edit,
                is_preferred=True,
            )
        )
    return actions


@server.feature(lsp.TEXT_DOCUMENT_CODE_LENS)
def on_code_lens(ls: LanguageServer, params: lsp.CodeLensParams) -> list[lsp.CodeLens]:
    analysis = _analyses.get(params.text_document.uri)
    if not analysis or not analysis.entries:
        return []

    by_block: dict[tuple[int, int], list[DiagnosticEntry]] = {}
    for entry in analysis.entries:
        key = (entry.block_range.start.line, entry.block_range.start.character)
        by_block.setdefault(key, []).append(entry)

    lenses = []
    for entries in by_block.values():
        start = entries[0].block_range.start
        n = len(entries)
        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(start=start, end=start),
                command=lsp.Command(title=f"{n} recommendation{'s' if n != 1 else ''}", command=""),
            )
        )
    return lenses


def run_stdio() -> None:
    server.start_io()
