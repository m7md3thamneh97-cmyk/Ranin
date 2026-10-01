// Provider events are presentation signals, never authorization to create learning.
// Durable evidence and confirmation remain owned by the backend sideband.
export function semanticEvents(event) {
  const id =
    event.item_id || event.item?.id || event.response_id || event.response?.id;
  const common = { id, provider: "openai" };
  switch (event.type) {
    case "raneen.connected":
      return [{ type: "session.started" }];
    case "input_audio_buffer.speech_started":
      return [{ ...common, type: "trainer.speech_started" }];
    case "conversation.item.input_audio_transcription.delta":
      return [
        { ...common, type: "trainer.turn_partial", text: event.delta || "" },
      ];
    case "conversation.item.input_audio_transcription.completed":
      return [
        {
          ...common,
          type: "trainer.turn_completed",
          text: event.transcript || "",
        },
      ];
    case "response.created":
      return [{ ...common, type: "raneen.thinking" }];
    case "output_audio_buffer.started":
      return [{ ...common, type: "raneen.turn_started" }];
    case "response.output_audio_transcript.delta":
    case "response.audio_transcript.delta":
      return [
        { ...common, type: "raneen.turn_partial", text: event.delta || "" },
      ];
    case "response.output_audio_transcript.done":
    case "response.audio_transcript.done":
      return [
        {
          ...common,
          type: "raneen.turn_completed",
          text: event.transcript || "",
        },
      ];
    case "output_audio_buffer.cleared":
      return [{ ...common, type: "raneen.interrupted" }];
    case "output_audio_buffer.stopped":
      return [{ ...common, type: "raneen.speech_ended" }];
    case "error":
      return [{ type: "error" }];
    default:
      return [];
  }
}
export class ConversationFeed {
  constructor(limit = 60) {
    this.limit = limit;
    this.reset();
  }
  reset() {
    this.turns = [];
    this.seen = new Set();
    this.speaker = "";
    this.connection = 0;
  }
  apply(event) {
    if (event.type === "session.started") {
      this.connection++;
      this.speaker = "";
      return;
    }
    if (event.type === "trainer.speech_started") {
      this.speaker = "trainer";
      return;
    }
    if (event.type === "raneen.thinking") {
      this.speaker = "thinking";
      return;
    }
    if (event.type === "raneen.turn_started") {
      this.speaker = "raneen";
      return;
    }
    if (
      [
        "raneen.interrupted",
        "raneen.speech_ended",
        "error",
        "session.ended",
      ].includes(event.type)
    ) {
      this.speaker = "";
      return;
    }
    if (
      !/^(trainer|raneen)\.turn_(partial|completed)$/.test(event.type) ||
      !event.id
    )
      return;
    const role = event.type.split(".")[0],
      key = `${this.connection}:${role}:${event.id}`,
      final = event.type.endsWith("completed");
    let turn = this.turns.find((x) => x.key === key);
    if (turn?.final) return;
    if (!turn) {
      if (this.seen.has(key)) return;
      turn = { key, role, text: "", final: false };
      this.turns.push(turn);
    }
    turn.text = (final ? event.text : turn.text + event.text).slice(0, 6000);
    turn.final = final;
    if (final) this.seen.add(key);
    if (this.turns.length > this.limit)
      this.turns.splice(0, this.turns.length - this.limit);
    if (this.seen.size > this.limit * 4)
      this.seen = new Set([...this.seen].slice(-this.limit * 2));
    if (role === "trainer" && final) this.speaker = "thinking";
  }
}
