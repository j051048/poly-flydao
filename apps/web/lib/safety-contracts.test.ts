import { describe, expect, it } from "vitest";
import { asNumber, parseStatus, tradingGateState } from "./dashboard";
import { parsePerformance, validationLabel } from "./performance";
import { parseEquityHistory } from "./equity";

describe("truthful trading state", () => {
  it("does not turn missing, failed, stale or expired authority into a safe state", () => {
    const now = Date.now();
    const status = parseStatus({ control: {
      armed: true, kill_switch: false, accept_new_intents: true, mode: "live",
      armed_until: new Date(now + 10_000).toISOString(),
    } });
    expect(tradingGateState(null, now)).toBe("unknown");
    expect(tradingGateState(parseStatus({}), now)).toBe("unknown");
    expect(tradingGateState(status, now)).toBe("armed");
    expect(tradingGateState(status, now, true)).toBe("stale");
    expect(tradingGateState(status, now + 11_000)).toBe("unknown");
    expect(tradingGateState(status, now + 31_000)).toBe("stale");
    expect(tradingGateState(parseStatus({ control: {
      armed: false, kill_switch: true, accept_new_intents: false,
    } }), now)).toBe("locked");
  });
});

describe("serialized financial values", () => {
  it("accepts Decimal strings and keeps absent or malformed values unknown", () => {
    const parsed = parsePerformance({ sample_size: 2, resolved_markets: 1,
      brier_score: "0.25", log_loss: "0.6931", research_only: true,
      calibration: [{ lower: "0.4", upper: "0.6", samples: 2, mean_forecast: "0.5", observed_frequency: "1" }],
    });
    expect(parsed.brier_score?.toFixed(4)).toBe("0.2500");
    expect(parsed.log_loss?.toFixed(4)).toBe("0.6931");
    expect(parsed.calibration[0].mean_forecast).toBe(0.5);
    for (const value of [null, undefined, "", " ", false, [], "NaN", "Infinity"]) expect(asNumber(value)).toBeUndefined();
    expect(parsePerformance({ brier_score: "bad", log_loss: "Infinity" }).log_loss).toBeUndefined();
    expect(validationLabel(null)).toBe("未获取");
    expect(validationLabel(parsePerformance({ research_only: false }))).toBe("待验证");
  });
});

describe("equity ledger separation", () => {
  it("cannot draw paper capital into a real-money series", () => {
    const parsed = parseEquityHistory({ scope: "real", items: [
      { source: "paper_cycle", equity_usd: "1000", recorded_at: "2026-09-28T01:00:00Z" },
      { source: "canary_cycle", equity_usd: "20", recorded_at: "2026-09-28T02:00:00Z" },
      { source: "live_cycle", equity_usd: "22", recorded_at: "2026-09-28T03:00:00Z" },
      { source: "unknown", equity_usd: "9000", recorded_at: "2026-09-28T04:00:00Z" },
    ] }, "real");
    expect(parsed.points.map((point) => point.equityUsd)).toEqual([20, 22]);
    expect(() => parseEquityHistory({ scope: "paper", items: [] }, "real")).toThrow("口径未确认");
    expect(() => parseEquityHistory({ items: [] })).toThrow("口径未确认");
  });
});
