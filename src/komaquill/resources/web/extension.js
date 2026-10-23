import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { prepareWorkflow } from "./preparation.mjs";

let text = {};
const POLL_INTERVAL_MS = 1000;
let activeRequest = null;
let running = false;
async function request(path, data) {
  const response = await api.fetchApi(`/komaquill/v1${path}`, data === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)
  });
  if (!response.ok) throw new Error(`http_${response.status}`);
  return response.json();
}
function notify(key, severity = "info") {
  app.extensionManager.toast.add({severity, summary: text[key] || key, life: 5000});
}
async function prepare() {
  if (running) return;
  running = true;
  try {
    const capabilities = await request("/capabilities");
    text = capabilities.messages;
    if (!capabilities.workspace_id || !capabilities.accepting) return notify("ui.unavailable", "warn");
    const job = await prepareWorkflow({request, workflow: app.graph.serialize(), text,
      confirm: async (message) => window.confirm(message),
      status: (phase, job) => { activeRequest = job?.request_id; if (phase === "preparing") notify("ui.preparing"); },
      sleep: () => new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))});
    if (job.phase === "ready") {
      await app.refreshComboInNodes();
      // Content-addressed references distinguish equal filenames with different bytes.
      for (const node of app.graph._nodes) {
        for (const widget of node.widgets || []) {
          const asset = job.assets.find((item) => item.filename === widget.value);
          if (asset) widget.value = `${asset.sha256}/${asset.filename}`;
        }
      }
      notify("ui.saved");
    } else notify("ui.retry", "warn");
  } catch { notify("ui.retry", "error"); }
  finally { running = false; }
}
text = (await request("/capabilities")).messages;
api.addEventListener("execution_success", () => notify("ui.generated"));
api.addEventListener("komaquill_saved", (event) => {
  const keys = {saving: "ui.saving", saved: "ui.saved", retry: "ui.retry"};
  const key = keys[event.detail?.state];
  if (key) notify(key, event.detail.state === "retry" ? "warn" : "success");
});
app.registerExtension({
  name: "komaquill.workspace",
  commands: [{id: "komaquill.prepare", label: text["ui.prepare"], function: prepare},
    {id: "komaquill.cancel", label: text["ui.cancel"], function: async () => { if (activeRequest) await request(`/requests/${activeRequest}`, {action: "cancel"}); }}],
  menuCommands: [{path: ["Workflow"], commands: ["komaquill.prepare", "komaquill.cancel"]}],
  async afterConfigureGraph() {
    if (!app.refreshMissingModels) return;
    const missing = await app.refreshMissingModels({silent: true});
    if (missing?.missingModels?.length) await prepare();
  }
});
