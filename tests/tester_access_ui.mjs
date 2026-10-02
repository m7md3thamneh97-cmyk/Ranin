import assert from "node:assert/strict";
import { test } from "node:test";
import { signInWithCode } from "../studio/static/tester-access-ui.js";

test("existing tester access code resumes without redeeming another invitation", async () => {
  const set = [];
  const result = await signInWithCode({
    code: "synthetic-return-code", setToken: value => set.push(value),
    request: async path => { assert.equal(path, "/api/me"); return { id: "tester1", role: "tester" }; },
    post: async () => { throw Error("No invitation redemption on valid access"); },
  });
  assert.deepEqual(set, ["synthetic-return-code"]);
  assert.equal(result.user.role, "tester");
  assert.equal(result.newToken, undefined);
});

test("one-use invitation is exchanged for a distinct returning credential", async () => {
  const set = [];
  const result = await signInWithCode({
    code: "synthetic-invitation", setToken: value => set.push(value),
    request: async () => { throw Object.assign(Error(), { status: 401 }); },
    post: async (path, body) => {
      assert.equal(path, "/api/testing/redeem");
      assert.deepEqual(body, { code: "synthetic-invitation" });
      return { token: "synthetic-return-code", user: { id: "tester1", role: "tester" } };
    },
  });
  assert.deepEqual(set, ["synthetic-invitation", "", "synthetic-return-code"]);
  assert.equal(result.newToken, "synthetic-return-code");
});

test("authorization denial or a network failure does not redeem invitations", async () => {
  for (const status of [403, 503, undefined]) {
    const set = [];
    await assert.rejects(signInWithCode({
      code: "synthetic-code", setToken: value => set.push(value),
      request: async () => { throw Object.assign(Error("failed"), { status }); },
      post: async () => { throw Error("Must not redeem"); },
    }), { message: "failed" });
    assert.deepEqual(set, ["synthetic-code", ""]);
  }
});

test("ordinary contributor credentials cannot enter the invited tester workflow", async () => {
  await assert.rejects(signInWithCode({
    code: "synthetic-contributor", setToken() {},
    request: async () => ({ role: "contributor" }),
    post: async () => { throw Error("Must not redeem"); },
  }), { status: 403 });
});
