import { NextRequest, NextResponse } from "next/server";

import { API_BASE_URL } from "../../../lib/api-config";
import {
  evaluateDashboardCors,
  type CorsProbe,
  type DeploymentCheckPayload,
  type HealthProbe,
} from "../../../lib/deployment-diagnostics";

export const dynamic = "force-dynamic";

const REQUEST_TIMEOUT_MS = 8_000;

async function fetchWithTimeout(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );
  try {
    return await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

async function probeHealth(path: string): Promise<HealthProbe> {
  try {
    const response = await fetchWithTimeout(path, {
      headers: { Accept: "application/json" },
    });
    const payload = await response.json().catch(() => null);
    const reportsOk =
      typeof payload === "object" &&
      payload !== null &&
      (payload as Record<string, unknown>).ok === true;
    return {
      reachable: true,
      ok: response.ok && reportsOk,
      status: response.status,
    };
  } catch {
    return { reachable: false, ok: false, status: null };
  }
}

async function probeCors(dashboardOrigin: string): Promise<CorsProbe> {
  try {
    const response = await fetchWithTimeout("/v1/personal/cycles/run", {
      method: "OPTIONS",
      headers: {
        Origin: dashboardOrigin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers":
          "authorization,content-type,idempotency-key",
      },
    });
    return {
      reachable: true,
      status: response.status,
      ...evaluateDashboardCors(
        dashboardOrigin,
        response.headers.get("access-control-allow-origin"),
        response.headers.get("access-control-allow-methods"),
        response.headers.get("access-control-allow-headers"),
      ),
    };
  } catch {
    return {
      reachable: false,
      status: null,
      ok: false,
      allowsOrigin: false,
      allowsPost: false,
      allowsAuthorization: false,
      allowsContentType: false,
      allowsIdempotencyKey: false,
    };
  }
}

export async function GET(request: NextRequest) {
  const dashboardOrigin = request.nextUrl.origin;
  const unavailable: HealthProbe = {
    reachable: false,
    ok: false,
    status: null,
  };
  const missingCors: CorsProbe = {
    reachable: false,
    status: null,
    ok: false,
    allowsOrigin: false,
    allowsPost: false,
    allowsAuthorization: false,
    allowsContentType: false,
    allowsIdempotencyKey: false,
  };

  if (!API_BASE_URL) {
    return NextResponse.json<DeploymentCheckPayload>(
      {
        configured: false,
        dashboardOrigin,
        api: unavailable,
        database: unavailable,
        worker: unavailable,
        cors: missingCors,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  }

  const [api, database, worker, cors] = await Promise.all([
    probeHealth("/livez"),
    probeHealth("/health"),
    probeHealth("/worker-health"),
    probeCors(dashboardOrigin),
  ]);

  return NextResponse.json<DeploymentCheckPayload>(
    {
      configured: true,
      apiBaseUrl: API_BASE_URL,
      dashboardOrigin,
      api,
      database,
      worker,
      cors,
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}
