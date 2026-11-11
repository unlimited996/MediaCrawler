-- 表情包库（内容寻址规范对象）建表脚本
-- 部署时**直接在数据库执行**，不依赖容器启动时自动建表。
--
-- 群晖内网 PostgreSQL：
--   psql "postgresql://im:<密码>@192.168.10.38:4321/imdb" -f deploy/sql/emoji_asset.sql
-- 或在群晖 Container Manager 里用 phpPgAdmin / DBeaver 粘贴执行。
--
-- 幂等：重复执行不会覆盖已有数据。列定义与 database/models_v2.py::EmojiAsset 一致。

CREATE TABLE IF NOT EXISTS emoji_asset (
    id          BIGSERIAL PRIMARY KEY,
    checksum    VARCHAR(64)  NOT NULL UNIQUE,
    object_key  TEXT         NOT NULL,
    name        VARCHAR(128),
    source      VARCHAR(16)  NOT NULL DEFAULT 'manual',
    use_count   INTEGER      NOT NULL DEFAULT 0,
    size_bytes  BIGINT,
    mime_type   VARCHAR(64),
    origin_keys JSONB        NOT NULL DEFAULT '[]'::jsonb,
    story_ids   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    ext         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    schema_ver  INTEGER      NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_emoji_source ON emoji_asset (source);
CREATE INDEX IF NOT EXISTS idx_emoji_use    ON emoji_asset (use_count);
-- 去重 / 入库前校验都按 checksum 查未软删的资产
CREATE INDEX IF NOT EXISTS idx_emoji_checksum_alive
    ON emoji_asset (checksum) WHERE deleted_at IS NULL;
