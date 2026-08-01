export interface HealthProbe {
  reachable: boolean;
  ok: boolean;
  status: number | null;
}

export interface CorsEvaluation {
  ok: boolean;
  allowsOrigin: boolean;
  allowsPut: boolean;
  allowsAuthorization: boolean;
  allowsContentType: boolean;
  allowsIdempotencyKey: boolean;
}

export interface CorsProbe extends CorsEvaluation {
  reachable: boolean;
  status: number | null;
}

export interface DeploymentCheckPayload {
  configured: boolean;
  apiBaseUrl?: string;
  dashboardOrigin: string;
  api: HealthProbe;
  database: HealthProbe;
  worker: HealthProbe;
  cors: CorsProbe;
}

function csvTokens(value: string | null): Set<string> {
  return new Set(
    (value ?? "")
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean),
  );
}

export function evaluateDashboardCors(
  dashboardOrigin: string,
  allowOrigin: string | null,
  allowMethods: string | null,
  allowHeaders: string | null,
): CorsEvaluation {
  const normalizedOrigin = allowOrigin?.trim().toLowerCase() ?? "";
  const methods = csvTokens(allowMethods);
  const headers = csvTokens(allowHeaders);
  const allowsOrigin =
    normalizedOrigin === "*" ||
    normalizedOrigin === dashboardOrigin.trim().toLowerCase();
  const allowsPut = methods.has("put");
  const allowsAuthorization = headers.has("authorization") || headers.has("*");
  const allowsContentType = headers.has("content-type") || headers.has("*");
  const allowsIdempotencyKey =
    headers.has("idempotency-key") || headers.has("*");

  return {
    ok:
      allowsOrigin &&
      allowsPut &&
      allowsAuthorization &&
      allowsContentType &&
      allowsIdempotencyKey,
    allowsOrigin,
    allowsPut,
    allowsAuthorization,
    allowsContentType,
    allowsIdempotencyKey,
  };
}
