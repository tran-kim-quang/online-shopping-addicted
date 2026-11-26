import type { CitationItem } from "../citations";
import { getApiBaseUrl } from "./client";
import { ApiError, normalizeApiError } from "./errors";

export interface RawSseFrame {
  event: string;
  data: string;
  id?: string;
}

/**
 * Incremental SSE frame parser handling chunk boundaries, CRLF/LF normalization,
 * multi-line data fields, and comment lines (e.g. `: keepalive`).
 */
export class SseFrameParser {
  private buffer = "";

  feed(chunk: string): RawSseFrame[] {
    this.buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const frames: RawSseFrame[] = [];

    let delimiterIdx = this.buffer.indexOf("\n\n");
    while (delimiterIdx !== -1) {
      const rawBlock = this.buffer.slice(0, delimiterIdx);
      this.buffer = this.buffer.slice(delimiterIdx + 2);
      const parsed = this.parseBlock(rawBlock);
      if (parsed !== null) {
        frames.push(parsed);
      }
      delimiterIdx = this.buffer.indexOf("\n\n");
    }

    return frames;
  }

  flush(): RawSseFrame[] {
    const remaining = this.buffer.trim();
    this.buffer = "";
    if (!remaining) return [];
    const parsed = this.parseBlock(remaining);
    return parsed ? [parsed] : [];
  }

  private parseBlock(block: string): RawSseFrame | null {
    const lines = block.split("\n");
    let eventName = "message";
    let id: string | undefined;
    const dataLines: string[] = [];

    for (const line of lines) {
      if (!line || line.startsWith(":")) {
        continue;
      }
      const colonIdx = line.indexOf(":");
      const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
      let value = colonIdx === -1 ? "" : line.slice(colonIdx + 1);
      if (value.startsWith(" ")) {
        value = value.slice(1);
      }

      if (field === "event") {
        eventName = value || "message";
      } else if (field === "data") {
        dataLines.push(value);
      } else if (field === "id") {
        id = value;
      }
    }

    if (dataLines.length === 0) {
      return null;
    }
    return { event: eventName, data: dataLines.join("\n"), id };
  }
}

// ---------------------------------------------------------------------------
// Chat SSE (POST /api/chat via fetch + ReadableStream)
// ---------------------------------------------------------------------------

export interface ChatRequestPayload {
  session_id?: string | null;
  source_id?: string | null;
  llm_profile_id?: string | null;
  message: string;
}

export interface ChatSessionEventData {
  session_id: string;
  message_id: string;
}

export interface ChatToolCallEventData {
  name: string;
  args: Record<string, unknown>;
}

export interface ChatToolResultEventData {
  name: string;
  ok: boolean;
  count: number;
  ms: number;
  error?: string;
}

export interface ChatTokenEventData {
  text: string;
}

export interface ChatDoneEventData {
  message_id: string;
  usage: Record<string, unknown> | null;
  data_as_of: string | null;
  steps: number;
  invalid_citations: number;
}

export interface ChatErrorEventData {
  code: string;
  message: string;
}

export type ChatSseEvent =
  | { type: "session"; data: ChatSessionEventData }
  | { type: "tool_call"; data: ChatToolCallEventData }
  | { type: "tool_result"; data: ChatToolResultEventData }
  | { type: "token"; data: ChatTokenEventData }
  | { type: "discard"; data: Record<string, never> }
  | { type: "citations"; data: CitationItem[] }
  | { type: "done"; data: ChatDoneEventData }
  | { type: "error"; data: ChatErrorEventData };

export function parseChatSseFrame(frame: RawSseFrame): ChatSseEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data) as unknown;
  } catch {
    return null;
  }

  switch (frame.event) {
    case "session":
      return { type: "session", data: parsed as ChatSessionEventData };
    case "tool_call":
      return { type: "tool_call", data: parsed as ChatToolCallEventData };
    case "tool_result":
      return { type: "tool_result", data: parsed as ChatToolResultEventData };
    case "token":
      return { type: "token", data: parsed as ChatTokenEventData };
    case "discard":
      return { type: "discard", data: {} };
    case "citations":
      return {
        type: "citations",
        data: Array.isArray(parsed) ? (parsed as CitationItem[]) : [],
      };
    case "done":
      return { type: "done", data: parsed as ChatDoneEventData };
    case "error":
      return { type: "error", data: parsed as ChatErrorEventData };
    default:
      return null;
  }
}

export interface StreamChatOptions {
  signal?: AbortSignal;
  onEvent: (event: ChatSseEvent) => void;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

export async function streamChat(
  payload: ChatRequestPayload,
  options: StreamChatOptions
): Promise<{ aborted: boolean }> {
  const fetchFn = options.fetchImpl ?? fetch;
  const baseUrl = (options.baseUrl ?? getApiBaseUrl()).replace(/\/+$/, "");

  let response: Response;
  try {
    response = await fetchFn(`${baseUrl}/api/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(payload),
      signal: options.signal,
    });
  } catch (err) {
    if (options.signal?.aborted) {
      return { aborted: true };
    }
    throw new ApiError({
      status: 0,
      code: "network_error",
      detail: err instanceof Error ? err.message : "Network error",
    });
  }

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    let body: unknown = text;
    try {
      body = text ? (JSON.parse(text) as unknown) : null;
    } catch {
      body = text;
    }
    throw normalizeApiError(response.status, body, response.statusText);
  }

  if (!response.body) {
    throw new ApiError({
      status: response.status,
      code: "stream_error",
      detail: "Empty SSE response body.",
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = new SseFrameParser();

  try {
    while (true) {
      if (options.signal?.aborted) {
        await reader.cancel().catch(() => undefined);
        return { aborted: true };
      }

      const { done, value } = await reader.read();
      if (done) {
        break;
      }

      const text = decoder.decode(value, { stream: true });
      const frames = parser.feed(text);
      for (const frame of frames) {
        const event = parseChatSseFrame(frame);
        if (event) {
          options.onEvent(event);
        }
      }
    }

    const tail = decoder.decode();
    if (tail) {
      for (const frame of parser.feed(tail)) {
        const event = parseChatSseFrame(frame);
        if (event) {
          options.onEvent(event);
        }
      }
    }

    for (const frame of parser.flush()) {
      const event = parseChatSseFrame(frame);
      if (event) {
        options.onEvent(event);
      }
    }

    return { aborted: false };
  } catch (err) {
    if (options.signal?.aborted) {
      return { aborted: true };
    }
    throw new ApiError({
      status: 0,
      code: "stream_error",
      detail: err instanceof Error ? err.message : "Stream interrupted",
    });
  } finally {
    reader.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// Job SSE (GET /api/jobs/{id}/events via EventSource)
// ---------------------------------------------------------------------------

export interface JobProgressEventData {
  seq: number;
  stage: string;
  done: number;
  total: number | null;
  message: string;
  ts: string;
}

export interface JobTerminalEventData {
  seq: number;
  job_id: string;
  status: string;
  source_id: string | null;
  error: string | null;
}

export interface JobTimeoutEventData {
  seq: number;
  job_id: string;
  status: string;
  message: string;
}

export interface JobEventHandlers {
  onProgress: (data: JobProgressEventData) => void;
  onDone: (data: JobTerminalEventData) => void;
  onError: (data: JobTerminalEventData) => void;
  onTimeout?: (data: JobTimeoutEventData) => void;
  onConnectionError?: () => void;
}

export interface SubscribeJobOptions {
  baseUrl?: string;
  eventSourceFactory?: (url: string) => EventSource;
}

function safeParseJson<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function subscribeJobEvents(
  jobId: string,
  handlers: JobEventHandlers,
  options: SubscribeJobOptions = {}
): () => void {
  const baseUrl = (options.baseUrl ?? getApiBaseUrl()).replace(/\/+$/, "");
  const streamUrl = `${baseUrl}/api/jobs/${encodeURIComponent(jobId)}/events`;
  const makeEventSource =
    options.eventSourceFactory ?? ((url: string) => new EventSource(url));

  let closed = false;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  const cleanup = () => {
    closed = true;
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (source) {
      source.close();
      source = null;
    }
  };

  const connect = () => {
    if (closed) return;
    const es = makeEventSource(streamUrl);
    source = es;

    es.addEventListener("progress", (evt) => {
      if (closed) return;
      const msg = evt as MessageEvent<string>;
      const data = safeParseJson<JobProgressEventData>(msg.data);
      if (data) {
        handlers.onProgress(data);
      }
    });

    es.addEventListener("done", (evt) => {
      if (closed) return;
      const msg = evt as MessageEvent<string>;
      const data = safeParseJson<JobTerminalEventData>(msg.data);
      cleanup();
      if (data) {
        handlers.onDone(data);
      }
    });

    es.addEventListener("error", (evt) => {
      if (closed) return;
      // Distinguish server-sent `event: error` (MessageEvent with JSON data)
      // from browser network reconnect errors (plain Event with no data).
      const maybeMsg = evt as MessageEvent<string | undefined>;
      if (typeof maybeMsg.data === "string" && maybeMsg.data.length > 0) {
        const data = safeParseJson<JobTerminalEventData>(maybeMsg.data);
        cleanup();
        if (data) {
          handlers.onError(data);
        }
        return;
      }
      handlers.onConnectionError?.();
    });

    es.addEventListener("timeout", (evt) => {
      if (closed) return;
      const msg = evt as MessageEvent<string>;
      const data = safeParseJson<JobTimeoutEventData>(msg.data);
      if (data) {
        handlers.onTimeout?.(data);
      }
      es.close();
      source = null;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, 1000);
    });
  };

  connect();
  return cleanup;
}
