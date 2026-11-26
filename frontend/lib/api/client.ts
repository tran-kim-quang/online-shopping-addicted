import createClient from "openapi-fetch";
import type { components, paths } from "./schema";
import { ApiError, normalizeApiError } from "./errors";

export type SourceListItem = components["schemas"]["SourceListItem"];
export type SourceListResponse = components["schemas"]["SourceListResponse"];
export type SourceSummary = components["schemas"]["SourceSummaryResponse"];
export type SourceReport = components["schemas"]["SourceReport"];
export type ProductReportRow = components["schemas"]["ProductReportRow"];
export type RankingItem = components["schemas"]["RankingItem"];
export type ReportWarning = components["schemas"]["ReportWarning"];
export type RatingBucket = components["schemas"]["RatingBucket"];
export type AspectCount = components["schemas"]["AspectCount"];
export type ProfileResponse = components["schemas"]["ProfileResponse"];
export type CreateProfilePayload = components["schemas"]["CreateProfileRequest"];
export type UpdateProfilePayload = components["schemas"]["UpdateProfileRequest"];
export type ChatSessionSummary = components["schemas"]["ChatSessionSummary"];
export type ChatSessionDetail = components["schemas"]["ChatSessionDetail"];
export type ChatMessageOut = components["schemas"]["ChatMessageOut"];
export type JobStatusResponse = components["schemas"]["JobStatusResponse"];
export type IngestRequestPayload = components["schemas"]["IngestRequest"];
export type IngestCachedResponse = components["schemas"]["IngestCachedResponse"];
export type IngestQueuedResponse = components["schemas"]["IngestQueuedResponse"];

export type IngestSubmitResponse =
  | { kind: "cached"; data: IngestCachedResponse }
  | { kind: "queued"; data: IngestQueuedResponse };

export function getApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
  return configured.replace(/\/+$/, "");
}

export function createApiClient(baseUrl = getApiBaseUrl()) {
  return createClient<paths>({ baseUrl });
}

export const apiClient = createApiClient();

async function parseJsonSafe(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

export interface ListSourcesParams {
  limit?: number;
  offset?: number;
  status?: string;
  url_type?: string;
  platform?: string;
}

export async function listSources(
  params: ListSourcesParams = {}
): Promise<SourceListResponse> {
  const query: Record<string, string | number> = {};
  if (params.limit !== undefined) query.limit = params.limit;
  if (params.offset !== undefined) query.offset = params.offset;
  if (params.status) query.status = params.status;
  if (params.url_type) query.url_type = params.url_type;
  if (params.platform) query.platform = params.platform;

  const { data, error, response } = await apiClient.GET("/api/sources", {
    params: { query },
  });
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function getSource(sourceId: string): Promise<SourceSummary> {
  const { data, error, response } = await apiClient.GET(
    "/api/sources/{source_id}",
    {
      params: { path: { source_id: sourceId } },
    }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function getSourceReport(
  sourceId: string,
  topN = 10
): Promise<SourceReport> {
  const { data, error, response } = await apiClient.GET(
    "/api/sources/{source_id}/report",
    {
      params: { path: { source_id: sourceId }, query: { top_n: topN } },
    }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function submitIngest(
  payload: IngestRequestPayload
): Promise<IngestSubmitResponse> {
  const url = `${getApiBaseUrl()}/api/ingest`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    throw new ApiError({
      status: 0,
      code: "network_error",
      detail: err instanceof Error ? err.message : "Network error",
    });
  }

  const body = await parseJsonSafe(response);
  if (!response.ok) {
    throw normalizeApiError(response.status, body, response.statusText);
  }

  if (response.status === 200) {
    return { kind: "cached", data: body as IngestCachedResponse };
  }
  return { kind: "queued", data: body as IngestQueuedResponse };
}

export async function getJobStatus(jobId: string): Promise<JobStatusResponse> {
  const { data, error, response } = await apiClient.GET("/api/jobs/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function listLlmProfiles(): Promise<ProfileResponse[]> {
  const result = await apiClient.GET("/api/llm/profiles");
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function listProviderModels(
  baseUrl: string,
  apiKey?: string | null
): Promise<string[]> {
  const { data, error, response } = await apiClient.POST("/api/llm/models", {
    body: {
      base_url: baseUrl,
      api_key: apiKey && apiKey.trim().length > 0 ? apiKey.trim() : null,
    },
  });
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data.models;
}

export async function createLlmProfile(
  payload: CreateProfilePayload
): Promise<ProfileResponse> {
  const { data, error, response } = await apiClient.POST("/api/llm/profiles", {
    body: payload,
  });
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function updateLlmProfile(
  profileId: string,
  payload: UpdateProfilePayload
): Promise<ProfileResponse> {
  const { data, error, response } = await apiClient.PATCH(
    "/api/llm/profiles/{profile_id}",
    {
      params: { path: { profile_id: profileId } },
      body: payload,
    }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function deleteLlmProfile(profileId: string): Promise<void> {
  const { error, response } = await apiClient.DELETE(
    "/api/llm/profiles/{profile_id}",
    {
      params: { path: { profile_id: profileId } },
    }
  );
  if (error !== undefined || (!response.ok && response.status !== 204)) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
}

export async function testLlmProfile(
  profileId: string
): Promise<ProfileResponse> {
  const { data, error, response } = await apiClient.POST(
    "/api/llm/profiles/{profile_id}/test",
    {
      params: { path: { profile_id: profileId } },
    }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function listChatSessions(
  sourceId: string
): Promise<ChatSessionSummary[]> {
  const { data, error, response } = await apiClient.GET("/api/chat/sessions", {
    params: { query: { source_id: sourceId } },
  });
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function getChatSession(
  sessionId: string
): Promise<ChatSessionDetail> {
  const { data, error, response } = await apiClient.GET(
    "/api/chat/sessions/{session_id}",
    {
      params: { path: { session_id: sessionId } },
    }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function deleteChatSession(sessionId: string): Promise<void> {
  const { error, response } = await apiClient.DELETE(
    "/api/chat/sessions/{session_id}",
    {
      params: { path: { session_id: sessionId } },
    }
  );
  if (error !== undefined || (!response.ok && response.status !== 204)) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
}

export function getProductsCsvUrl(sourceId: string): string {
  return `${getApiBaseUrl()}/api/sources/${encodeURIComponent(sourceId)}/export/products.csv`;
}

export interface ReviewsCsvParams {
  include_author?: boolean;
  sentiment?: "positive" | "neutral" | "negative";
  min_rating?: number;
  max_rating?: number;
}

export function getReviewsCsvUrl(
  sourceId: string,
  params: ReviewsCsvParams = {}
): string {
  const search = new URLSearchParams();
  if (params.include_author) search.set("include_author", "true");
  if (params.sentiment) search.set("sentiment", params.sentiment);
  if (params.min_rating !== undefined) {
    search.set("min_rating", String(params.min_rating));
  }
  if (params.max_rating !== undefined) {
    search.set("max_rating", String(params.max_rating));
  }
  const qs = search.toString();
  const base = `${getApiBaseUrl()}/api/sources/${encodeURIComponent(sourceId)}/export/reviews.csv`;
  return qs ? `${base}?${qs}` : base;
}
