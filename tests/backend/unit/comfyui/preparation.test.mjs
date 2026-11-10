import test from "node:test";
import assert from "node:assert/strict";
import { applyPreparedModels, assetRows, prepareWorkflow } from "../../../../src/backend/src/komaquill/interfaces/comfyui/web/preparation.js";
const text = {"ui.unknown_size": "unknown", "ui.confirm": "Prepare?"};
test("model plan preserves unknown capacity and content identity", () => {
  assert.deepEqual(assetRows({assets: [{filename: "same.sft", sha256: "a", url: "https://source"}], reused: ["a/same.sft"]}, "unknown"), ["same.sft | unknown | https://source | true"]);
});
for (const mode of ["confirm", "cancel", "unsupported", "missing"]) {
  test(`preparation handles ${mode}`, async () => {
    const calls = [];
    const request = async (path, data) => {
      calls.push(data);
      if (path === "/requests" && !data) return {requests: mode === "missing" ? [] : [{request_id: "id", phase: "ready"}]};
      return {request_id: "id", phase: data?.action === "cancel" ? "cancelled" : "confirmation", assets: [], reused: [], unsupported: mode === "unsupported" ? ["custom"] : []};
    };
    const action = () => prepareWorkflow({request, workflow: {}, confirm: async () => mode !== "cancel", status: () => {}, text, sleep: async () => {}});
    if (mode === "missing") await assert.rejects(action);
    else assert.equal((await action()).phase, mode === "confirm" ? "ready" : "cancelled");
    assert.equal(calls[1].action, ["cancel", "unsupported"].includes(mode) ? "cancel" : "confirm");
  });
}

test("lost confirmation is reconciled through the same request", async () => {
  const calls = [];
  let polls = 0;
  let confirmations = 0;
  const request = async (path, data) => {
    calls.push([path, data?.action]);
    if (data?.workflow) return {request_id: "id", phase: "confirmation", assets: [], reused: [], unsupported: []};
    if (data?.action === "confirm" || polls++ === 0) throw new TypeError("Network error");
    return {requests: [{request_id: "another", phase: "failed"}, {request_id: "id", phase: "ready"}]};
  };
  const job = await prepareWorkflow({request, workflow: {}, confirm: async () => ++confirmations === 1,
    status: () => {}, text, sleep: async () => {}});
  assert.equal(job.phase, "ready");
  assert.equal(confirmations, 1);
  assert.deepEqual(calls.map(([path, action]) => action || path), ["/requests", "confirm", "/requests", "/requests"]);
});

test("model capacity has units and grouping", () => {
  assert.equal(assetRows({assets: [{filename: "model.sft", sha256: "a", size_bytes: 1048576, url: "https://source"}], reused: []}, "unknown")[0], "model.sft | 1,048,576 bytes | https://source | false");
});

for (const refreshSupported of [true, false]) {
  test(`prepared node references remain distinct and refresh missing-model state (${refreshSupported})`, async () => {
    const nodes = [{id: 1, widgets: [{value: "same.sft"}, {value: "keep"}]}, {id: 2, widgets: [{value: "same.sft"}]}, {id: 3}];
    let refreshed = false;
    const app = {rootGraph: {nodes}, refreshComboInNodes: async () => {}};
    if (refreshSupported) app.refreshMissingModels = async (options) => {
      assert.deepEqual(options, {silent: true, reloadDefs: false});
      assert.equal(nodes[1].widgets[0].value, "b/same.sft");
      refreshed = true;
    };
    await applyPreparedModels(app, {node_models: {1: {"same.sft": "a/same.sft"}, 2: {"same.sft": "b/same.sft"}}}, () => true);
    assert.equal(nodes[0].widgets[0].value, "a/same.sft");
    assert.equal(nodes[1].widgets[0].value, "b/same.sft");
    assert.equal(nodes[0].widgets[1].value, "keep");
    assert.equal(refreshed, refreshSupported);
  });
}

test("prepared models do not change a different workflow after refresh", async () => {
  const node = {id: 1, widgets: [{value: "same.sft"}]};
  let current = true;
  const app = {
    rootGraph: {nodes: [node]},
    refreshComboInNodes: async () => { current = false; },
  };
  const job = {node_models: {1: {"same.sft": "a/same.sft"}}};
  assert.equal(await applyPreparedModels(app, job, () => current), false);
  assert.equal(node.widgets[0].value, "same.sft");
});
