begin;

-- 0011 added the custom runtime provider but the credential-ingestion RPC from
-- 0007 still rejected the new provider. Replace it without changing the
-- ciphertext-only storage boundary or tenant-scoped rotation behavior.
create or replace function public.store_ai_credential(
  p_account_id uuid,
  p_provider text,
  p_label text,
  p_algorithm text,
  p_key_version integer,
  p_aad_version bigint,
  p_nonce text,
  p_ciphertext text,
  p_encrypted_data_key text,
  p_fingerprint text,
  p_last_four text
)
returns setof public.credential_refs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_version bigint;
  v_old_credential_id uuid;
  v_saved public.credential_refs%rowtype;
begin
  if p_provider not in ('openai', 'anthropic', 'openrouter', 'litellm', 'custom') then
    raise exception 'unsupported AI provider' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':ai:' || p_provider, 0)
  );

  select coalesce(max(credential.version), 0) + 1 into v_version
  from public.credential_refs as credential
  where credential.account_id = p_account_id
    and credential.kind = 'ai_api_key'
    and credential.provider = p_provider;

  select credential.id into v_old_credential_id
  from public.credential_refs as credential
  where credential.account_id = p_account_id
    and credential.kind = 'ai_api_key'
    and credential.provider = p_provider
    and credential.status = 'active'
  for update;

  update public.credential_refs as credential
  set status = 'revoked', revoked_at = now()
  where credential.account_id = p_account_id
    and credential.kind = 'ai_api_key'
    and credential.provider = p_provider
    and credential.status = 'active';

  insert into public.credential_refs (
    account_id, kind, provider, label, status, cipher_algorithm,
    key_version, aad_version, nonce, ciphertext, encrypted_data_key,
    fingerprint, last_four, version, rotated_at
  )
  values (
    p_account_id, 'ai_api_key', p_provider, p_label, 'active', p_algorithm,
    p_key_version, p_aad_version, p_nonce, p_ciphertext, p_encrypted_data_key,
    p_fingerprint, p_last_four, v_version,
    case when v_version > 1 then now() else null end
  )
  returning * into v_saved;

  if v_old_credential_id is not null then
    update public.account_runtime_profiles as profile
    set ai_credential_id = v_saved.id, version = profile.version + 1
    where profile.account_id = p_account_id
      and profile.ai_credential_id = v_old_credential_id;
  end if;
  return next v_saved;
  return;
end;
$$;

revoke all on function public.store_ai_credential(
  uuid, text, text, text, integer, bigint, text, text, text, text, text
) from public, anon, authenticated, service_role;
grant execute on function public.store_ai_credential(
  uuid, text, text, text, integer, bigint, text, text, text, text, text
) to service_role;

-- A stable readiness sentinel prevents a partially migrated deployment from
-- appearing healthy merely because its older tables are reachable.
create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$
  select 14;
$$;

revoke all on function public.polybot_schema_version()
from public, anon, authenticated, service_role;
grant execute on function public.polybot_schema_version() to service_role;

notify pgrst, 'reload schema';

commit;
