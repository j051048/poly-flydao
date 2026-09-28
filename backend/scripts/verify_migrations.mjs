// Offline PostgreSQL migration verification. No environment variables, secrets,
// connections, or application database are read. Install the test runtime into
// an ignored temporary directory first:
// npm install --prefix .audit-migrations --ignore-scripts @electric-sql/pglite@0.5.8
// node backend/scripts/verify_migrations.mjs .audit-migrations
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const runtimeDirectory = resolve(process.argv[2] ?? ".audit-migrations");
const require = createRequire(resolve(runtimeDirectory, "package.json"));
const { PGlite } = require("@electric-sql/pglite");
const { pgcrypto } = require("@electric-sql/pglite/contrib/pgcrypto");
const migrationDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "../supabase/migrations");
const db = new PGlite({ extensions: { pgcrypto } });
const account = "00000000-0000-0000-0000-000000000001";
const outsider = "00000000-0000-0000-0000-000000000002";
const market = "00000000-0000-0000-0000-000000000003";
const report = { runtime: "@electric-sql/pglite@0.5.8", migrations: [], checks: [] };

async function expectFailure(sql, code, label) {
  let failed = false;
  try {
    await db.exec(sql);
  } catch (error) {
    assert.equal(error.code, code, `${label}: unexpected SQL error ${error.message}`);
    failed = true;
  }
  assert.ok(failed, `${label}: statement unexpectedly succeeded`);
  report.checks.push(label);
}

try {
  report.postgres = (await db.query("select version() as version")).rows[0].version;
  // This is the minimal Supabase substrate, not a replacement implementation
  // of auth. RLS uses the same request.jwt.claim.sub setting as production.
  await db.exec(`
    create schema extensions;
    create schema auth;
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin bypassrls;
    create table auth.users (id uuid primary key);
    create function auth.uid() returns uuid language sql stable as $$
      select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
    $$;
    grant usage on schema public, auth, extensions to anon, authenticated, service_role;
    grant execute on function auth.uid() to anon, authenticated, service_role;
  `);

  const names = (await readdir(migrationDirectory)).filter((name) => /^\d+.*\.sql$/.test(name)).sort();
  for (const name of names) {
    const sql = await readFile(resolve(migrationDirectory, name), "utf8");
    try {
      await db.exec(sql);
    } catch (error) {
      throw new Error(`${name}: ${error.code} ${error.message}`, { cause: error });
    }
    report.migrations.push({ name, sha256: createHash("sha256").update(sql).digest("hex") });
    console.log(`PASS ${name}`);

    if (name.startsWith("0020_")) {
      await db.exec(`
        insert into auth.users values ('${account}'), ('${outsider}');
        insert into public.markets (id, condition_id, question)
          values ('${market}', 'legacy-condition', 'Migration fixture?');
        insert into public.account_activities
          (account_id, activity_key, activity_type, condition_id, amount_pusd,
           occurred_at, raw_payload)
          values ('${account}', 'legacy-activity-key', 'REDEEM', 'legacy-condition',
                  10, now() - interval '30 days', '{"preserved":true}');
        insert into public.order_intents
          (account_id, market_id, intent_hash, run_id, bucket, outcome, token_id,
           side, price, size, notional_usd, edge_after_costs, strategy)
          values ('${account}', '${market}', repeat('a', 64), 'migration-fixture',
                  'general', 'YES', 'fixture-token', 'BUY', 0.5, 10, 5, 0.1, 'fixture');
      `);
    }
    if (name.startsWith("0021_")) {
      const row = (await db.query("select ledger_scope, scope_reason, raw_payload from public.account_activities")).rows[0];
      assert.equal(row.ledger_scope, "bot");
      assert.equal(row.scope_reason, null);
      assert.deepEqual(row.raw_payload, { preserved: true });
      report.checks.push("0021 preserves existing lifecycle audit data in strict scope");
      await expectFailure(
        "update public.account_activities set ledger_scope='quarantine'",
        "23514", "0021 quarantine requires a reason",
      );
      await expectFailure(
        "update public.account_activities set ledger_scope='quarantine', scope_reason=' '",
        "23514", "0021 quarantine rejects blank reason",
      );
      await db.exec(`
        set role service_role;
        insert into public.reconciliation_quarantine
          (account_id, kind, external_key, reason, condition_id, notional_usd)
          values ('${account}', 'activity', 'legacy-activity-key',
                  'prebaseline_external_activity', 'legacy-condition', 10);
        update public.account_activities
          set ledger_scope='quarantine', scope_reason='prebaseline_external_activity';
        reset role;
        set request.jwt.claim.sub = '${account}';
        set role authenticated;
      `);
      assert.equal((await db.query("select ledger_scope from public.account_activities")).rows.length, 1);
      assert.equal((await db.query("select kind from public.reconciliation_quarantine")).rows[0].kind, "activity");
      await expectFailure(
        "update public.account_activities set ledger_scope='bot', scope_reason=null",
        "42501", "0021 owner cannot change ledger scope",
      );
      await db.exec(`reset role; set request.jwt.claim.sub = '${outsider}'; set role authenticated;`);
      assert.equal((await db.query("select ledger_scope from public.account_activities")).rows.length, 0);
      assert.equal((await db.query("select kind from public.reconciliation_quarantine")).rows.length, 0);
      await db.exec("reset role; set role anon;");
      await expectFailure(
        "select ledger_scope from public.account_activities",
        "42501", "0021 anonymous scope reads denied",
      );
      await db.exec("reset role;");
      report.checks.push("0021 activity records readable only by owner or service role");
    }
    if (name.startsWith("0022_")) {
      assert.equal((await db.query("select max_commitment_usd from public.order_intents")).rows[0].max_commitment_usd, null);
      report.checks.push("0022 leaves legacy commitment unknown");
      await expectFailure(
        "update public.order_intents set max_commitment_usd=4.99",
        "23514", "0022 rejects BUY commitment below limit price times size",
      );
      await expectFailure(
        "update public.order_intents set max_commitment_usd=0",
        "23514", "0022 rejects zero commitment",
      );
      for (const value of ["NaN", "Infinity", "-Infinity"]) {
        await expectFailure(
          `update public.order_intents set max_commitment_usd='${value}'::numeric`,
          "23514", `0022 rejects non-finite commitment ${value}`,
        );
      }
      await db.exec("update public.order_intents set max_commitment_usd=5, condition_id='legacy-condition'");
      assert.equal(Number((await db.query("select max_commitment_usd from public.order_intents")).rows[0].max_commitment_usd), 5);
      assert.equal((await db.query("select public.polybot_schema_version() as version")).rows[0].version, 22);
      report.checks.push("0022 accepts valid commitment and reports schema 22");
    }
  }
  report.status = "passed";
  await writeFile(resolve(runtimeDirectory, "migration-verification.json"), JSON.stringify(report, null, 2));
  console.log(`PASS ${report.migrations.length} unmodified migrations; ${report.checks.length} upgrade, constraint, and RLS checks`);
} finally {
  await db.close();
}
