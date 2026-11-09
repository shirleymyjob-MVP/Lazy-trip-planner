-- V2 Phase 1：私人行程樂觀鎖。
-- 部署會讀寫 version 的程式之前，先在 Supabase SQL editor 執行本檔。
-- 應用程式不會自動跑這段 SQL，也不要對真實資料做其他修改。
--
-- 未加欄位就部署新程式：insert/update 會失敗（未知欄位），
-- 或者讀到的列沒有 version。程式會拒絕儲存，不會跳過版本檢查。
--
-- 本檔可以先於新程式執行。舊版程式不寫 version，欄位會維持預設 1，
-- 現有讀寫仍可用。部署新程式之後，不要再留舊版同時寫入：
-- 舊版 UPDATE 沒有 version 條件，可以覆蓋新版剛寫入的內容。

alter table public.trips
  add column if not exists version integer not null default 1;

comment on column public.trips.version is
  'Optimistic lock. Compare-and-swap: update where version = read version and set version = read version + 1.';

-- 核對（預期一行：integer、NO、預設 1；null_versions = 0）：
-- select column_name, data_type, is_nullable, column_default
-- from information_schema.columns
-- where table_schema = 'public'
--   and table_name = 'trips'
--   and column_name = 'version';
--
-- select count(*) as trips,
--        count(*) filter (where version is null) as null_versions,
--        coalesce(min(version), 0) as min_version
-- from public.trips;

-- 回滾（先確認新版程式已停用。會丟掉版本號）：
-- alter table public.trips drop column if exists version;
