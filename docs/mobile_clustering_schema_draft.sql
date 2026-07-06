-- Stav mobile clustering app schema draft v2
-- Offline/staging artifact only. Do not run against production without explicit approval.
-- Adds group-first review, stable external IDs, catalog cards, review queue, and projection safety.

create extension if not exists pgcrypto;

create type photo_status as enum ('unreviewed', 'assigned', 'skipped', 'blocked', 'archived');
create type shot_role as enum ('studio', 'live', 'macro', 'model_lifestyle', 'unknown');
create type review_group_status as enum ('open', 'partially_resolved', 'resolved', 'needs_or_review', 'archived');
create type review_group_type as enum (
  'same_new_product_group',
  'link_existing_product',
  'link_working_cluster',
  'same_design_sibling',
  'split_likely',
  'singleton'
);
create type confidence_bucket as enum ('high', 'medium', 'low');
create type cluster_status as enum ('proposed', 'human_confirmed', 'split', 'rejected');
create type product_cluster_type as enum ('existing_catalog_product', 'new_product', 'same_design_sibling', 'unknown');
create type product_cluster_status as enum ('active', 'needs_more_photos', 'needs_or_review', 'ready_for_raw_intake', 'merged', 'archived');
create type candidate_type as enum ('existing_product', 'working_cluster', 'same_design', 'image_cluster', 'review_group');
create type photo_role as enum ('primary', 'supporting', 'duplicate', 'rejected');
create type relation_type as enum ('same_product', 'same_design_different_product', 'possible_variant_or_offering', 'different_design', 'unknown');
create type difference_type as enum ('metal_color', 'metal_type', 'stone_type', 'stone_color', 'size', 'texture_finish', 'stone_count', 'shape_structure', 'product_type', 'unknown');
create type decision_type as enum (
  'approve_group_as_one_product',
  'link_group_to_existing_product',
  'link_group_to_working_cluster',
  'create_new_product_cluster',
  'split_review_group',
  'same_design_different_product',
  'not_same_product',
  'not_sure',
  'skip',
  'merge_clusters',
  'split_photo_from_cluster',
  'undo_decision',
  'needs_more_images',
  'send_to_or_review'
);

create table app_config (
  id boolean primary key default true,
  environment text not null check (environment in ('local', 'staging', 'production')) default 'staging',
  production_writes_enabled boolean not null default false,
  check (id)
);

insert into app_config (id, environment, production_writes_enabled)
values (true, 'staging', false)
on conflict (id) do nothing;

create table app_users (
  id uuid primary key default gen_random_uuid(),
  display_name text not null,
  role text not null check (role in ('dalia', 'eyal', 'or', 'hal_admin')),
  created_at timestamptz not null default now()
);

create table import_batches (
  id uuid primary key default gen_random_uuid(),
  external_batch_id text not null unique,
  detector_policy text not null,
  schema_version text not null,
  imported_at timestamptz not null default now(),
  manifest_json jsonb not null default '{}'::jsonb
);

create table catalog_products (
  id uuid primary key default gen_random_uuid(),
  external_product_id text not null unique,
  catalog_id text,
  display_name_he text,
  design_id text,
  product_type text,
  thumbnail_url text,
  metadata_json jsonb not null default '{}'::jsonb,
  imported_batch_id uuid references import_batches(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table catalog_product_media (
  id uuid primary key default gen_random_uuid(),
  catalog_product_id uuid not null references catalog_products(id),
  external_photo_id text,
  thumbnail_url text not null,
  full_image_url text not null,
  shot_role shot_role not null default 'unknown',
  sort_order int not null default 0
);

create table photos (
  id uuid primary key default gen_random_uuid(),
  external_photo_id text not null unique,
  import_batch_id uuid references import_batches(id),
  source_kind text not null check (source_kind in ('raw', 'catalog', 'shopify', 'drive', 'imported')),
  source_ref text not null,
  thumbnail_url text not null,
  full_image_url text not null,
  shot_role shot_role not null default 'unknown',
  status photo_status not null default 'unreviewed',
  created_at timestamptz not null default now(),
  unique (source_kind, source_ref)
);

create table image_clusters (
  id uuid primary key default gen_random_uuid(),
  external_image_cluster_id text unique,
  representative_photo_id uuid references photos(id),
  status cluster_status not null default 'proposed',
  created_by text not null check (created_by in ('detector', 'human', 'system')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table product_clusters (
  id uuid primary key default gen_random_uuid(),
  external_product_cluster_id text unique,
  cluster_type product_cluster_type not null default 'unknown',
  catalog_product_id uuid references catalog_products(id),
  reference_catalog_product_id uuid references catalog_products(id),
  design_id text,
  human_label text,
  representative_photo_id uuid references photos(id),
  status product_cluster_status not null default 'active',
  merged_into_cluster_id uuid references product_clusters(id),
  created_by uuid references app_users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (
    (cluster_type = 'existing_catalog_product' and catalog_product_id is not null)
    or cluster_type <> 'existing_catalog_product'
  ),
  check (
    (status = 'merged' and merged_into_cluster_id is not null)
    or (status <> 'merged' and merged_into_cluster_id is null)
  )
);

create table review_groups (
  id uuid primary key default gen_random_uuid(),
  external_review_group_id text not null unique,
  import_batch_id uuid references import_batches(id),
  group_type review_group_type not null,
  status review_group_status not null default 'open',
  representative_photo_id uuid references photos(id),
  confidence_bucket confidence_bucket not null default 'medium',
  recommended_action_he text not null,
  evidence_summary_he text,
  resolved_product_cluster_id uuid references product_clusters(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table review_group_photos (
  review_group_id uuid not null references review_groups(id),
  photo_id uuid not null references photos(id),
  sort_order int not null default 0,
  selected_by_default boolean not null default true,
  resolved_at timestamptz,
  primary key (review_group_id, photo_id)
);

create table decisions (
  id uuid primary key default gen_random_uuid(),
  external_decision_id text unique,
  actor_id uuid references app_users(id),
  review_group_id uuid references review_groups(id),
  photo_id uuid references photos(id),
  source_cluster_id uuid references product_clusters(id),
  target_product_cluster_id uuid references product_clusters(id),
  target_catalog_product_id uuid references catalog_products(id),
  decision_type decision_type not null,
  decision_payload_json jsonb not null default '{}'::jsonb,
  undone_by_decision_id uuid references decisions(id),
  created_at timestamptz not null default now()
);

create table image_cluster_photos (
  image_cluster_id uuid not null references image_clusters(id),
  photo_id uuid not null references photos(id),
  confidence numeric,
  human_confirmed boolean not null default false,
  decision_id uuid references decisions(id),
  created_at timestamptz not null default now(),
  primary key (image_cluster_id, photo_id)
);

create table image_cluster_assignments (
  image_cluster_id uuid primary key references image_clusters(id),
  product_cluster_id uuid not null references product_clusters(id),
  decision_id uuid references decisions(id),
  created_at timestamptz not null default now()
);

create table product_cluster_photos (
  product_cluster_id uuid not null references product_clusters(id),
  photo_id uuid not null references photos(id),
  role photo_role not null default 'supporting',
  active boolean not null default true,
  confirmed_by uuid references app_users(id),
  decision_id uuid references decisions(id),
  created_at timestamptz not null default now(),
  primary key (product_cluster_id, photo_id)
);

-- One active non-rejected product assignment per photo.
create unique index uniq_active_product_assignment_per_photo
on product_cluster_photos(photo_id)
where active and role <> 'rejected';

create table product_cluster_relations (
  id uuid primary key default gen_random_uuid(),
  source_product_cluster_id uuid not null references product_clusters(id),
  target_product_cluster_id uuid references product_clusters(id),
  target_catalog_product_id uuid references catalog_products(id),
  relation_type relation_type not null,
  difference_type difference_type not null default 'unknown',
  needs_or_review boolean not null default false,
  decision_id uuid references decisions(id),
  created_at timestamptz not null default now(),
  check (target_product_cluster_id is not null or target_catalog_product_id is not null)
);

create table candidate_suggestions (
  id uuid primary key default gen_random_uuid(),
  import_batch_id uuid references import_batches(id),
  photo_id uuid references photos(id),
  review_group_id uuid references review_groups(id),
  candidate_type candidate_type not null,
  candidate_product_id uuid references catalog_products(id),
  candidate_product_cluster_id uuid references product_clusters(id),
  candidate_image_cluster_id uuid references image_clusters(id),
  candidate_review_group_id uuid references review_groups(id),
  rank int not null check (rank >= 1),
  score numeric,
  margin numeric,
  supporting_photo_ids uuid[] not null default '{}',
  explanation_he text,
  generated_by text not null,
  created_at timestamptz not null default now(),
  check (photo_id is not null or review_group_id is not null)
);

create table review_queue_items (
  id uuid primary key default gen_random_uuid(),
  review_group_id uuid references review_groups(id),
  photo_id uuid references photos(id),
  product_cluster_id uuid references product_clusters(id),
  priority int not null default 100,
  reason text not null,
  notes text,
  status text not null check (status in ('open', 'resolved', 'archived')) default 'open',
  created_by_decision_id uuid references decisions(id),
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

create index idx_review_groups_status_confidence on review_groups(status, confidence_bucket, created_at);
create index idx_review_group_photos_photo on review_group_photos(photo_id);
create index idx_photos_status_created on photos(status, created_at);
create index idx_candidate_suggestions_group_rank on candidate_suggestions(review_group_id, rank);
create index idx_candidate_suggestions_photo_rank on candidate_suggestions(photo_id, rank);
create index idx_product_clusters_status on product_clusters(status, updated_at);
create index idx_decisions_group_created on decisions(review_group_id, created_at);
create index idx_decisions_photo_created on decisions(photo_id, created_at);
create index idx_decisions_target_cluster_created on decisions(target_product_cluster_id, created_at);

-- View for mobile group queue.
create view review_group_queue_cards as
select
  rg.id as review_group_id,
  rg.external_review_group_id,
  rg.group_type,
  rg.status,
  rg.confidence_bucket,
  rg.recommended_action_he,
  rg.evidence_summary_he,
  count(rgp.photo_id) as photo_count,
  rg.representative_photo_id,
  rg.updated_at
from review_groups rg
left join review_group_photos rgp on rgp.review_group_id = rg.id
where rg.status in ('open', 'partially_resolved', 'needs_or_review')
group by rg.id;

-- View for mobile product cards: current cluster image counts.
create view product_cluster_card_counts as
select
  pc.id as product_cluster_id,
  pc.cluster_type,
  cp.external_product_id,
  cp.catalog_id,
  coalesce(pc.human_label, cp.display_name_he, cp.catalog_id, pc.external_product_cluster_id) as display_name,
  pc.design_id,
  pc.status,
  count(pcp.photo_id) filter (where pcp.active and pcp.role <> 'rejected') as linked_photo_count,
  min(pcp.created_at) as first_photo_linked_at,
  max(pcp.created_at) as last_photo_linked_at
from product_clusters pc
left join catalog_products cp on cp.id = pc.catalog_product_id
left join product_cluster_photos pcp on pcp.product_cluster_id = pc.id
group by pc.id, cp.id;
