"""Control-plane and worker job contracts, protocols, and repositories.

The public import path is ``polybot.jobs``; the implementation lives in
``schemas`` (contracts), ``protocols`` (repository contracts), ``supabase``
(the service-role repository), and ``memory`` (the in-process one).
"""

from __future__ import annotations

from polybot.jobs.memory import InMemoryJobRepository
from polybot.jobs.schemas import (
    AccountNotReadyError,
    AIBudgetRequest,
    AIDiagnosticJob,
    CycleJob,
    CycleJobRequest,
    CycleJobStatus,
    JobConflictError,
    JobNotFoundError,
    JobRepository,
    PerformanceSnapshot,
    PortfolioSnapshot,
    RiskPolicySnapshot,
    RiskPresetRequest,
    RuntimeProfile,
    RuntimeProfilePatch,
    WorkerJobRepository,
    WorkerStatusSnapshot,
    WorkerTradingWallet,
    _decimal_or_none,
    _decimal_or_zero,
    _decimal_text,
    _inside_window,
    _job_from_row,
    _parse_datetime,
    _performance_snapshot,
    _profile_from_row,
    _ratio_text,
    arm_expiry,
)
from polybot.jobs.supabase import SupabaseJobRepository

__all__ = [
    "AIBudgetRequest",
    "AIDiagnosticJob",
    "AccountNotReadyError",
    "CycleJob",
    "CycleJobRequest",
    "CycleJobStatus",
    "InMemoryJobRepository",
    "JobConflictError",
    "JobNotFoundError",
    "JobRepository",
    "PerformanceSnapshot",
    "PortfolioSnapshot",
    "RiskPolicySnapshot",
    "RiskPresetRequest",
    "RuntimeProfile",
    "RuntimeProfilePatch",
    "SupabaseJobRepository",
    "WorkerJobRepository",
    "WorkerStatusSnapshot",
    "WorkerTradingWallet",
    "_decimal_or_none",
    "_decimal_or_zero",
    "_decimal_text",
    "_inside_window",
    "_job_from_row",
    "_parse_datetime",
    "_performance_snapshot",
    "_profile_from_row",
    "_ratio_text",
    "arm_expiry",
]
