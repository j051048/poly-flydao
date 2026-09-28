import { asBoolean, asNumber, asRecord, asString } from "./dashboard";

export interface CalibrationBin {
  lower: number;
  upper: number;
  samples: number;
  mean_forecast?: number;
  observed_frequency?: number;
}

export interface PerformanceSnapshot {
  sample_size: number;
  resolved_markets: number;
  brier_score?: number;
  log_loss?: number;
  calibration: CalibrationBin[];
  ai_usage_used: number;
  ai_usage_limit: number;
  strategy_validation?: string;
  research_only?: boolean;
  warning?: string;
}

const probability = (value: unknown) => {
  const parsed = asNumber(value);
  return parsed !== undefined && parsed >= 0 && parsed <= 1 ? parsed : undefined;
};
const count = (value: unknown) => {
  const parsed = asNumber(value);
  return parsed !== undefined && Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
};

/** FastAPI serializes Decimal as strings; validate at the network boundary. */
export function parsePerformance(payload: unknown): PerformanceSnapshot {
  const row = asRecord(payload);
  if (!Object.keys(row).length) throw new Error("绩效响应为空，请重新读取。");
  const loss = asNumber(row.log_loss);
  return {
    sample_size: count(row.sample_size),
    resolved_markets: count(row.resolved_markets),
    brier_score: probability(row.brier_score),
    log_loss: loss !== undefined && loss >= 0 ? loss : undefined,
    calibration: (Array.isArray(row.calibration) ? row.calibration : []).flatMap((raw) => {
      const bin = asRecord(raw);
      const lower = probability(bin.lower);
      const upper = probability(bin.upper);
      return lower === undefined || upper === undefined || lower >= upper ? [] : [{
        lower, upper, samples: count(bin.samples),
        mean_forecast: probability(bin.mean_forecast),
        observed_frequency: probability(bin.observed_frequency),
      }];
    }),
    ai_usage_used: count(row.ai_usage_used),
    ai_usage_limit: count(row.ai_usage_limit),
    strategy_validation: asString(row.strategy_validation),
    research_only: asBoolean(row.research_only),
    warning: asString(row.warning),
  };
}

export function validationLabel(snapshot: PerformanceSnapshot | null): string {
  if (!snapshot) return "未获取";
  if (snapshot.research_only === true) return "RESEARCH ONLY";
  return snapshot.strategy_validation === "validated" && snapshot.research_only === false
    ? "VALIDATED" : "待验证";
}
