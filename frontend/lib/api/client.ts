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
export type RatingCountsSummary =
  components["schemas"]["RatingCountsSummary"];
export type AspectCount = components["schemas"]["AspectCount"];
export type PlatformTagCount = components["schemas"]["PlatformTagCount"];
export type SupportedPlatformInfo =
  components["schemas"]["SupportedPlatformInfo"];
export type UnsupportedPlatformInfo =
  components["schemas"]["UnsupportedPlatformInfo"];
export type PlatformCatalogItem =
  SupportedPlatformInfo | UnsupportedPlatformInfo;
export type DataProviderProfileResponse =
  components["schemas"]["DataProviderProfileResponse"];
export type CreateDataProviderProfilePayload =
  components["schemas"]["CreateDataProviderProfileRequest"];
export type UpdateDataProviderProfilePayload =
  components["schemas"]["UpdateDataProviderProfileRequest"];
export type VerifyDataProviderResponse =
  components["schemas"]["VerifyDataProviderResponse"];
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
export type AnalyzeSourceRequestPayload =
  components["schemas"]["AnalyzeSourceRequest"];
export type AnalyzeUpToDateResponse =
  components["schemas"]["AnalyzeUpToDateResponse"];
export type AnalyzeQueuedResponse =
  components["schemas"]["AnalyzeQueuedResponse"];
export type CreateTrendSearchPayload =
  components["schemas"]["CreateTrendSearchRequest"];
export type TrendSearchQueuedResponse =
  components["schemas"]["TrendSearchQueuedResponse"];
export type TrendSearchSummary = components["schemas"]["TrendSearchSummary"];
export type TrendSearchDetail = components["schemas"]["TrendSearchDetail"];
export type TrendBrand = components["schemas"]["TrendBrand"];
export type TrendCandidate = components["schemas"]["TrendCandidate"];
export type TrendEvidenceVideo = components["schemas"]["TrendEvidenceVideo"];
export type SystemOptionsResponse =
  components["schemas"]["SystemOptionsResponse"];
export type AdminSessionResponse =
  components["schemas"]["AdminSessionResponse"];
export type AdminConfigResponse =
  components["schemas"]["AdminConfigResponse"];
export type AdminLLMConfigItem =
  components["schemas"]["AdminLLMConfigItem"];
export type AdminDataProviderConfigItem =
  components["schemas"]["AdminDataProviderConfigItem"];
export type AdminUpsertLLMPayload =
  components["schemas"]["AdminUpsertLLMRequest"];
export type AdminUpsertProviderPayload =
  components["schemas"]["AdminUpsertProviderRequest"];
export type AdminSharingUpdatePayload =
  components["schemas"]["AdminSharingUpdateRequest"];

export type IngestSubmitResponse =
  | { kind: "cached"; data: IngestCachedResponse }
  | { kind: "queued"; data: IngestQueuedResponse };

export type AnalyzeSourceResponse =
  | { kind: "up_to_date"; data: AnalyzeUpToDateResponse }
  | { kind: "queued"; data: AnalyzeQueuedResponse };

export function getApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
  return configured.replace(/\/+$/, "");
}

export function createApiClient(baseUrl = getApiBaseUrl()) {
  return createClient<paths>({ baseUrl, credentials: "include" });
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
  fresh?: boolean;
}

export async function listSources(
  params: ListSourcesParams = {}
): Promise<SourceListResponse> {
  const query: NonNullable<
    paths["/api/sources"]["get"]["parameters"]["query"]
  > = {};
  if (params.limit !== undefined) query.limit = params.limit;
  if (params.offset !== undefined) query.offset = params.offset;
  if (params.status) query.status = params.status;
  if (params.url_type) query.url_type = params.url_type;
  if (params.platform) query.platform = params.platform;
  if (params.fresh !== undefined) query.fresh = params.fresh;

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
      credentials: "include",
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

export async function analyzeSource(
  sourceId: string,
  profileId: string
): Promise<AnalyzeSourceResponse> {
  const url = `${getApiBaseUrl()}/api/sources/${encodeURIComponent(sourceId)}/analyze`;
  const payload: AnalyzeSourceRequestPayload = { llm_profile_id: profileId };
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
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
    return { kind: "up_to_date", data: body as AnalyzeUpToDateResponse };
  }
  return { kind: "queued", data: body as AnalyzeQueuedResponse };
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

export async function listPlatforms(): Promise<PlatformCatalogItem[]> {
  const { data, error, response } = await apiClient.GET("/api/platforms");
  if (error !== undefined || !data) {
    const resp = response as Response;
    throw normalizeApiError(resp.status, error, resp.statusText);
  }
  return data;
}

export async function listDataProviderProfiles(): Promise<
  DataProviderProfileResponse[]
> {
  const { data, error, response } = await apiClient.GET(
    "/api/data-providers/profiles"
  );
  if (error !== undefined || !data) {
    const resp = response as Response;
    throw normalizeApiError(resp.status, error, resp.statusText);
  }
  return data;
}

export async function createDataProviderProfile(
  payload: CreateDataProviderProfilePayload
): Promise<DataProviderProfileResponse> {
  const { data, error, response } = await apiClient.POST(
    "/api/data-providers/profiles",
    { body: payload }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function updateDataProviderProfile(
  profileId: string,
  payload: UpdateDataProviderProfilePayload
): Promise<DataProviderProfileResponse> {
  const { data, error, response } = await apiClient.PATCH(
    "/api/data-providers/profiles/{profile_id}",
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

export async function deleteDataProviderProfile(
  profileId: string
): Promise<void> {
  const { error, response } = await apiClient.DELETE(
    "/api/data-providers/profiles/{profile_id}",
    {
      params: { path: { profile_id: profileId } },
    }
  );
  if (error !== undefined || (!response.ok && response.status !== 204)) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
}

export async function verifyDataProviderProfile(
  profileId: string
): Promise<VerifyDataProviderResponse> {
  const { data, error, response } = await apiClient.POST(
    "/api/data-providers/profiles/{profile_id}/verify",
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

export async function createTrendSearch(
  payload: CreateTrendSearchPayload
): Promise<TrendSearchQueuedResponse> {
  const { data, error, response } = await apiClient.POST(
    "/api/trends/searches",
    { body: payload }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function listTrendSearches(
  limit = 20
): Promise<TrendSearchSummary[]> {
  const { data, error, response } = await apiClient.GET(
    "/api/trends/searches",
    { params: { query: { limit } } }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function getTrendSearch(
  searchId: string
): Promise<TrendSearchDetail> {
  const { data, error, response } = await apiClient.GET(
    "/api/trends/searches/{search_id}",
    { params: { path: { search_id: searchId } } }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function getSystemOptions(): Promise<SystemOptionsResponse> {
  const result = await apiClient.GET("/api/system-options");
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function getAdminSession(): Promise<AdminSessionResponse> {
  const result = await apiClient.GET("/api/admin/session");
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function adminLogin(
  password: string
): Promise<AdminSessionResponse> {
  const result = await apiClient.POST("/api/admin/login", {
    body: { password },
  });
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function adminLogout(): Promise<AdminSessionResponse> {
  const result = await apiClient.POST("/api/admin/logout");
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function getAdminConfig(): Promise<AdminConfigResponse> {
  const result = await apiClient.GET("/api/admin/config");
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function upsertAdminLlm(
  payload: AdminUpsertLLMPayload
): Promise<AdminLLMConfigItem> {
  const result = await apiClient.PUT("/api/admin/llm", {
    body: payload,
  });
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function testAdminLlm(): Promise<AdminLLMConfigItem> {
  const result = await apiClient.POST("/api/admin/llm/test");
  if (!result.response.ok || result.data === undefined) {
    throw normalizeApiError(
      result.response.status,
      result.error,
      result.response.statusText
    );
  }
  return result.data;
}

export async function deleteAdminLlm(): Promise<void> {
  const { error, response } = await apiClient.DELETE("/api/admin/llm");
  if (!response.ok) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
}

export async function updateAdminLlmSharing(
  payload: AdminSharingUpdatePayload
): Promise<AdminLLMConfigItem> {
  const { data, error, response } = await apiClient.PATCH(
    "/api/admin/llm/sharing",
    { body: payload }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function upsertAdminProvider(
  provider: "apify" | "scrapecreators",
  payload: AdminUpsertProviderPayload
): Promise<AdminDataProviderConfigItem> {
  const { data, error, response } = await apiClient.PUT(
    "/api/admin/providers/{provider}",
    {
      params: { path: { provider } },
      body: payload,
    }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function verifyAdminProvider(
  provider: "apify" | "scrapecreators"
): Promise<VerifyDataProviderResponse> {
  const { data, error, response } = await apiClient.POST(
    "/api/admin/providers/{provider}/verify",
    { params: { path: { provider } } }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}

export async function deleteAdminProvider(
  provider: "apify" | "scrapecreators"
): Promise<void> {
  const { error, response } = await apiClient.DELETE(
    "/api/admin/providers/{provider}",
    { params: { path: { provider } } }
  );
  if (!response.ok) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
}

export async function updateAdminProviderSharing(
  provider: "apify" | "scrapecreators",
  payload: AdminSharingUpdatePayload
): Promise<AdminDataProviderConfigItem> {
  const { data, error, response } = await apiClient.PATCH(
    "/api/admin/providers/{provider}/sharing",
    {
      params: { path: { provider } },
      body: payload,
    }
  );
  if (error !== undefined || !data) {
    throw normalizeApiError(response.status, error, response.statusText);
  }
  return data;
}
