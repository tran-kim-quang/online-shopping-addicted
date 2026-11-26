import { describe, expect, it } from "vitest";
import { ApiError, isApiError, normalizeApiError } from "@/lib/api/errors";

describe("ApiError", () => {
  it("keeps the HTTP status, code and detail", () => {
    const error = new ApiError({
      status: 409,
      code: "source_not_ready",
      detail: "Dữ liệu đang xử lý",
      platform: "tiki",
      errors: { chat: "boom" },
    });

    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ApiError");
    expect(error.message).toBe("Dữ liệu đang xử lý");
    expect(error.status).toBe(409);
    expect(error.code).toBe("source_not_ready");
    expect(error.platform).toBe("tiki");
    expect(error.errors).toEqual({ chat: "boom" });
    expect(isApiError(error)).toBe(true);
    expect(isApiError(new Error("plain"))).toBe(false);
    expect(isApiError({ status: 500 })).toBe(false);
  });

  it("uses the code as message when no detail is provided", () => {
    expect(new ApiError({ status: 500, code: "server_error", detail: "" }).message).toBe(
      "server_error"
    );
  });
});

describe("normalizeApiError", () => {
  it("reads the {error, detail} envelope used by the backend", () => {
    const error = normalizeApiError(409, {
      error: "api_key_required_on_host_change",
      detail: "Base URL changed",
      platform: "openrouter",
    });

    expect(error).toMatchObject({
      status: 409,
      code: "api_key_required_on_host_change",
      detail: "Base URL changed",
      platform: "openrouter",
    });
  });

  it("falls back to a status-derived code when the body has no error field", () => {
    expect(normalizeApiError(404, { detail: "Source not found." }).code).toBe("not_found");
    expect(normalizeApiError(409, { detail: "conflict" }).code).toBe("conflict");
    expect(normalizeApiError(422, { detail: "bad" }).code).toBe("validation_error");
    expect(normalizeApiError(429, {}).code).toBe("rate_limit");
    expect(normalizeApiError(500, {}).code).toBe("server_error");
    expect(normalizeApiError(418, {}).code).toBe("unknown_error");
    expect(normalizeApiError(0, {}).code).toBe("network_error");
  });

  it("flattens FastAPI 422 validation details into readable text", () => {
    const error = normalizeApiError(422, {
      detail: [
        { loc: ["body", "url"], msg: "field required" },
        { loc: ["body", "limit"], msg: "must be <= 100" },
        { msg: "no location" },
        "not-an-object",
      ],
    });

    expect(error.detail).toBe("body.url: field required; body.limit: must be <= 100; no location");
    expect(error.code).toBe("validation_error");
  });

  it("keeps a plain string detail untouched", () => {
    expect(normalizeApiError(400, { detail: "plain reason" }).detail).toBe("plain reason");
  });

  it("prefers detail over a message field", () => {
    expect(normalizeApiError(400, { message: "message", detail: "detail" }).detail).toBe("detail");
    expect(normalizeApiError(400, { message: "message" }).detail).toBe("message");
  });

  it("keeps only string entries of an errors map", () => {
    const error = normalizeApiError(400, {
      detail: "x",
      errors: { chat: "chat failed", tools: 3, json: null },
    });

    expect(error.errors).toEqual({ chat: "chat failed" });
  });

  it("drops an empty errors map", () => {
    expect(normalizeApiError(400, { detail: "x", errors: {} }).errors).toBeUndefined();
    expect(normalizeApiError(400, { detail: "x", errors: "nope" }).errors).toBeUndefined();
  });

  it("ignores a blank platform", () => {
    expect(normalizeApiError(400, { detail: "x", platform: "   " }).platform).toBeUndefined();
  });

  it("uses a string body as detail", () => {
    const error = normalizeApiError(500, "  gateway exploded  ", "fallback");

    expect(error.detail).toBe("gateway exploded");
    expect(error.code).toBe("server_error");
  });

  it("uses the fallback message or the status code for empty bodies", () => {
    expect(normalizeApiError(500, null, "Internal Server Error").detail).toBe(
      "Internal Server Error"
    );
    expect(normalizeApiError(400, null).detail).toBe("bad_request");
    expect(normalizeApiError(400, "").detail).toBe("bad_request");
    expect(normalizeApiError(400, [], "fallback").detail).toBe("fallback");
  });

  it("ignores an array body", () => {
    expect(normalizeApiError(422, [{ loc: ["body"] }]).detail).toBe("validation_error");
  });
});
