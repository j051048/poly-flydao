from __future__ import annotations

from dataclasses import dataclass

from polybot.config import TradingMode

LIVE_MODES: frozenset[TradingMode] = frozenset({TradingMode.CANARY, TradingMode.LIVE})

LIVE_DISABLED_NOTE = (
    "本部署未开启实盘能力（POLYBOT_PERSONAL_LIVE_ENABLED=false），"
    "desired_mode 已被降级为 paper。"
)
LIVE_WITHOUT_SIGNER_NOTE = (
    "本部署没有配置签名私钥，无法进入 canary/live，desired_mode 已被降级为 paper。"
)


@dataclass(frozen=True, slots=True)
class ModeDecision:
    """Durable request versus the mode the process is actually allowed to run."""

    desired: TradingMode
    effective: TradingMode
    note: str | None = None

    @property
    def downgraded(self) -> bool:
        return self.desired is not self.effective


def resolve_effective_mode(
    desired: TradingMode,
    *,
    live_enabled: bool,
    signer_configured: bool = True,
) -> ModeDecision:
    """Clamp a durable mode request to what this deployment may actually run.

    Only the *capability* layer stays deployment-level: whether real funds may be
    touched at all. Everything else (paper/shadow/canary/live selection) is a
    durable, hot-applied choice. A downgrade is always reported, never silent.
    """

    if desired not in LIVE_MODES:
        return ModeDecision(desired=desired, effective=desired)
    if not live_enabled:
        return ModeDecision(
            desired=desired,
            effective=TradingMode.PAPER,
            note=LIVE_DISABLED_NOTE,
        )
    if not signer_configured:
        return ModeDecision(
            desired=desired,
            effective=TradingMode.PAPER,
            note=LIVE_WITHOUT_SIGNER_NOTE,
        )
    return ModeDecision(desired=desired, effective=desired)
