import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;
let diagnostics: vscode.OutputChannel | undefined;

function describeCommand(command: string): string {
  if (!fs.existsSync(command)) {
    return `does not exist at this path`;
  }
  try {
    fs.accessSync(command, fs.constants.X_OK);
    return "exists, executable";
  } catch {
    return "exists, but NOT executable (check permissions)";
  }
}

export function activate(context: vscode.ExtensionContext): void {
  diagnostics = vscode.window.createOutputChannel("poliac (client diagnostics)");
  context.subscriptions.push(diagnostics);

  const config = vscode.workspace.getConfiguration("poliac");
  const command = config.get<string>("serverCommand", "poliac");
  const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

  diagnostics.appendLine(`[activate] serverCommand setting: ${JSON.stringify(command)}`);
  diagnostics.appendLine(`[activate] command on disk: ${describeCommand(command)}`);
  diagnostics.appendLine(`[activate] workspace folder (cwd for spawn): ${cwd ?? "(none)"}`);
  diagnostics.appendLine(`[activate] node child_process will run: ${command} --stdio`);

  const serverOptions: ServerOptions = {
    command,
    args: ["--stdio"],
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: "file", language: "terraform" }],
  };

  client = new LanguageClient("poliac", "poliac", serverOptions, clientOptions);

  // Command IDs here are deliberately NOT "poliac.analyse" / "poliac.setMetricsFile" /
  // "poliac.setContractFile" -- those are the server's `executeCommandProvider` names, and
  // vscode-languageclient's built-in ExecuteCommandFeature auto-registers a passthrough VS Code
  // command for each one during client.start(). Reusing the same IDs here collides with that
  // auto-registration ("command '...' already exists") and crashes startup. These IDs exist
  // only so the Command Palette entries can attach richer client-side behavior (grab the active
  // editor's URI, show a file picker) before forwarding to the same server command by name.
  context.subscriptions.push(
    vscode.commands.registerCommand("poliac.analyseCurrentFile", () => runAnalyse()),
    vscode.commands.registerCommand("poliac.pickMetricsFile", () => setWorkspaceRelativeFile("poliac.setMetricsFile", "Select metrics CSV file")),
    vscode.commands.registerCommand("poliac.pickContractFile", () => setWorkspaceRelativeFile("poliac.setContractFile", "Select contract file"))
  );

  diagnostics.appendLine("[activate] calling client.start()...");
  client.start().then(
    () => {
      diagnostics?.appendLine("[activate] client.start() resolved -- connected.");
      context.subscriptions.push(new vscode.Disposable(() => client?.stop()));
    },
    (err: unknown) => {
      const e = err as { message?: string; stack?: string } | undefined;
      diagnostics?.appendLine(`[activate] client.start() REJECTED`);
      diagnostics?.appendLine(`  message: ${e?.message ?? String(err)}`);
      diagnostics?.appendLine(`  stack: ${e?.stack ?? "(no stack)"}`);
      diagnostics?.appendLine(`  raw: ${JSON.stringify(err, Object.getOwnPropertyNames(err ?? {}))}`);
      diagnostics?.show(true);
      vscode.window.showErrorMessage(`poliac: failed to start server (${command} --stdio) -- see "poliac (client diagnostics)" output`);
    }
  );
}

export function deactivate(): Thenable<void> | undefined {
  return client?.stop();
}

// The server's `--stdio` process is spawned once at activation; it stays cold until this
// fires (workspace/executeCommand -> poliac.analyse), because each analysis is a paid LLM
// call, never something we trigger implicitly from the client side.
async function runAnalyse(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage("poliac: no active editor");
    return;
  }
  if (editor.document.languageId !== "terraform") {
    vscode.window.showWarningMessage("poliac: active file is not a Terraform document");
    return;
  }
  if (!client) {
    vscode.window.showErrorMessage("poliac: language client is not running");
    return;
  }
  await client.sendRequest("workspace/executeCommand", {
    command: "poliac.analyse",
    arguments: [editor.document.uri.toString()],
  });
}

// Server-side, `metrics.path` / `contract.path` are resolved relative to the workspace root
// (see server.py's `_run_analysis_inner`), so the override we send here must be workspace-
// relative too, not an absolute filesystem path.
async function setWorkspaceRelativeFile(command: string, title: string): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    vscode.window.showWarningMessage("poliac: open a workspace folder first");
    return;
  }
  if (!client) {
    vscode.window.showErrorMessage("poliac: language client is not running");
    return;
  }

  const picked = await vscode.window.showOpenDialog({
    title,
    defaultUri: folder.uri,
    canSelectMany: false,
  });
  if (!picked || picked.length === 0) {
    return;
  }

  const relative = path.relative(folder.uri.fsPath, picked[0].fsPath);
  await client.sendRequest("workspace/executeCommand", {
    command,
    arguments: [relative],
  });
}
