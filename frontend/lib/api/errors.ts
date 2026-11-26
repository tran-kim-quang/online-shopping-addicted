export interface ApiErrorOptions {
  status: number;
  code: string;
  detail: string;
  platform?: string;
  errors?: Record<string, string>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly platform?: string;
  readonly errors?: Record<string, string>;

  constructor(options: ApiErrorOptions) {
    super(options.detail || options.code);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.detail = options.detail;
    this.platform = options.platform;
    this.errors = options.errors;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractValidationDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (!isRecord(item)) return "";
        const msg = typeof item.msg === "string" ? item.msg : "";
        const loc = Array.isArray(item.loc) ? item.loc.map(String).join(".") : "";
        return loc ? `${loc}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (parts.length > 0) {
      return parts.join("; ");
    }
  }
  return "";
}

function defaultCodeForStatus(status: number): string {
  if (status === 0) return "network_error";
  if (status === 400) return "bad_request";
  if (status === 401 || status === 403) return "unauthorized";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 422) return "validation_error";
  if (status === 429) return "rate_limit";
  if (status >= 500) return "server_error";
  return "unknown_error";
}

export function normalizeApiError(
  status: number,
  body: unknown,
  fallbackMessage = ""
): ApiError {
  if (isRecord(body)) {
    const rawCode = typeof body.error === "string" && body.error.trim() ? body.error.trim() : null;
    const rawDetail = extractValidationDetail(body.detail);
    const rawMessage = typeof body.message === "string" ? body.message : "";
    const detail = rawDetail || rawMessage || fallbackMessage || defaultCodeForStatus(status);
    const code = rawCode ?? defaultCodeForStatus(status);
    const platform =
      typeof body.platform === "string" && body.platform.trim()
        ? body.platform.trim()
        : undefined;

    let errors: Record<string, string> | undefined;
    if (isRecord(body.errors)) {
      const cleaned: Record<string, string> = {};
      for (const [key, val] of Object.entries(body.errors)) {
        if (typeof val === "string") {
          cleaned[key] = val;
        }
      }
      if (Object.keys(cleaned).length > 0) {
        errors = cleaned;
      }
    }

    return new ApiError({ status, code, detail, platform, errors });
  }

  if (typeof body === "string" && body.trim()) {
    return new ApiError({
      status,
      code: defaultCodeForStatus(status),
      detail: body.trim(),
    });
  }

  return new ApiError({
    status,
    code: defaultCodeForStatus(status),
    detail: fallbackMessage || defaultCodeForStatus(status),
  });
}
