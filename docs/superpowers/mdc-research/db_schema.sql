-- actress_log
CREATE TABLE "actress_log" ( "id" integer NOT NULL UNIQUE PRIMARY KEY AUTOINCREMENT, "actress_task_id" integer NOT NULL, "stage" integer NOT NULL DEFAULT 0, "level" varchar NOT NULL, "message" varchar NOT NULL, FOREIGN KEY ("actress_task_id") REFERENCES "actress_task" ("id") ON DELETE CASCADE ON UPDATE CASCADE );

-- actress_task
CREATE TABLE "actress_task" ( "id" integer NOT NULL UNIQUE PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL, "overwrite_mode" integer, "status" integer NOT NULL DEFAULT 0, "stage" integer NOT NULL DEFAULT 0, "created_at" varchar NOT NULL DEFAULT CURRENT_TIMESTAMP, "started_at" varchar, "end_at" varchar, "error_message" varchar, "emby_id" varchar, "overview" varchar, "year" integer, "has_pic" boolean NOT NULL DEFAULT FALSE, "has_backdrop" boolean NOT NULL DEFAULT FALSE , "pre_fetched_data" json_text);

-- log
CREATE TABLE "log" ( "id" integer NOT NULL UNIQUE PRIMARY KEY AUTOINCREMENT, "task_id" integer NOT NULL, "stage" integer NOT NULL DEFAULT 0, "level" varchar NOT NULL, "message" varchar NOT NULL, "session_id" integer, FOREIGN KEY ("task_id") REFERENCES "task" ("id") ON DELETE CASCADE ON UPDATE CASCADE );

-- manual_job
CREATE TABLE "manual_job" ( "id" integer NOT NULL UNIQUE PRIMARY KEY AUTOINCREMENT, "source_pathes" varchar NOT NULL DEFAULT 0, "target_dir" varchar NOT NULL, "link_mode" integer NOT NULL, "delete_empty_parent_after_move" boolean NOT NULL, "total_count" integer NOT NULL DEFAULT 0, "finish_count" integer NOT NULL DEFAULT 0, "skip_count" integer NOT NULL DEFAULT 0, "error_count" integer NOT NULL DEFAULT 0, "status" integer NOT NULL DEFAULT 0, "stage" integer NOT NULL DEFAULT 0, "created_at" varchar NOT NULL DEFAULT CURRENT_TIMESTAMP, "started_at" varchar, "error_message" varchar , "end_at" varchar, "config_override" json_text, "skipped_tasks" json_text, "abort_count" integer NOT NULL DEFAULT 0);

-- seaql_migrations
CREATE TABLE "seaql_migrations" ( "version" varchar NOT NULL PRIMARY KEY, "applied_at" bigint NOT NULL );

-- sqlite_sequence
CREATE TABLE sqlite_sequence(name,seq);

-- task
CREATE TABLE "task" ( "id" integer NOT NULL UNIQUE PRIMARY KEY AUTOINCREMENT, "manual_job_id" integer, "trigger" integer NOT NULL, "source_path" varchar NOT NULL, "target_dir" varchar, "target_path" varchar, "link_mode" integer NOT NULL, "delete_empty_parent_after_move" boolean, "status" integer NOT NULL DEFAULT 0, "stage" integer NOT NULL DEFAULT 0, "created_at" varchar NOT NULL DEFAULT CURRENT_TIMESTAMP, "started_at" varchar, "error_message" varchar, "number" varchar, "actors" varchar, "parse_result" json_text, "image_result" json_text, "metadata" json_text, "metadata_provider_info" json_text, "metadata_all_sites" json_text, "end_at" varchar, "override_url" varchar, "title" text GENERATED ALWAYS AS (json_extract(metadata, '$.Title')) VIRTUAL, "original_title" text GENERATED ALWAYS AS (json_extract(metadata, '$.OriginalTitle')) VIRTUAL, "release" text GENERATED ALWAYS AS (json_extract(metadata, '$.Release')) VIRTUAL, "poster_provider" text GENERATED ALWAYS AS (coalesce(json_extract(metadata_provider_info, '$.Poster'), json_extract(metadata_provider_info, '$.Cover'))) VIRTUAL, "primary_task_id" integer, "part" varchar, "video_prefix" varchar, "reorganize_use_global_config" boolean NOT NULL DEFAULT FALSE, FOREIGN KEY ("manual_job_id") REFERENCES "manual_job" ("id") ON DELETE CASCADE ON UPDATE CASCADE );

-- task_log_session
CREATE TABLE "task_log_session" ( "id" integer NOT NULL UNIQUE PRIMARY KEY AUTOINCREMENT, "task_id" integer NOT NULL, "session_type" varchar NOT NULL, "created_at" varchar NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY ("task_id") REFERENCES "task" ("id") ON DELETE CASCADE ON UPDATE CASCADE );

-- idx_actress_task_id
CREATE INDEX "idx_actress_task_id" ON "actress_log" ("actress_task_id");

-- idx_actress_task_status
CREATE INDEX "idx_actress_task_status" ON "actress_task" ("status");

-- idx_log_session_id
CREATE INDEX "idx_log_session_id" ON "log" ("session_id");

-- idx_manual_job_id
CREATE INDEX "idx_manual_job_id" ON "task" ("manual_job_id");

-- idx_primary_task_id
CREATE INDEX "idx_primary_task_id" ON "task" ("primary_task_id");

-- idx_source_path
CREATE INDEX "idx_source_path" ON "task" ("source_path");

-- idx_status
CREATE INDEX "idx_status" ON "task" ("status");

-- idx_task_id
CREATE INDEX "idx_task_id" ON "log" ("task_id");

-- idx_task_log_session_task_id
CREATE INDEX "idx_task_log_session_task_id" ON "task_log_session" ("task_id");

-- idx_trigger
CREATE INDEX "idx_trigger" ON "task" ("trigger");


===== Actress.db =====
-- Info
CREATE TABLE [Info]
(
	[Name]         NVarChar(255)     NULL,
	[Roma]         NVarChar(255)     NULL,
	[Href]         NVarChar(255)     NULL,
	[Birthday]     DateTime2         NULL,
	[Age]          INTEGER           NULL,
	[Height]       INTEGER           NULL,
	[Bust]         INTEGER           NULL,
	[Waist]        INTEGER           NULL,
	[Hip]          INTEGER           NULL,
	[Cup]          NVarChar(255)     NULL,
	[Birthplace]   NVarChar(255)     NULL,
	[CareerPeriod] NVarChar(255)     NULL,
	[DebutWork]    NVarChar(255)     NULL,
	[Account]      NVarChar(255)     NULL,
	[OfficialSite] NVarChar(255)     NULL,
	[UpdateTime]   DateTime2     NOT NULL,
	[Completeness] INTEGER       NOT NULL,

	CONSTRAINT [PK_Info] PRIMARY KEY ([Href])
);

-- Names
CREATE TABLE [Names]
(
	[Alias] NVarChar(255)     NULL,
	[Name]  NVarChar(255)     NULL,
	[Roma]  NVarChar(255)     NULL,

	CONSTRAINT [PK_Names] PRIMARY KEY ([Alias])
);

-- Urls
CREATE TABLE [Urls]
(
	[Name]       NVarChar(255)     NULL,
	[Href]       NVarChar(255)     NULL,
	[Youma]      INTEGER       NOT NULL,
	[Wuma]       INTEGER       NOT NULL,
	[UpdateTime] DateTime2     NOT NULL,
	[Update]     Bit           NOT NULL,

	CONSTRAINT [PK_Urls] PRIMARY KEY ([Href])
);

