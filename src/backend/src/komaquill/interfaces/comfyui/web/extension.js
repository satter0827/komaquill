import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { applyPreparedModels, prepareWorkflow } from "./preparation.js";

app.registerExtension({
  name: "komaquill.app-mode-layout",
  setup() {
    // App Mode's output gallery must not set the flex item's minimum width.
    const style = document.createElement("style");
    style.textContent = '[data-testid="linear-workspace-column"] > .p-splitter { min-inline-size: 0; }';
    document.head.append(style);
  }
});

let text = {};
const POLL_INTERVAL_MS = 1000;
let activeRequest = null;
let running = false;
async function request(path, data, signal) {
  const response = await api.fetchApi(`/komaquill/v1${path}`, {signal, ...(data === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)
  })});
  if (!response.ok) {
    const error = new Error(`http_${response.status}`);
    error.retryable = response.status === 429 || response.status >= 500;
    throw error;
  }
  return response.json();
}
function notify(key, severity = "info", detail) {
  app.extensionManager.toast.add({severity, summary: text[key] || key, detail, life: 5000});
}
async function prepare() {
  if (running) return;
  running = true;
  try {
    const graph = app.rootGraph;
    const nodes = [...graph.nodes];
    const isCurrent = () => app.rootGraph === graph && nodes.length === graph.nodes.length &&
      nodes.every((node) => graph.getNodeById(node.id) === node);
    const workflow = graph.serialize();
    const capabilities = await request("/capabilities");
    text = capabilities.messages;
    if (!capabilities.workspace_id || !capabilities.accepting) return notify("ui.unavailable", "warn");
    const signal = AbortSignal.timeout(Math.ceil(capabilities.preparation_timeout * 1000));
    const job = await prepareWorkflow({request: (path, data) => request(path, data, signal),
      workflow, text, signal,
      confirm: async (message) => window.confirm(message),
      status: (phase, job) => {
        activeRequest = job.request_id;
        if (phase === "requested") notify("ui.preparation_waiting");
        if (phase === "preparing") notify("ui.preparing", "info", job.current_asset?.split("/").at(-1));
      },
      sleep: () => new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))});
    if (job.phase === "ready") {
      const applied = await applyPreparedModels(app, job, isCurrent);
      notify(applied ? "ui.models_ready" : "ui.models_ready_other_workflow");
    } else if (job.unsupported.length) {
      notify("ui.preparation_unsupported", "warn", job.unsupported.join(", "));
    } else if (job.phase === "failed") {
      const details = {
        preparation_timeout: text["ui.preparation_timeout"],
        connection_failed: text["ui.preparation_connection"],
        authentication_failed: text["ui.preparation_authentication"],
        invalid_output: text["ui.preparation_integrity"],
        storage_failed: text["ui.preparation_storage"],
      };
      notify("ui.preparation_failed", "warn", details[job.failure?.code]);
    }
  } catch (error) {
    notify("ui.preparation_failed", "error",
      error.name === "TimeoutError" ? text["ui.preparation_timeout"] : undefined);
  }
  finally { activeRequest = null; running = false; }
}
api.addEventListener("execution_success", () => notify("ui.generated"));
api.addEventListener("komaquill_saved", (event) => {
  const keys = {saving: "ui.saving", saved: "ui.saved", retry: "ui.retry"};
  const key = keys[event.detail?.state];
  if (key) notify(key, event.detail.state === "retry" ? "warn" : "success");
});
let subscribed = null;
async function subscribe() {
  if (!api.clientId || subscribed === api.clientId || !text["ui.saved"]) return;
  await request(`/capabilities?client_id=${encodeURIComponent(api.clientId)}`);
  subscribed = api.clientId;
}
api.addEventListener("status", (event) => {
  if (!event.detail) subscribed = null;
  else subscribe().catch(() => notify("ui.unavailable", "warn"));
});
text = (await request("/capabilities")).messages;
await subscribe();
const cancel = async () => {
  if (activeRequest) await request(`/requests/${activeRequest}`, {action: "cancel"});
};
app.registerExtension({
  name: "komaquill.workspace",
  commands: [{id: "komaquill.prepare", label: text["ui.prepare"], function: prepare},
    {id: "komaquill.cancel", label: text["ui.cancel"], function: cancel}],
  actionBarButtons: [
    {icon: "icon-[lucide--package-check]", label: text["ui.prepare"], onClick: prepare},
    {icon: "icon-[lucide--circle-stop]", tooltip: text["ui.cancel"], onClick: cancel}
  ],
  menuCommands: [{path: ["Workflow"], commands: ["komaquill.prepare", "komaquill.cancel"]}]
});
