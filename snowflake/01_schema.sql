-- ============================================================================
-- Basis Tracker — Snowflake schema (migrated from Supabase Postgres)
-- ----------------------------------------------------------------------------
-- Run once against the target Snowflake database. Set your database first:
--     USE DATABASE <YOUR_DB>;
-- then run this whole file. Creates schema BASIS_TRACKER and all tables.
--
-- Type mapping from Postgres:
--   text                      -> STRING
--   integer / smallint        -> NUMBER(38,0)
--   bigint (SERIAL/identity)   -> NUMBER(38,0) IDENTITY(1,1)
--   double precision / real   -> FLOAT
--   timestamp with time zone  -> TIMESTAMP_TZ
--   now()                     -> CURRENT_TIMESTAMP()
--
-- Notes:
--  * Column names are created UNQUOTED (Snowflake folds to UPPERCASE); the app's
--    unquoted lowercase SQL matches fine. The Snowflake DB backend lowercases
--    result-set keys so existing r['col'] access keeps working.
--  * PRIMARY KEY / UNIQUE are informational in Snowflake (not enforced) but are
--    kept here to document the MERGE keys and help downstream BI tools.
--  * The rail_fob_bak_20260721 backup table is intentionally NOT migrated.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS BASIS_TRACKER;
USE SCHEMA BASIS_TRACKER;

-- ── Cash-bid snapshots (the core dataset) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS snapshots (
    id             NUMBER(38,0) IDENTITY(1,1),
    timestamp      STRING       NOT NULL,
    provider       STRING       NOT NULL,
    location       STRING       NOT NULL,
    source         STRING       NOT NULL DEFAULT 'manual',
    email_subject  STRING,
    email_date     STRING,
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (id),
    UNIQUE (timestamp, provider, location)   -- MERGE key
);

CREATE TABLE IF NOT EXISTS snapshot_rows (
    id             NUMBER(38,0) IDENTITY(1,1),
    snapshot_id    NUMBER(38,0) NOT NULL,
    row_id         STRING       NOT NULL,
    grain          STRING       NOT NULL,
    delivery_month STRING       NOT NULL,
    futures_symbol STRING       NOT NULL,
    basis_cents    NUMBER(38,0),
    is_spot        NUMBER(38,0) NOT NULL DEFAULT 0,
    spot_grain     STRING,
    PRIMARY KEY (id),
    UNIQUE (snapshot_id, row_id)             -- MERGE key
);

-- ── Rail FOB board ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rail_fob (
    date         STRING  NOT NULL,
    source       STRING  NOT NULL DEFAULT 'manual',
    market       STRING  NOT NULL,
    rail         STRING,
    commodity    STRING,
    period       STRING  NOT NULL,
    period_order NUMBER(38,0),
    futures      STRING,
    bid          NUMBER(38,0),
    offer        NUMBER(38,0),
    captured_at  STRING,
    bid_raw      STRING,
    offer_raw    STRING,
    PRIMARY KEY (date, source, market, period)   -- MERGE key
);

-- ── Futures curve / history ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS futures_prices (
    date        STRING NOT NULL,
    symbol      STRING NOT NULL,
    price_cents FLOAT  NOT NULL,
    captured_at STRING,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS futures_history (
    as_of     STRING NOT NULL,
    commodity STRING NOT NULL,
    month     STRING NOT NULL,
    value     FLOAT,
    PRIMARY KEY (as_of, commodity, month)
);

-- ── Location metadata (geocode, facility type, region, delivery zone) ───────
CREATE TABLE IF NOT EXISTS location_meta (
    provider      STRING NOT NULL,
    location      STRING NOT NULL,
    state         STRING,
    facility_type STRING,
    lat           FLOAT,
    lon           FLOAT,
    region        STRING,
    delivery_zone STRING,
    PRIMARY KEY (provider, location)
);

CREATE TABLE IF NOT EXISTS grain_map (
    raw_grain       STRING NOT NULL,
    canonical_grain STRING NOT NULL,
    wheat_class     STRING,
    protein         STRING,
    is_active       NUMBER(38,0) NOT NULL DEFAULT 1,
    PRIMARY KEY (raw_grain)
);

CREATE TABLE IF NOT EXISTS index_excludes (
    provider STRING NOT NULL,
    location STRING NOT NULL,
    PRIMARY KEY (provider, location)
);

-- ── Email import ledger ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS imported_emails (
    email_id    STRING NOT NULL,
    subject     STRING,
    imported_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (email_id)
);

-- ── Client reports config ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS client_reports (
    id          STRING NOT NULL,
    client_name STRING NOT NULL,
    email       STRING NOT NULL,
    cc          STRING,
    frequency   STRING NOT NULL,
    day_of_week NUMBER(38,0),
    locations   STRING NOT NULL,
    active      NUMBER(38,0) NOT NULL DEFAULT 1,
    created_at  STRING,
    depth       STRING DEFAULT 'curve',
    commodities STRING DEFAULT '[]',
    PRIMARY KEY (id)
);

-- ── Nightly rundown overrides ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nightly_override (
    date      STRING NOT NULL,
    item_name STRING NOT NULL,
    spot      NUMBER(38,0),
    nxt       NUMBER(38,0),
    spot_chg  NUMBER(38,0),
    nxt_chg   NUMBER(38,0),
    fut       STRING,
    PRIMARY KEY (date, item_name)
);

-- ── Spot/forward manual entry (CIF, freight, ethanol) ───────────────────────
CREATE TABLE IF NOT EXISTS spot_forward_manual (
    date              STRING NOT NULL,
    corn_cif_cents    NUMBER(38,0),
    bean_cif_cents    NUMBER(38,0),
    ilr_freight_cents NUMBER(38,0),
    chi_eth_cents     NUMBER(38,0),
    ny_eth_cents      NUMBER(38,0),
    captured_at       TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (date)
);

-- ── River-FOB workbook history (freight / CIF / calendar / spreads) ─────────
CREATE TABLE IF NOT EXISTS freight_history (
    as_of  STRING NOT NULL,
    region STRING NOT NULL,
    month  STRING NOT NULL,
    value  FLOAT,
    PRIMARY KEY (as_of, region, month)
);

CREATE TABLE IF NOT EXISTS cif_history (
    as_of     STRING NOT NULL,
    commodity STRING NOT NULL,
    month     STRING NOT NULL,
    value     FLOAT,
    PRIMARY KEY (as_of, commodity, month)
);

CREATE TABLE IF NOT EXISTS calendar_history (
    as_of     STRING NOT NULL,
    commodity STRING NOT NULL,
    seq       NUMBER(38,0) NOT NULL,
    month     STRING NOT NULL,
    contract  STRING,
    PRIMARY KEY (as_of, commodity, seq)
);

CREATE TABLE IF NOT EXISTS spreads_history (
    as_of     STRING NOT NULL,
    commodity STRING NOT NULL,
    seq       NUMBER(38,0) NOT NULL,
    label     STRING NOT NULL,
    value     FLOAT,
    PRIMARY KEY (as_of, commodity, seq)
);
