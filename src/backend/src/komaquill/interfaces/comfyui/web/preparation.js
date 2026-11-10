// Keep preparation state handling independent of ComfyUI and the DOM.
export const terminal = new Set(["ready", "failed", "cancelled"]);
const capacity = new Intl.NumberFormat("en-US", {
  style: "unit", unit: "byte", unitDisplay: "long", maximumFractionDigits: 0
});
export function assetRows(job, unknown) {
  return job.assets.map((asset) => [asset.filename, asset.size_bytes == null ? unknown : capacity.format(asset.size_bytes),
    asset.url || asset.repo_id, job.reused.includes(`${asset.sha256}/${asset.filename}`)].join(" | "));
}
function isTemporary(error) {
  return error instanceof TypeError || error.retryable === true;
}

export async function prepareWorkflow({request, workflow, confirm, status, text, sleep, signal}) {
  let job = await request("/requests", {workflow});
  let previous = null;
  let confirmed = false;
  for (;;) {
    const current = `${job.phase}:${job.current_asset || ""}`;
    if (current !== previous) status(job.phase, job);
    previous = current;
    if (terminal.has(job.phase)) return job;
    if (job.phase === "confirmation") {
      const description = job.unsupported.length ? job.unsupported.join("\n") : assetRows(job, text["ui.unknown_size"]).join("\n");
      if (!confirmed) {
        if (job.unsupported.length || !(await confirm(`${text["ui.confirm"]}\n${description}`))) {
          return request(`/requests/${job.request_id}`, {action: "cancel"});
        }
        confirmed = true;
      }
      try {
        await request(`/requests/${job.request_id}`, {action: "confirm"});
      } catch (error) {
        if (!isTemporary(error)) throw error;
      }
    }
    for (;;) {
      await sleep();
      signal?.throwIfAborted();
      let data;
      try {
        data = await request("/requests");
      } catch (error) {
        if (!isTemporary(error)) throw error;
        continue;
      }
      const next = data.requests.find((item) => item.request_id === job.request_id);
      if (!next) throw new Error("request_not_found");
      job = next;
      break;
    }
  }
}

export async function applyPreparedModels(app, job, isCurrent) {
  if (!isCurrent()) return false;
  await app.refreshComboInNodes();
  if (!isCurrent()) return false;
  function apply(graph, prefix = "") {
    for (const node of graph.nodes) {
      if (node.mode === 2 || node.mode === 4) continue;
      const identity = prefix + node.id;
      const references = job.node_models?.[identity] || {};
      for (const widget of node.widgets || []) {
        if (Object.hasOwn(references, widget.value)) widget.value = references[widget.value];
      }
      if (node.subgraph) apply(node.subgraph, identity + ":");
    }
  }
  apply(app.rootGraph);
  if (app.refreshMissingModels) await app.refreshMissingModels({silent: true, reloadDefs: false});
  return true;
}
