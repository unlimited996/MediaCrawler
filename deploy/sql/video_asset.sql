-- 小红书视频贴转储记录（独立表，不影响图片备份链路）
-- 部署时直接在数据库执行，不依赖容器启动时自动建表。
--
--   psql "postgresql://im:<密码>@192.168.10.38:4321/imdb" -f deploy/sql/video_asset.sql
--
-- 幂等：重复执行不会覆盖已有数据。列定义与 database/models_v2.py::VideoAsset 一致。
-- 列可空 / 默认值与模型对齐，老库补列也走同一份 IF NOT EXISTS。

CREATE TABLE IF NOT EXISTS video_asset (
    id                  BIGSERIAL PRIMARY KEY,
    story_id            BIGINT,
    content_id          VARCHAR(255) NOT NULL,
    platform            VARCHAR(16)  NOT NULL DEFAULT 'xhs',
    source_url          TEXT         NOT NULL,
    backup_urls         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    object_key          TEXT,
    bucket              VARCHAR(128) NOT NULL DEFAULT 'videos',
    status              VARCHAR(16)  NOT NULL DEFAULT 'pending',
    mime_type           VARCHAR(64)  NOT NULL DEFAULT 'video/mp4',
    original_size_bytes BIGINT,
    stored_size_bytes   BIGINT,
    codec_v             VARCHAR(32),
    codec_a             VARCHAR(32),
    duration_sec        NUMERIC(10, 2),
    checksum            VARCHAR(64),
    retry_count         INTEGER      NOT NULL DEFAULT 0,
    max_retries         INTEGER      NOT NULL DEFAULT 3,
    last_error          TEXT,
    uploaded_at         TIMESTAMPTZ,
    ext                 JSONB        NOT NULL DEFAULT '{}'::jsonb,
    schema_ver          INTEGER      NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_video_asset_content
    ON video_asset (platform, content_id);
CREATE INDEX IF NOT EXISTS idx_video_asset_story  ON video_asset (story_id);
CREATE INDEX IF NOT EXISTS idx_video_asset_status ON video_asset (status);
