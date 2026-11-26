import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SseFrameParser,
  parseChatSseFrame,
  streamChat,
  subscribeJobEvents,
  type ChatSseEvent,
  type JobEventHandlers,
} from "@/lib/api/sse";
import { ApiError } from "@/lib/api/errors";

const BASE = "http://api.test";

function sseResponse(chunks: string[], init: ResponseInit = {}): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
    ...init,
  });
}

function collect(): { events: ChatSseEvent[]; onEvent: (event: ChatSseEvent) => void } {
  const events: ChatSseEvent[] = [];
  return { events, onEvent: (event) => events.push(event) };
}

describe("streamChat", () => {
  it("parses every event type from a POST SSE body", async () => {
    const body = [
      'event: session\ndata: {"session_id":"s1","message_id":"m1"}\n\n',
      'event: tool_call\ndata: {"name":"query_products","args":{"limit":3}}\n\n',
      'event: tool_result\ndata: {"name":"query_products","ok":true,"count":3,"ms":12}\n\n',
      'event: discard\ndata: {}\n\n',
      'event: token\ndata: {"text":"Top 3"}\n\n',
      'event: citations\ndata: [{"ref":"P1","type":"product","product_id":"p1","title":"A","url":"u","snippet":"s"}]\n\n',
      'event: done\ndata: {"message_id":"m2","usage":null,"data_as_of":"2026-11-26T03:00:00Z","steps":2,"invalid_citations":0}\n\n',
    ];
    const { events, onEvent } = collect();

    const result = await streamChat(
      { message: "Top 3?", source_id: "src-1", llm_profile_id: null },
      {
        baseUrl: BASE,
        onEvent,
        fetchImpl: vi.fn(async () => sseResponse(body)),
      }
    );

    expect(result).toEqual({ aborted: false });
    expect(events.map((event) => event.type)).toEqual([
      "session",
      "tool_call",
      "tool_result",
      "discard",
      "token",
      "citations",
      "done",
    ]);
    const citations = events.find((event) => event.type === "citations");
    expect(citations).toMatchObject({ data: [{ ref: "P1", product_id: "p1" }] });
  });

  it("reassembles frames that are cut in the middle of a multi-byte character", async () => {
    const encoder = new TextEncoder();
    const payload = encoder.encode('event: token\ndata: {"text":"Xin chào"}\n\n');
    // Split inside the 2-byte "à" sequence to prove the decoder streams UTF-8.
    const cut = payload.indexOf(0xc3) + 1;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(payload.slice(0, cut));
        controller.enqueue(payload.slice(cut));
        controller.close();
      },
    });
    const { events, onEvent } = collect();

    await streamChat(
      { message: "hi" },
      {
        baseUrl: `${BASE}/`,
        onEvent,
        fetchImpl: vi.fn(async () => new Response(stream, { status: 200 })),
      }
    );

    expect(events).toEqual([{ type: "token", data: { text: "Xin chào" } }]);
  });

  it("sends the POST body to /api/chat with SSE accept headers", async () => {
    const fetchImpl = vi.fn(async () => sseResponse([]));

    await streamChat(
      { message: "hello", session_id: "s1", source_id: null, llm_profile_id: "prof-1" },
      { baseUrl: BASE, onEvent: () => undefined, fetchImpl }
    );

    const [url, init] = fetchImpl.mock.calls[0]! as unknown as [string, RequestInit];
    expect(url).toBe("http://api.test/api/chat");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Accept"]).toBe("text/event-stream");
    expect(JSON.parse(String(init.body))).toEqual({
      message: "hello",
      session_id: "s1",
      source_id: null,
      llm_profile_id: "prof-1",
    });
  });

  it("keeps a frame that arrives without a trailing blank line", async () => {
    const { events, onEvent } = collect();

    await streamChat(
      { message: "x" },
      {
        baseUrl: BASE,
        onEvent,
        fetchImpl: vi.fn(async () =>
          sseResponse(['event: token\ndata: {"text":"tail"}'])
        ),
      }
    );

    expect(events).toEqual([{ type: "token", data: { text: "tail" } }]);
  });

  it("skips keepalive comments and unknown or malformed frames", async () => {
    const { events, onEvent } = collect();

    await streamChat(
      { message: "x" },
      {
        baseUrl: BASE,
        onEvent,
        fetchImpl: vi.fn(async () =>
          sseResponse([
            ": keepalive\n\n",
            "event: mystery\ndata: {\"a\":1}\n\n",
            "event: token\ndata: not-json\n\n",
            'event: token\ndata: {"text":"ok"}\n\n',
          ])
        ),
      }
    );

    expect(events).toEqual([{ type: "token", data: { text: "ok" } }]);
  });

  it("normalises a non-2xx response into ApiError with the server code", async () => {
    const { onEvent } = collect();

    const promise = streamChat(
      { message: "x", source_id: "src" },
      {
        baseUrl: BASE,
        onEvent,
        fetchImpl: vi.fn(async () =>
          new Response(JSON.stringify({ error: "source_not_ready", detail: "Đang xử lý" }), {
            status: 409,
            statusText: "Conflict",
          })
        ),
      }
    );

    await expect(promise).rejects.toBeInstanceOf(ApiError);
    await expect(promise).rejects.toMatchObject({
      status: 409,
      code: "source_not_ready",
      detail: "Đang xử lý",
    });
  });

  it("treats an empty response body as a stream error", async () => {
    const { onEvent } = collect();

    const promise = streamChat(
      { message: "x" },
      {
        baseUrl: BASE,
        onEvent,
        fetchImpl: vi.fn(async () => new Response(null, { status: 200 })),
      }
    );

    await expect(promise).rejects.toMatchObject({ code: "stream_error", status: 200 });
  });

  it("maps a network failure to network_error", async () => {
    const { onEvent } = collect();

    const promise = streamChat(
      { message: "x" },
      {
        baseUrl: BASE,
        onEvent,
        fetchImpl: vi.fn(async () => {
          throw new TypeError("Failed to fetch");
        }),
      }
    );

    await expect(promise).rejects.toMatchObject({
      code: "network_error",
      status: 0,
      detail: "Failed to fetch",
    });
  });

  it("reports aborted when the caller stops mid-stream", async () => {
    const controller = new AbortController();
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(ctrl) {
        ctrl.enqueue(encoder.encode('event: token\ndata: {"text":"a"}\n\n'));
        // Never closes: the reader stays pending until the abort signal fires.
      },
    });
    const { events, onEvent } = collect();

    const promise = streamChat(
      { message: "x" },
      {
        baseUrl: BASE,
        signal: controller.signal,
        onEvent,
        fetchImpl: vi.fn(async () => new Response(stream, { status: 200 })),
      }
    );
    controller.abort();

    await expect(promise).resolves.toEqual({ aborted: true });
    expect(events.length).toBeLessThanOrEqual(1);
  });

  it("reports aborted when the fetch itself is cancelled", async () => {
    const controller = new AbortController();
    controller.abort();

    const result = await streamChat(
      { message: "x" },
      {
        baseUrl: BASE,
        signal: controller.signal,
        onEvent: () => undefined,
        fetchImpl: vi.fn(async () => {
          throw new DOMException("Aborted", "AbortError");
        }),
      }
    );

    expect(result).toEqual({ aborted: true });
  });
});

// ---------------------------------------------------------------------------
// Job SSE (EventSource)
// ---------------------------------------------------------------------------

type Listener = (event: { data?: string | null }) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  closed = false;
  private listeners = new Map<string, Listener[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data?: string): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data });
    }
  }
}

function handlers(): JobEventHandlers & {
  progress: ReturnType<typeof vi.fn>;
  done: ReturnType<typeof vi.fn>;
  error: ReturnType<typeof vi.fn>;
  timeout: ReturnType<typeof vi.fn>;
  connection: ReturnType<typeof vi.fn>;
} {
  const progress = vi.fn();
  const done = vi.fn();
  const error = vi.fn();
  const timeout = vi.fn();
  const connection = vi.fn();
  return {
    progress,
    done,
    error,
    timeout,
    connection,
    onProgress: progress,
    onDone: done,
    onError: error,
    onTimeout: timeout,
    onConnectionError: connection,
  };
}

describe("subscribeJobEvents", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const factory = (url: string) => new FakeEventSource(url) as unknown as EventSource;

  it("subscribes to the job events endpoint and forwards progress", () => {
    const spy = handlers();
    const unsubscribe = subscribeJobEvents("job-1", spy, {
      baseUrl: BASE,
      eventSourceFactory: factory,
    });

    const source = FakeEventSource.instances[0]!;
    expect(source.url).toBe("http://api.test/api/jobs/job-1/events");

    source.emit(
      "progress",
      JSON.stringify({ seq: 1, stage: "reviews", done: 5, total: 8, message: "Đang lấy", ts: "t" })
    );

    expect(spy.progress).toHaveBeenCalledWith(
      expect.objectContaining({ stage: "reviews", done: 5, total: 8 })
    );
    unsubscribe();
    expect(source.closed).toBe(true);
  });

  it("closes the stream on done and forwards the terminal payload", () => {
    const spy = handlers();
    subscribeJobEvents("job-1", spy, { baseUrl: BASE, eventSourceFactory: factory });
    const source = FakeEventSource.instances[0]!;

    source.emit(
      "done",
      JSON.stringify({ seq: 9, job_id: "job-1", status: "done", source_id: "src-9", error: null })
    );

    expect(spy.done).toHaveBeenCalledWith(expect.objectContaining({ source_id: "src-9" }));
    expect(source.closed).toBe(true);
  });

  it("distinguishes a server error event from a transport error", () => {
    const spy = handlers();
    subscribeJobEvents("job-1", spy, { baseUrl: BASE, eventSourceFactory: factory });
    const source = FakeEventSource.instances[0]!;

    source.emit("error", "");
    expect(spy.connection).toHaveBeenCalledTimes(1);
    expect(spy.error).not.toHaveBeenCalled();

    source.emit(
      "error",
      JSON.stringify({
        seq: 3,
        job_id: "job-1",
        status: "failed",
        source_id: null,
        error: "blocked: captcha",
      })
    );

    expect(spy.error).toHaveBeenCalledWith(expect.objectContaining({ error: "blocked: captcha" }));
    expect(source.closed).toBe(true);
  });

  it("ignores malformed progress payloads instead of crashing", () => {
    const spy = handlers();
    subscribeJobEvents("job-1", spy, { baseUrl: BASE, eventSourceFactory: factory });
    const source = FakeEventSource.instances[0]!;

    source.emit("progress", "{not-json");

    expect(spy.progress).not.toHaveBeenCalled();
    expect(source.closed).toBe(false);
  });

  it("reconnects with a new EventSource after the server timeout event", () => {
    const spy = handlers();
    subscribeJobEvents("job-1", spy, { baseUrl: BASE, eventSourceFactory: factory });
    const first = FakeEventSource.instances[0]!;

    first.emit(
      "timeout",
      JSON.stringify({ seq: 4, job_id: "job-1", status: "running", message: "1h" })
    );

    expect(spy.timeout).toHaveBeenCalledWith(expect.objectContaining({ status: "running" }));
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(1);

    vi.advanceTimersByTime(1000);

    expect(FakeEventSource.instances).toHaveLength(2);
    const second = FakeEventSource.instances[1]!;
    second.emit(
      "progress",
      JSON.stringify({ seq: 5, stage: "embed", done: 1, total: 1, message: "x", ts: "t" })
    );
    expect(spy.progress).toHaveBeenCalledTimes(1);
  });

  it("does not reconnect after the caller unsubscribes", () => {
    const spy = handlers();
    const unsubscribe = subscribeJobEvents("job-1", spy, {
      baseUrl: BASE,
      eventSourceFactory: factory,
    });
    const source = FakeEventSource.instances[0]!;

    source.emit("timeout", JSON.stringify({ seq: 1, job_id: "job-1", status: "running" }));
    unsubscribe();
    vi.advanceTimersByTime(5000);

    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("works without an onTimeout handler", () => {
    const spy = handlers();
    subscribeJobEvents(
      "job-1",
      { onProgress: spy.onProgress, onDone: spy.onDone, onError: spy.onError },
      { baseUrl: BASE, eventSourceFactory: factory }
    );
    const source = FakeEventSource.instances[0]!;

    expect(() =>
      source.emit("timeout", JSON.stringify({ seq: 1, job_id: "job-1", status: "running" }))
    ).not.toThrow();
    vi.advanceTimersByTime(1000);
    expect(FakeEventSource.instances).toHaveLength(2);
  });
});

describe("parseChatSseFrame", () => {
  it("returns null for unknown events and bad JSON", () => {
    expect(parseChatSseFrame({ event: "unknown", data: "{}" })).toBeNull();
    expect(parseChatSseFrame({ event: "token", data: "nope" })).toBeNull();
  });

  it("falls back to an empty citation list when the payload is not an array", () => {
    expect(parseChatSseFrame({ event: "citations", data: '{"ref":"P1"}' })).toEqual({
      type: "citations",
      data: [],
    });
  });

  it("keeps session ids and error payloads", () => {
    expect(
      parseChatSseFrame({
        event: "session",
        data: JSON.stringify({ session_id: "s", message_id: "m" }),
      })
    ).toEqual({ type: "session", data: { session_id: "s", message_id: "m" } });
    expect(
      parseChatSseFrame({
        event: "error",
        data: JSON.stringify({ code: "llm_auth", message: "sai key" }),
      })
    ).toEqual({ type: "error", data: { code: "llm_auth", message: "sai key" } });
  });
});

describe("SseFrameParser unit behaviour", () => {
  it("supports multi-line data and ids, and flushes a partial frame", () => {
    const parser = new SseFrameParser();

    expect(parser.feed("id: 7\ndata: line1\ndata: line2")).toEqual([]);
    expect(parser.flush()).toEqual([{ event: "message", data: "line1\nline2", id: "7" }]);
    expect(parser.flush()).toEqual([]);
  });
});

describe("streamChat edge cases", () => {
  it("uses the configured base url when none is passed", async () => {
    const previous = process.env.NEXT_PUBLIC_API_BASE_URL;
    process.env.NEXT_PUBLIC_API_BASE_URL = "http://env.test";
    try {
      const fetchImpl = vi.fn(async () => sseResponse([]));
      await streamChat({ message: "x" }, { onEvent: () => undefined, fetchImpl });
      expect(String((fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[0])).toBe(
        "http://env.test/api/chat"
      );
    } finally {
      process.env.NEXT_PUBLIC_API_BASE_URL = previous;
    }
  });

  it("falls back to a generic detail when a non-Error value is thrown", async () => {
    await expect(
      streamChat(
        { message: "x" },
        {
          baseUrl: BASE,
          onEvent: () => undefined,
          fetchImpl: vi.fn(async () => {
            throw "boom";
          }),
        }
      )
    ).rejects.toMatchObject({ code: "network_error", detail: "Network error" });
  });

  it("handles a non-2xx response with an empty body", async () => {
    await expect(
      streamChat(
        { message: "x" },
        {
          baseUrl: BASE,
          onEvent: () => undefined,
          fetchImpl: vi.fn(async () => new Response("", { status: 503, statusText: "Unavailable" })),
        }
      )
    ).rejects.toMatchObject({ status: 503, code: "server_error" });
  });

  it("flushes a trailing partial frame with an invalid payload without emitting", async () => {
    const { events, onEvent } = collect();

    await streamChat(
      { message: "x" },
      {
        baseUrl: BASE,
        onEvent,
        fetchImpl: vi.fn(async () => sseResponse(['event: token\ndata: {"text":'])),
      }
    );

    expect(events).toEqual([]);
  });

  it("decodes an incomplete multi-byte sequence left in the decoder buffer", async () => {
    const encoder = new TextEncoder();
    const frame = encoder.encode('event: token\ndata: {"text":"ok"}\n\n');
    const broken = new Uint8Array([0xc3]); // dangling UTF-8 lead byte
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(frame);
        controller.enqueue(broken);
        controller.close();
      },
    });
    const { events, onEvent } = collect();

    await streamChat(
      { message: "x" },
      { baseUrl: BASE, onEvent, fetchImpl: vi.fn(async () => new Response(stream, { status: 200 })) }
    );

    expect(events).toHaveLength(1);
  });

  it("maps a mid-stream reader failure to stream_error", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error(new Error("connection reset"));
      },
    });

    await expect(
      streamChat(
        { message: "x" },
        { baseUrl: BASE, onEvent: () => undefined, fetchImpl: vi.fn(async () => new Response(stream, { status: 200 })) }
      )
    ).rejects.toMatchObject({ code: "stream_error", detail: "connection reset" });
  });

  it("uses a default detail when a non-Error reader failure surfaces", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error("reset");
      },
    });

    await expect(
      streamChat(
        { message: "x" },
        { baseUrl: BASE, onEvent: () => undefined, fetchImpl: vi.fn(async () => new Response(stream, { status: 200 })) }
      )
    ).rejects.toMatchObject({ code: "stream_error", detail: "Stream interrupted" });
  });
});

describe("SseFrameParser edge cases", () => {
  it("handles a field-only line and strips exactly one leading space from values", () => {
    const parser = new SseFrameParser();

    const frames = parser.feed("event: token\ndata:  two-spaces\n\n");

    expect(frames).toEqual([{ event: "token", data: " two-spaces", id: undefined }]);
    expect(parser.feed("data\n\n")).toEqual([{ event: "message", data: "", id: undefined }]);
  });

  it("defaults the event name to message for an empty event field", () => {
    const parser = new SseFrameParser();

    expect(parser.feed("event:\ndata: {}\n\n")).toEqual([
      { event: "message", data: "{}", id: undefined },
    ]);
  });

  it("returns nothing when flushing an empty buffer", () => {
    expect(new SseFrameParser().flush()).toEqual([]);
  });
});

describe("subscribeJobEvents — defaults and closed stream", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
  });

  it("uses the env base url and the global EventSource by default", () => {
    const previousEnv = process.env.NEXT_PUBLIC_API_BASE_URL;
    process.env.NEXT_PUBLIC_API_BASE_URL = "http://env.test";
    const OriginalEventSource = globalThis.EventSource;
    const created: string[] = [];
    class GlobalEventSource extends FakeEventSource {
      constructor(url: string) {
        super(url);
        created.push(url);
      }
    }
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      GlobalEventSource as unknown as typeof EventSource;

    try {
      const unsubscribe = subscribeJobEvents("job-9", {
        onProgress: () => undefined,
        onDone: () => undefined,
        onError: () => undefined,
      });
      expect(created).toEqual(["http://env.test/api/jobs/job-9/events"]);
      unsubscribe();
    } finally {
      process.env.NEXT_PUBLIC_API_BASE_URL = previousEnv;
      (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
        OriginalEventSource;
    }
  });

  it("ignores every event after the caller unsubscribes", () => {
    const spy = handlers();
    const unsubscribe = subscribeJobEvents("job-1", spy, {
      baseUrl: BASE,
      eventSourceFactory: (url) => new FakeEventSource(url) as unknown as EventSource,
    });
    const source = FakeEventSource.instances[0]!;

    unsubscribe();
    source.emit("progress", JSON.stringify({ seq: 1, stage: "reviews", done: 1, total: 1 }));
    source.emit("done", JSON.stringify({ seq: 2, job_id: "j", status: "done" }));
    source.emit("error", JSON.stringify({ seq: 3, job_id: "j", status: "failed" }));
    source.emit("timeout", JSON.stringify({ seq: 4, job_id: "j", status: "running" }));

    expect(spy.progress).not.toHaveBeenCalled();
    expect(spy.done).not.toHaveBeenCalled();
    expect(spy.error).not.toHaveBeenCalled();
    expect(spy.timeout).not.toHaveBeenCalled();
  });

  it("does not fire terminal handlers for malformed payloads", () => {
    const spy = handlers();
    subscribeJobEvents("job-1", spy, {
      baseUrl: BASE,
      eventSourceFactory: (url) => new FakeEventSource(url) as unknown as EventSource,
    });
    const source = FakeEventSource.instances[0]!;

    source.emit("done", "{bad");
    source.emit("error", "{bad");
    source.emit("timeout", "{bad");

    expect(spy.done).not.toHaveBeenCalled();
    expect(spy.error).not.toHaveBeenCalled();
    expect(spy.timeout).not.toHaveBeenCalled();
  });
});
