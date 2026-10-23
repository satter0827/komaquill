// Keep preparation state handling independent of ComfyUI and the DOM.
export const terminal = new Set(["ready", "failed", "cancelled"]);
const capacity = new Intl.NumberFormat("en-US", {
  style: "unit", unit: "byte", unitDisplay: "long", maximumFractionDigits: 0
});
export function assetRows(job, unknown) {
  return job.assets.map((asset) => [asset.filename, asset.size_bytes == null ? unknown : capacity.format(asset.size_bytes),
    asset.url || asset.repo_id, job.reused.includes(`${asset.sha256}/${asset.filename}`)].join(" | "));
}
export async function prepareWorkflow({request, workflow, confirm, status, text, sleep}) {
  let job = await request("/requests", {workflow});
  let previous = null;
  for (;;) {
    const current = `${job.phase}:${job.current_asset || ""}`;
    if (current !== previous) status(job.phase, job);
    previous = current;
    if (terminal.has(job.phase)) return job;
    if (job.phase === "confirmation") {
      const description = job.unsupported.length ? job.unsupported.join("\n") : assetRows(job, text["ui.unknown_size"]).join("\n");
      if (job.unsupported.length || !(await confirm(`${text["ui.confirm"]}\n${description}`))) {
        return request(`/requests/${job.request_id}`, {action: "cancel"});
      }
      await request(`/requests/${job.request_id}`, {action: "confirm"});
    }
    await sleep();
    const data = await request("/requests");
    const next = data.requests.find((item) => item.request_id === job.request_id);
    if (!next) throw new Error("request_not_found");
    job = next;
  }
}
