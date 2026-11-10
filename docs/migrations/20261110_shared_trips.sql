-- 共同行程：is_shared、trip_members、trip_invites。
-- 只可在 Supabase SQL editor 由產品負責人手動執行。應用程式不會自動跑這段 SQL。
-- 不要對真實行程 payload 或 Storage 物件做其他修改。不要部署程式，直到本檔成功跑完。
--
-- 已核對（只讀，2026-11-10）：
--   public.trips.id 是 uuid
--   public.trips 的 RLS = true，而且 FORCE RLS = true
-- 尚未核對、本檔假設不存在：
--   trips.is_shared
--   public.trip_members
--   public.trip_invites
-- 如果 preflight 失敗，停低。不要改語句去遷就另一種型別或已存在的表。
-- 把錯誤原文交返，再決定點改。
--
-- 預期（未被告知之前）：
--   trips.id            uuid，而且是單一欄位主鍵或唯一鍵
--   trips.owner_sub     text，或 character varying（成員欄會用同一個字串型別）
--   trips.version       integer（本檔不會再新增）
--   RLS / FORCE RLS     兩者都係 true
--   is_shared、trip_members、trip_invites  都不存在

do $migration$
declare
  id_type text;
  id_udt text;
  owner_type text;
  owner_udt text;
  owner_len integer;
  owner_sql text;
  version_type text;
  rls boolean;
  force_rls boolean;
  pk_ok boolean;
  cols text;
  constraint_names text;
begin
  if to_regclass('public.trips') is null then
    raise exception 'preflight failed: public.trips does not exist. Stop. Do not create a replacement table.';
  end if;

  select string_agg(c.column_name || ' ' || c.data_type, ', ' order by c.ordinal_position)
    into cols
  from information_schema.columns c
  where c.table_schema = 'public'
    and c.table_name = 'trips';
  raise notice 'preflight public.trips columns: %', cols;

  select string_agg(con.conname::text || ' ' || con.contype::text, ', ' order by con.conname)
    into constraint_names
  from pg_constraint con
  where con.conrelid = 'public.trips'::regclass;
  raise notice 'preflight public.trips constraints: %', coalesce(constraint_names, '(none)');

  select c.relrowsecurity, c.relforcerowsecurity
    into rls, force_rls
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public'
    and c.relname = 'trips';
  raise notice 'preflight public.trips rls=% force_rls=%', rls, force_rls;
  if rls is distinct from true or force_rls is distinct from true then
    raise exception
      'preflight failed: public.trips RLS (%) or FORCE RLS (%) is not the confirmed true/true. Stop and do not continue.',
      rls, force_rls;
  end if;

  select c.data_type, c.udt_name
    into id_type, id_udt
  from information_schema.columns c
  where c.table_schema = 'public'
    and c.table_name = 'trips'
    and c.column_name = 'id';
  if id_udt is null then
    raise exception 'preflight failed: public.trips.id is missing. Stop.';
  end if;
  if id_udt <> 'uuid' or id_type <> 'uuid' then
    raise exception
      'preflight failed: public.trips.id type is % (udt %). Expected uuid. Stop. Do not add a foreign key of another type.',
      id_type, id_udt;
  end if;

  select exists (
    select 1
    from pg_constraint con
    join pg_attribute att
      on att.attrelid = con.conrelid
     and att.attnum = any (con.conkey)
    where con.conrelid = 'public.trips'::regclass
      and con.contype in ('p', 'u')
      and att.attname = 'id'
      and array_length(con.conkey, 1) = 1
  ) into pk_ok;
  if not pk_ok then
    raise exception
      'preflight failed: public.trips.id is not a single-column primary or unique key, so a uuid foreign key cannot be added. Stop.';
  end if;

  select c.data_type, c.udt_name, c.character_maximum_length
    into owner_type, owner_udt, owner_len
  from information_schema.columns c
  where c.table_schema = 'public'
    and c.table_name = 'trips'
    and c.column_name = 'owner_sub';
  if owner_udt is null then
    raise exception 'preflight failed: public.trips.owner_sub is missing. Stop.';
  end if;
  if owner_udt = 'text' then
    owner_sql := 'text';
  elsif owner_udt = 'varchar' and owner_len is null then
    owner_sql := 'character varying';
  elsif owner_udt = 'varchar' then
    owner_sql := format('character varying(%s)', owner_len);
  else
    raise exception
      'preflight failed: public.trips.owner_sub type is % (udt %). Expected text or character varying. Stop. Do not invent another member_sub type.',
      owner_type, owner_udt;
  end if;
  raise notice 'preflight owner_sub sql type: %', owner_sql;

  select c.data_type
    into version_type
  from information_schema.columns c
  where c.table_schema = 'public'
    and c.table_name = 'trips'
    and c.column_name = 'version';
  if version_type is distinct from 'integer' then
    raise exception
      'preflight failed: public.trips.version is %. Expected integer. This migration does not add version. Stop.',
      coalesce(version_type, 'missing');
  end if;

  if exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = 'trips'
      and c.column_name = 'is_shared'
  ) then
    raise exception
      'preflight failed: public.trips.is_shared already exists. This migration assumes it does not. Stop and review the live column before continuing.';
  end if;

  if to_regclass('public.trip_members') is not null then
    raise exception
      'preflight failed: public.trip_members already exists. This migration assumes it does not. Stop and review the live table before continuing.';
  end if;
  if to_regclass('public.trip_invites') is not null then
    raise exception
      'preflight failed: public.trip_invites already exists. This migration assumes it does not. Stop and review the live table before continuing.';
  end if;

  if not exists (select 1 from pg_roles where rolname = 'anon')
     or not exists (select 1 from pg_roles where rolname = 'authenticated')
     or not exists (select 1 from pg_roles where rolname = 'service_role') then
    raise exception
      'preflight failed: expected Supabase roles anon, authenticated, and service_role. Stop.';
  end if;

  execute 'alter table public.trips add column is_shared boolean not null default false';
  execute $sql$
    comment on column public.trips.is_shared is
      'Owner invite upgrades this trip in place. Members can access only while this is true.'
  $sql$;

  execute format($sql$
    create table public.trip_members (
      trip_id uuid not null references public.trips (id) on delete cascade,
      member_sub %s not null,
      display_name text not null,
      joined_at timestamptz not null default now(),
      primary key (trip_id, member_sub)
    )
  $sql$, owner_sql);
  execute 'create index trip_members_member_sub_idx on public.trip_members (member_sub)';
  execute $sql$
    comment on table public.trip_members is
      'Current members only. Access still requires trips.is_shared. No client role can read this table.'
  $sql$;

  execute format($sql$
    create table public.trip_invites (
      token_hash text not null,
      trip_id uuid not null references public.trips (id) on delete cascade,
      created_by_sub %s not null,
      created_at timestamptz not null default now(),
      expires_at timestamptz not null,
      revoked_at timestamptz,
      constraint trip_invites_token_hash_key unique (token_hash)
    )
  $sql$, owner_sql);
  execute 'create index trip_invites_trip_id_idx on public.trip_invites (trip_id)';
  execute $sql$
    comment on table public.trip_invites is
      'Stores only the SHA-256 hex of the invite token. Never add a raw token column.'
  $sql$;

  execute 'alter table public.trip_members enable row level security';
  execute 'alter table public.trip_members force row level security';
  execute 'alter table public.trip_invites enable row level security';
  execute 'alter table public.trip_invites force row level security';

  -- No policies. Default deny for roles without BYPASSRLS.
  -- service_role bypasses RLS but still needs table grants. anon/authenticated get nothing.
  execute 'revoke all on table public.trip_members from public, anon, authenticated';
  execute 'revoke all on table public.trip_invites from public, anon, authenticated';
  execute 'grant select, insert, update, delete on table public.trip_members to service_role';
  execute 'grant select, insert, update, delete on table public.trip_invites to service_role';

  if exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = 'trip_invites'
      and c.column_name = 'token'
  ) then
    raise exception 'verification failed: trip_invites.token must not exist.';
  end if;

  if not exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = 'trips'
      and c.column_name = 'is_shared'
      and c.data_type = 'boolean'
      and c.is_nullable = 'NO'
      and coalesce(c.column_default, '') like '%false%'
  ) then
    raise exception 'verification failed: trips.is_shared is not boolean not null default false.';
  end if;

  if not exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = 'trip_members'
      and c.column_name = 'trip_id'
      and c.udt_name = 'uuid'
  ) or not exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = 'trip_invites'
      and c.column_name = 'trip_id'
      and c.udt_name = 'uuid'
  ) then
    raise exception 'verification failed: trip_id foreign keys are not uuid.';
  end if;

  if not exists (
    select 1
    from pg_constraint con
    where con.conrelid = 'public.trip_members'::regclass
      and con.contype = 'p'
  ) then
    raise exception 'verification failed: trip_members primary key is missing.';
  end if;

  if not exists (
    select 1 from pg_indexes
    where schemaname = 'public'
      and tablename = 'trip_members'
      and indexname = 'trip_members_member_sub_idx'
  ) or not exists (
    select 1 from pg_indexes
    where schemaname = 'public'
      and tablename = 'trip_invites'
      and indexname = 'trip_invites_trip_id_idx'
  ) or not exists (
    select 1
    from pg_constraint con
    where con.conrelid = 'public.trip_invites'::regclass
      and con.conname = 'trip_invites_token_hash_key'
      and con.contype = 'u'
  ) then
    raise exception 'verification failed: expected indexes on member_sub, trip_id, or token_hash are missing.';
  end if;

  select c.relrowsecurity, c.relforcerowsecurity
    into rls, force_rls
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relname = 'trip_members';
  if rls is distinct from true or force_rls is distinct from true then
    raise exception 'verification failed: trip_members RLS/FORCE RLS is not enabled.';
  end if;
  select c.relrowsecurity, c.relforcerowsecurity
    into rls, force_rls
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relname = 'trip_invites';
  if rls is distinct from true or force_rls is distinct from true then
    raise exception 'verification failed: trip_invites RLS/FORCE RLS is not enabled.';
  end if;

  if has_table_privilege('anon', 'public.trip_members', 'select')
     or has_table_privilege('anon', 'public.trip_members', 'insert')
     or has_table_privilege('anon', 'public.trip_members', 'update')
     or has_table_privilege('anon', 'public.trip_members', 'delete')
     or has_table_privilege('authenticated', 'public.trip_members', 'select')
     or has_table_privilege('authenticated', 'public.trip_members', 'insert')
     or has_table_privilege('authenticated', 'public.trip_members', 'update')
     or has_table_privilege('authenticated', 'public.trip_members', 'delete')
     or has_table_privilege('anon', 'public.trip_invites', 'select')
     or has_table_privilege('anon', 'public.trip_invites', 'insert')
     or has_table_privilege('anon', 'public.trip_invites', 'update')
     or has_table_privilege('anon', 'public.trip_invites', 'delete')
     or has_table_privilege('authenticated', 'public.trip_invites', 'select')
     or has_table_privilege('authenticated', 'public.trip_invites', 'insert')
     or has_table_privilege('authenticated', 'public.trip_invites', 'update')
     or has_table_privilege('authenticated', 'public.trip_invites', 'delete')
  then
    raise exception 'verification failed: anon or authenticated still has privileges on the new tables.';
  end if;

  if not has_table_privilege('service_role', 'public.trip_members', 'select')
     or not has_table_privilege('service_role', 'public.trip_members', 'insert')
     or not has_table_privilege('service_role', 'public.trip_members', 'update')
     or not has_table_privilege('service_role', 'public.trip_members', 'delete')
     or not has_table_privilege('service_role', 'public.trip_invites', 'select')
     or not has_table_privilege('service_role', 'public.trip_invites', 'insert')
     or not has_table_privilege('service_role', 'public.trip_invites', 'update')
     or not has_table_privilege('service_role', 'public.trip_invites', 'delete')
  then
    raise exception 'verification failed: service_role is missing table privileges. The app cannot use the new tables.';
  end if;
end
$migration$;

-- 核對（遷移成功後可以再跑。預期見上面的 verification failed 條件的相反）：
-- select column_name, data_type, is_nullable, column_default
-- from information_schema.columns
-- where table_schema = 'public'
--   and table_name = 'trips'
--   and column_name in ('id', 'owner_sub', 'version', 'is_shared')
-- order by column_name;
--
-- select c.relname, c.relrowsecurity, c.relforcerowsecurity
-- from pg_class c
-- join pg_namespace n on n.oid = c.relnamespace
-- where n.nspname = 'public'
--   and c.relname in ('trips', 'trip_members', 'trip_invites')
-- order by c.relname;
--
-- select table_name, column_name
-- from information_schema.columns
-- where table_schema = 'public'
--   and table_name = 'trip_invites'
--   and column_name = 'token';
--
-- select grantee, table_name, privilege_type
-- from information_schema.role_table_grants
-- where table_schema = 'public'
--   and table_name in ('trip_members', 'trip_invites')
--   and grantee in ('anon', 'authenticated', 'service_role')
-- order by table_name, grantee, privilege_type;

-- 回滾（先停用這版程式。只刪新表、索引同 is_shared。
-- 不要刪 version。不要刪行程 payload。不要刪 Storage 物件。）：
-- drop table if exists public.trip_invites;
-- drop table if exists public.trip_members;
-- alter table public.trips drop column if exists is_shared;
