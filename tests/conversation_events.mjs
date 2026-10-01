import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ConversationFeed,
  semanticEvents,
} from "../studio/static/conversation-events.js";
import { learningPanel, escapeHTML } from "../studio/static/conversation-ui.js";

test("partial transcript becomes one final turn, and duplicate finals cannot overwrite it", () => {
  const feed = new ConversationFeed();
  for (const e of [
    {
      type: "conversation.item.input_audio_transcription.delta",
      item_id: "a",
      delta: "هل ",
    },
    {
      type: "conversation.item.input_audio_transcription.delta",
      item_id: "a",
      delta: "السعر مناسب؟",
    },
    {
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "a",
      transcript: "هل السعر مناسب؟",
    },
    {
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "a",
      transcript: "wrong duplicate",
    },
  ])
    semanticEvents(e).forEach((x) => feed.apply(x));
  assert.equal(feed.turns.length, 1);
  assert.equal(feed.turns[0].text, "هل السعر مناسب؟");
  assert.equal(feed.turns[0].final, true);
});
test("interruption changes display activity without inventing learning or losing prior turns", () => {
  const feed = new ConversationFeed();
  semanticEvents({
    type: "response.output_audio_transcript.done",
    item_id: "b",
    transcript: "Let’s compare the location first.",
  }).forEach((e) => feed.apply(e));
  feed.apply({ type: "raneen.turn_started" });
  assert.equal(feed.speaker, "raneen");
  semanticEvents({ type: "input_audio_buffer.speech_started" }).forEach((e) =>
    feed.apply(e),
  );
  assert.equal(feed.speaker, "trainer");
  semanticEvents({ type: "output_audio_buffer.cleared" }).forEach((e) =>
    feed.apply(e),
  );
  assert.equal(feed.turns.length, 1);
  assert.deepEqual(
    semanticEvents({
      type: "response.function_call_arguments.done",
      name: "propose_evidence",
    }),
    [],
  );
});
test("reconnect scopes provider IDs, retains history, and bounds memory", () => {
  const feed = new ConversationFeed(4);
  for (let i = 0; i < 8; i++) {
    feed.apply({ type: "session.started" });
    feed.apply({
      type: "trainer.turn_completed",
      id: "same-provider-id",
      text: String(i),
    });
  }
  assert.equal(feed.turns.length, 4);
  assert.deepEqual(
    feed.turns.map((t) => t.text),
    ["4", "5", "6", "7"],
  );
  feed.reset();
  assert.equal(feed.turns.length, 0);
  assert.equal(feed.seen.size, 0);
});
test("learning panel treats ready audio as unapproved and escapes all server text", () => {
  const html = learningPanel({
    lang: "en",
    journey: { confirmed_patterns: 2 },
    workflow: { voice_state: "ready" },
    learning: {
      hypotheses: [{ key: "<img onerror=alert(1)>", state: "tentative" }],
    },
  });
  assert.ok(html.includes("Ready for you to listen"));
  assert.ok(!html.includes("Voice approved by you"));
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img"));
  assert.equal(escapeHTML('"<&'), "&quot;&lt;&amp;");
});

const { LearningConnection, learningBinding } =
  await import("../studio/static/learning-api.js");
test("learning only connects when the server explicitly binds both identities", () => {
  assert.equal(
    learningBinding({ id: "enrollment-id", profile_id: "person" }, {}),
    null,
  );
  assert.equal(
    learningBinding(
      {
        learning: {
          contract: "raneen-backend-v1",
          session_id: "../../admin",
          profile_id: "person",
        },
      },
      {},
    ),
    null,
  );
  assert.deepEqual(
    learningBinding(
      {
        learning: {
          contract: "raneen-backend-v1",
          session_id: "teaching",
          profile_id: "person",
        },
      },
      {},
    ),
    { session_id: "teaching", profile_id: "person" },
  );
});
test("a delayed profile result cannot leak into a different conversation", async () => {
  let finish;
  const conn = new LearningConnection((path) =>
    path.includes("learning-state")
      ? new Promise((r) => (finish = r))
      : Promise.resolve({ items: [], cursor: 0 }),
  );
  conn.bind({ profile_id: "a", session_id: "s1" });
  const pending = conn.refresh();
  conn.bind({ profile_id: "b", session_id: "s2" });
  finish({ profile_id: "a", hypotheses: [] });
  assert.equal(await pending, null);
  assert.equal(conn.cursor, 0);
});
test("learning follows event cursors and rejects mismatched profile payloads", async () => {
  let wrong = false;
  const paths = [];
  const conn = new LearningConnection(async (path) => {
    paths.push(path);
    return path.includes("learning-state")
      ? { profile_id: wrong ? "other" : "person", hypotheses: [] }
      : { items: [{ type: "learning.correction_recorded" }], cursor: 7 };
  });
  conn.bind({ profile_id: "person", session_id: "session" });
  assert.equal(
    (await conn.refresh()).events[0].type,
    "learning.correction_recorded",
  );
  assert.equal(conn.cursor, 7);
  wrong = true;
  await assert.rejects(() => conn.refresh(), /invalid_learning_response/);
  assert.ok(paths.some((p) => p.includes("after=7")));
  assert.equal(conn.cursor, 7);
});
