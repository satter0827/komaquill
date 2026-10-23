import test from "node:test";
import assert from "node:assert/strict";
import { assetRows, prepareWorkflow } from "../../src/komaquill/resources/web/preparation.mjs";
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

test("model capacity has units and grouping", () => {
  assert.equal(assetRows({assets: [{filename: "model.sft", sha256: "a", size_bytes: 1048576, url: "https://source"}], reused: []}, "unknown")[0], "model.sft | 1,048,576 bytes | https://source | false");
});
