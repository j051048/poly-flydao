"use client";

import { getSupabaseBrowserClient } from "./supabase/browser";

export function validatedApiBaseUrl(
  value: string | undefined,
  allowLocalHttp = process.env.NODE_ENV === "development",
): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    const loopback =
      parsed.hostname === "localhost" ||
      parsed.hostname === "127.0.0.1" ||
      parsed.hostname === "[::1]";
    if (
      (parsed.protocol !== "https:" &&
        !(allowLocalHttp && parsed.protocol === "http:" && loopback)) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      !["", "/"].includes(parsed.pathname)
    ) {
      return null;
    }
    return parsed.origin;
  } catch {
    return null;
  }
}

export const API_BASE_URL = validatedApiBaseUrl(
  process.env.NEXT_PUBLIC_API_BASE_URL,
) ?? "";

export type ApiBody = Record<string, unknown>;

export interface ApiResult<T> {
  data: T;
  status: number;
  requestId?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function buildApiHeaders(
  accessToken: string | null,
  hasBody: boolean,
  idempotencyKey?: string,
): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (hasBody) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return headers;
}

function extractMessage(status: number, payload: unknown): string {
  if (typeof payload === "object" && payload !== null) {
    const detail = (payload as Record<string, unknown>).detail;
    if (typeof detail === "string" && detail) return detail;
    const message = (payload as Record<string, unknown>).message;
    if (typeof message === "string" && message) return message;
  }
  if (status === 401) return "登录会话已失效，请重新登录。";
  if (status === 403) return "当前账户没有执行此操作的权限。";
  if (status === 404) return "后端尚未提供此功能。";
  if (status === 409) return "当前状态冲突，请刷新后重试。";
  if (status === 422) return "提交的数据未通过服务端校验。";
  if (status >= 500) return "服务暂时不可用，请检查 Zeabur 后端日志。";
  return `请求失败（HTTP ${status}）。`;
}

export async function getAccessToken(): Promise<string | null> {
  const supabase = getSupabaseBrowserClient();
  if (!supabase) return null;
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}

export async function apiRequest<T>(
  path: string,
  options: {
    method?: "GET" | "POST" | "PUT" | "DELETE";
    body?: ApiBody;
    authenticated?: boolean;
    idempotencyKey?: string;
    timeoutMs?: number;
  } = {},
): Promise<ApiResult<T>> {
  if (!API_BASE_URL) {
    throw new ApiError(
      503,
      "控制 API 地址无效；远程后端必须使用 HTTPS 且只能配置 origin。",
    );
  }
  const accessToken =
    options.authenticated === false ? null : await getAccessToken();
  if (options.authenticated !== false && !accessToken) {
    throw new ApiError(401, "请先登录后再操作。");
  }

  const controller = new AbortController();
  const timeout = globalThis.setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? 15_000,
  );

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method: options.method ?? "GET",
      headers: buildApiHeaders(
        accessToken,
        options.body !== undefined,
        options.idempotencyKey,
      ),
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") ?? "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : null;

    if (!response.ok) {
      throw new ApiError(
        response.status,
        extractMessage(response.status, payload),
        payload,
      );
    }

    return {
      data: payload as T,
      status: response.status,
      requestId: response.headers.get("x-request-id") ?? undefined,
    };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(408, "请求超时，请检查网络和后端状态。");
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

export function readableApiError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "无法连接控制 API，请检查 HTTPS、CORS 和 Zeabur 服务状态。";
}
