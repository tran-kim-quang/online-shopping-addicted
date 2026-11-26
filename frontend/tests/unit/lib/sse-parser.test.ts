import { describe, expect, it } from "vitest";
import { SseFrameParser, parseChatSseFrame } from "@/lib/api/sse";

describe("SseFrameParser", () => {
  it("assembles a frame split across chunks", () => {
    const parser = new SseFrameParser();

    expect(parser.feed('event: token\ndata: {"text":"Xin ')).toEqual([]);
    const frames = parser.feed('chào"}\n\n');

    expect(frames).toEqual([{ event: "token", data: '{"text":"Xin chào"}', id: undefined }]);
  });

  it("returns every event contained in one chunk", () => {
    const parser = new SseFrameParser();

    const frames = parser.feed(
      'event: session\ndata: {"session_id":"s1"}\n\nevent: token\ndata: {"text":"a"}\n\n'
    );

    expect(frames.map((frame) => frame.event)).toEqual(["session", "token"]);
  });

  it("ignores keepalive comments and blank blocks", () => {
    const parser = new SseFrameParser();

    const frames = parser.feed(': keepalive\n\nevent: token\ndata: {"text":"ok"}\n\n');

    expect(frames).toHaveLength(1);
    expect(parseChatSseFrame(frames[0]!)).toEqual({ type: "token", data: { text: "ok" } });
  });

  it("normalises CRLF line endings", () => {
    const parser = new SseFrameParser();

    const frames = parser.feed('event: token\r\ndata: {"text":"crlf"}\r\n\r\n');

    expect(frames).toEqual([{ event: "token", data: '{"text":"crlf"}', id: undefined }]);
  });
});
