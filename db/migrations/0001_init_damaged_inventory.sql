-- 0001_init_damaged_inventory.sql
-- Idempotent initialization for damaged inventory storage + view + upsert.

-- 1) Schema
create schema if not exists damaged;

-- 2) Base table
create table if not exists damaged.inventory (
  inventory_item_id    bigint primary key,
  product_id           bigint not null,
  variant_id           bigint not null,
  handle               text not null,
  condition            text check (condition in ('light','moderate','heavy')),
  available            integer not null default 0,
  last_shopify_sync_at timestamptz not null default now(),
  last_webhook_at      timestamptz not null default now(),
  last_source          text not null default 'webhook', -- 'webhook' | 'reconcile'
  notes                text,
  title                text,
  sku                  text,
  barcode              text
);

-- Helpful indexes
create index if not exists idx_dmg_inventory_product_id on damaged.inventory (product_id);
create index if not exists idx_dmg_inventory_handle     on damaged.inventory (handle);
create index if not exists idx_dmg_inventory_last_sync  on damaged.inventory (last_shopify_sync_at);

-- 3) View
create or replace view damaged.inventory_view as
select
  i.*,
  case when i.available > 0 then 'in_stock' else 'out_of_stock' end as stock_status
from damaged.inventory i;

-- 4) (Optional) Changelog for admin/audit
create table if not exists damaged.changelog (
  id bigserial primary key,
  inventory_item_id bigint not null,
  change_type text not null,           -- 'availability','publish_state','override', etc.
  old_value text,
  new_value text,
  at timestamptz not null default now(),
  actor text default 'system'
);
create index if not exists idx_dmg_changelog_item on damaged.changelog (inventory_item_id);

-- 5) Upsert function (used by DBS)
create or replace function damaged.upsert_inventory(
  _inventory_item_id    bigint,
  _product_id           bigint,
  _variant_id           bigint,
  _handle               text,
  _condition            text,
  _available            integer,
  _source               text default 'webhook',
  _title                text default null,
  _sku                  text default null,
  _barcode              text default null
) returns void
language plpgsql
as $$
begin
  insert into damaged.inventory (
    inventory_item_id, product_id, variant_id, handle, condition,
    available, last_shopify_sync_at, last_webhook_at, last_source,
    title, sku, barcode
  ) values (
    _inventory_item_id, _product_id, _variant_id, _handle, _condition,
    _available,
    case when _source = 'reconcile' then now() else now() end,  -- both set now(); semantics differ via _source
    case when _source = 'webhook'   then now() else coalesce((select last_webhook_at from damaged.inventory i where i.inventory_item_id=_inventory_item_id), now()) end,
    _source,
    _title, _sku, _barcode
  )
  on conflict (inventory_item_id) do update
  set
    product_id           = excluded.product_id,
    variant_id           = excluded.variant_id,
    handle               = excluded.handle,
    condition            = excluded.condition,
    available            = excluded.available,
    last_shopify_sync_at = case when excluded.last_source='reconcile' then now() else damaged.inventory.last_shopify_sync_at end,
    last_webhook_at      = case when excluded.last_source='webhook'   then now() else damaged.inventory.last_webhook_at end,
    last_source          = excluded.last_source,
    title                = coalesce(excluded.title, damaged.inventory.title),
    sku                  = coalesce(excluded.sku, damaged.inventory.sku),
    barcode              = coalesce(excluded.barcode, damaged.inventory.barcode);
end;
$$;

-- (Optional) basic grants if you’re using PostgREST directly without Supabase Row Level Security (RLS):
-- grant usage on schema damaged to anon, authenticated, service_role;
-- grant select on damaged.inventory_view to anon, authenticated;
-- (Usually you’ll call via backend using the service key, so explicit grants may be unnecessary.)