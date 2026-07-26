## Summary

<!-- What changed, why it changed, and the user-visible impact. -->

## Risk level

- [ ] Low: docs, tests, or isolated tooling
- [ ] Medium: runtime behavior without order-signing impact
- [ ] High: execution, signer, reconciliation, ledger, or risk controls

## Change type

- [ ] Backend
- [ ] Web console
- [ ] Database migration
- [ ] Deployment or dependency
- [ ] Documentation

## Validation

- [ ] Python lint and tests pass
- [ ] Python production dependency audit passes
- [ ] Web production build and dependency audit pass
- [ ] Zeabur container builds
- [ ] Relevant paper/shadow/canary evidence is attached

## Trading safety

- [ ] No secret, private key, service-role key, or bearer token is committed
- [ ] Safe defaults remain `paper` + `mock`
- [ ] AI output cannot bypass deterministic sizing, price, exposure, arm, or kill controls
- [ ] Signer isolation, idempotency, lease/fencing, reconciliation, and ledger invariants remain intact

## Deployment and rollback

<!-- Environment changes, migrations, rollout order, monitoring, and rollback procedure. -->
