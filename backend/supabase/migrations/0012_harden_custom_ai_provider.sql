begin;

-- 0011 briefly accepted HTTP relay URLs. Fail closed for projects that applied
-- that migration before the application-layer SSRF controls were added.
update public.account_runtime_profiles
set
  ai_provider = 'platform',
  ai_base_url = null,
  ai_credential_id = null,
  auto_run_enabled = false,
  next_run_at = null,
  version = version + 1
where ai_provider = 'custom'
  and (
    ai_base_url is null
    or length(ai_base_url) not between 8 and 256
    or ai_base_url !~ '^https://[^/?#[:space:]@]+(/[^?#[:space:]]*)?$'
  );

alter table public.account_runtime_profiles
  drop constraint if exists account_runtime_profiles_custom_base_url;
alter table public.account_runtime_profiles
  add constraint account_runtime_profiles_custom_base_url
    check (
      (
        ai_provider = 'custom'
        and ai_base_url is not null
        and length(ai_base_url) between 8 and 256
        and ai_base_url ~ '^https://[^/?#[:space:]@]+(/[^?#[:space:]]*)?$'
      )
      or (ai_provider <> 'custom' and ai_base_url is null)
    );

-- The Control API binds p_account_id to the verified Supabase JWT. This
-- SECURITY DEFINER RPC must therefore remain callable only by service_role.
revoke all on function public.update_account_runtime_profile(
  uuid, bigint, text, text, text, uuid, uuid, uuid, text, boolean, integer
) from public, anon, authenticated, service_role;
grant execute on function public.update_account_runtime_profile(
  uuid, bigint, text, text, text, uuid, uuid, uuid, text, boolean, integer
) to service_role;

notify pgrst, 'reload schema';

commit;
