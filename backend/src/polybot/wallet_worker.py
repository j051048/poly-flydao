from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from decimal import Decimal
from typing import Any

from polybot.credentials import (
    EnvelopeDecryptor,
    SecretKind,
    WalletLifecycleClaim,
    WorkerCredential,
    WorkerCredentialRepository,
)

SecureClientFactory = Callable[[str], Any]


class WalletLifecycleWorker:
    """Fenced worker-side address verification and safe revocation."""

    def __init__(
        self,
        *,
        repository: WorkerCredentialRepository,
        decryptor: EnvelopeDecryptor,
        owner_id: str,
        lease_seconds: int = 60,
        poll_interval_seconds: float = 2,
        client_factory: SecureClientFactory | None = None,
        logger: logging.Logger | None = None,
    ):
        if not owner_id:
            raise ValueError("owner_id is required")
        if not 10 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 10 and 300")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.repository = repository
        self.decryptor = decryptor
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.client_factory = client_factory or _secure_client
        self.logger = logger or logging.getLogger("polybot.wallet_worker")

    async def serve(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            claim = await self.repository.claim_wallet_lifecycle(
                claimed_by=self.owner_id,
                lease_seconds=self.lease_seconds,
            )
            if claim is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_seconds)
                except TimeoutError:
                    pass
                continue
            await self.process_claim(claim)

    async def process_claim(self, claim: WalletLifecycleClaim) -> None:
        lease_valid = asyncio.Event()
        lease_valid.set()
        heartbeat = asyncio.create_task(
            self._heartbeat(claim, lease_valid),
            name=f"wallet-lifecycle-heartbeat-{claim.id}",
        )
        try:
            if claim.status == "pending_verification":
                await self._verify(claim, lease_valid)
            elif claim.status == "revocation_pending":
                await self._revoke(claim, lease_valid)
            else:
                self.logger.error("worker claimed an unsupported wallet lifecycle state")
        finally:
            lease_valid.clear()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _verify(
        self,
        claim: WalletLifecycleClaim,
        lease_valid: asyncio.Event,
    ) -> None:
        credential = await self.repository.get_pending_envelope_for_worker(
            account_id=str(claim.account_id),
            wallet_id=claim.id,
            claimed_by=self.owner_id,
            fencing_token=claim.lifecycle_fencing_token,
        )
        if credential is None:
            await self._fail_verification(
                claim,
                lease_valid,
                "verification_envelope_unavailable",
                retryable=True,
            )
            return
        secret = bytearray()
        client: Any | None = None
        try:
            secret = self._decrypt(claim, credential)
            client = await asyncio.to_thread(self.client_factory, "0x" + secret.hex())
            signer_address = str(client.signer)
            wallet_address = str(client.wallet)
            environment = client.environment
            chain_id = int(environment.chain_id)
            collateral_token = str(environment.collateral_token)
            balance = await asyncio.to_thread(
                client.get_balance_allowance,
                asset_type="COLLATERAL",
            )
            collateral_balance = Decimal(str(balance.balance)) / Decimal("1000000")
            allowances = [Decimal(str(value)) for value in balance.allowances.values()]
            allowances_ready = bool(allowances) and min(allowances) > 0
            if not lease_valid.is_set():
                return
            saved = await self.repository.complete_wallet_verification(
                account_id=str(claim.account_id),
                wallet_id=claim.id,
                claimed_by=self.owner_id,
                fencing_token=claim.lifecycle_fencing_token,
                signer_address=signer_address,
                deposit_wallet_address=wallet_address,
                chain_id=chain_id,
                collateral_token=collateral_token,
            )
            if saved is None:
                self.logger.critical(
                    "wallet verification completion was rejected by its fencing token"
                )
            else:
                await self.repository.record_wallet_readiness(
                    account_id=str(claim.account_id),
                    wallet_id=claim.id,
                    collateral_balance_pusd=collateral_balance,
                    allowances_ready=allowances_ready,
                )
        except Exception as exc:
            self.logger.warning(
                "wallet verification failed (%s)",
                type(exc).__name__,
            )
            await self._fail_verification(
                claim,
                lease_valid,
                f"verification_{type(exc).__name__.lower()}",
                retryable=not isinstance(exc, (TypeError, ValueError)),
            )
        finally:
            if client is not None:
                with suppress(Exception):
                    await asyncio.to_thread(client.close)
            _zero(secret)

    async def _revoke(
        self,
        claim: WalletLifecycleClaim,
        lease_valid: asyncio.Event,
    ) -> None:
        getter = getattr(
            self.repository,
            "get_revocation_envelope_for_worker",
            None,
        )
        if getter is None:
            self.logger.critical("revocation envelope repository method is unavailable")
            return
        credential: WorkerCredential | None = await getter(
            account_id=str(claim.account_id),
            wallet_id=claim.id,
            claimed_by=self.owner_id,
            fencing_token=claim.lifecycle_fencing_token,
        )
        if credential is None:
            self.logger.warning("wallet revocation envelope is temporarily unavailable")
            return
        secret = bytearray()
        client: Any | None = None
        try:
            secret = self._decrypt(claim, credential)
            client = await asyncio.to_thread(self.client_factory, "0x" + secret.hex())
            await asyncio.to_thread(client.cancel_all)
            open_orders = await asyncio.to_thread(
                lambda: list(client.list_open_orders().iter_items())
            )
            if open_orders or not lease_valid.is_set():
                self.logger.warning(
                    "wallet revocation remains pending until zero open orders is verified"
                )
                return
            saved = await self.repository.complete_wallet_revocation(
                account_id=str(claim.account_id),
                wallet_id=claim.id,
                claimed_by=self.owner_id,
                fencing_token=claim.lifecycle_fencing_token,
            )
            if saved is None:
                self.logger.critical(
                    "wallet revocation completion was rejected by its fencing token"
                )
        except Exception as exc:
            self.logger.warning(
                "wallet revocation retry deferred (%s)",
                type(exc).__name__,
            )
        finally:
            if client is not None:
                with suppress(Exception):
                    await asyncio.to_thread(client.close)
            _zero(secret)

    def _decrypt(
        self,
        claim: WalletLifecycleClaim,
        credential: WorkerCredential,
    ) -> bytearray:
        if (
            credential.kind is not SecretKind.EVM_SIGNER_KEY
            or credential.provider != "polymarket"
            or credential.account_id != claim.account_id
            or credential.id != claim.signer_credential_id
        ):
            raise ValueError("wallet credential identity mismatch")
        plaintext = self.decryptor.decrypt(
            credential.envelope,
            account_id=str(claim.account_id),
            kind=SecretKind.EVM_SIGNER_KEY,
            provider="polymarket",
        )
        if len(plaintext) != 32:
            raise ValueError("wallet credential length is invalid")
        return bytearray(plaintext)

    async def _fail_verification(
        self,
        claim: WalletLifecycleClaim,
        lease_valid: asyncio.Event,
        error_code: str,
        *,
        retryable: bool,
    ) -> None:
        if not lease_valid.is_set():
            return
        await self.repository.fail_wallet_verification(
            account_id=str(claim.account_id),
            wallet_id=claim.id,
            claimed_by=self.owner_id,
            fencing_token=claim.lifecycle_fencing_token,
            error_code=_error_code(error_code),
            retryable=retryable,
        )

    async def _heartbeat(
        self,
        claim: WalletLifecycleClaim,
        lease_valid: asyncio.Event,
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while lease_valid.is_set():
            await asyncio.sleep(interval)
            if not lease_valid.is_set():
                return
            try:
                valid = await self.repository.heartbeat_wallet_lifecycle(
                    account_id=str(claim.account_id),
                    wallet_id=claim.id,
                    claimed_by=self.owner_id,
                    fencing_token=claim.lifecycle_fencing_token,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                valid = False
                self.logger.exception("wallet lifecycle heartbeat failed")
            if not valid:
                lease_valid.clear()
                self.logger.critical("wallet lifecycle fencing lease was lost")
                return


def _secure_client(private_key: str) -> Any:
    from polymarket import SecureClient

    return SecureClient.create(private_key=private_key)


def _error_code(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "_.:-" else "_"
        for character in value.lower()
    )[:64]


def _zero(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)
