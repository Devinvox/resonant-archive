-- resonant-archive schema, migration 0001.
-- Requires the `vector` and `pg_trgm` extensions (db.ensure_extensions creates them before migrations run).
-- docs/SCHEMA.md is the contract other code is written against: change both together.

-- One row per input unit already imported (a file, a session, a conversation), so an unchanged input is skipped.
CREATE TABLE sources (
  importer     text        NOT NULL,           -- importer name, e.g. claude_code
  unit_key     text        NOT NULL,           -- stable key of the unit within that importer
  label        text        NOT NULL,           -- the messages.source label its rows were written under
  content_hash text        NOT NULL,           -- sha256 over the unit's file contents + importer version + options
  n_files      integer     NOT NULL DEFAULT 0,
  n_messages   integer     NOT NULL DEFAULT 0, -- rows kept from this unit
  n_memories   integer     NOT NULL DEFAULT 0,
  path         text,                           -- where it was read from (informational)
  imported_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (importer, unit_key)
);

CREATE TABLE messages (
  id            text        PRIMARY KEY,       -- deterministic, set by the importer: '<prefix>:<stable parts>'
  source        text        NOT NULL,          -- free label: chatgpt, claude-ai, claude-code, notes, ...
  conv_id       text        NOT NULL,          -- conversation id, unique within `source`
  conv_title    text,
  ts            timestamptz NOT NULL,
  speaker       text        NOT NULL CHECK (speaker IN ('user','agent','assistant','peer','system','tool')),
  context       text        NOT NULL DEFAULT 'default',  -- free tag; default, system, peer, notes, ...
  text          text        NOT NULL,
  platform      text,
  model         text,
  hidden        boolean     NOT NULL DEFAULT false,      -- tool calls/results, harness input: stored, left out of default search
  tags          text[]      NOT NULL DEFAULT '{}',
  src           text,                                    -- origin within the source (file name, relative path)
  seq           integer     NOT NULL,                    -- 0-based position within the conversation
  tsv           tsvector    GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
  embedding     vector(768),                             -- nomic-embed-text-v1.5; NULL until embedded
  embed_status  text        NOT NULL DEFAULT 'pending'
                CHECK (embed_status IN ('pending','done','skipped','failed')),
  embedded_hash text,                                    -- sha1 of the text that was embedded (written by the embedder)
  embedded_at   timestamptz,
  imported_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX messages_tsv_gin      ON messages USING gin (tsv);
CREATE INDEX messages_text_trgm    ON messages USING gin (text gin_trgm_ops);
CREATE INDEX messages_embedding    ON messages USING hnsw (embedding vector_cosine_ops);
CREATE INDEX messages_source_ts    ON messages (source, ts);
CREATE INDEX messages_conv_seq     ON messages (conv_id, seq);
CREATE INDEX messages_ts           ON messages (ts);
CREATE INDEX messages_speaker      ON messages (speaker);
CREATE INDEX messages_embed_pending ON messages (id) WHERE embed_status = 'pending';

-- One row per conversation, kept up to date by the importers.
CREATE TABLE conversations (
  source     text        NOT NULL,
  conv_id    text        NOT NULL,
  title      text,
  platform   text,
  first_ts   timestamptz NOT NULL,
  last_ts    timestamptz NOT NULL,
  n_messages integer     NOT NULL,
  n_visible  integer     NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, conv_id)
);
CREATE INDEX conversations_last_ts ON conversations (last_ts);

-- Exact-recall strings (paths, urls, ids, quoted phrases) pulled from visible messages.
CREATE TABLE anchors (
  token  text        NOT NULL,
  kind   text        NOT NULL,
  msg_id text        NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
  ts     timestamptz NOT NULL,
  PRIMARY KEY (msg_id, token, kind)
);
CREATE INDEX anchors_token_kind ON anchors (token, kind);
CREATE INDEX anchors_kind       ON anchors (kind);

-- Chronicle L1 memories (written by the chronicle mod, loaded by the chronicle_mirror importer).
CREATE TABLE memories (
  id          text        PRIMARY KEY,         -- 'memory:<sid>:<ordinal>:<source_hash>'
  source      text        NOT NULL,            -- same label as the conversation's messages
  sid         text        NOT NULL,            -- session id = messages.conv_id of the session
  level       integer     NOT NULL DEFAULT 1,
  ordinal     integer,
  ts          timestamptz NOT NULL,            -- when the memory was written
  t_from      timestamptz,                     -- first and last row the memory covers
  t_to        timestamptz,
  from_uuid   text,
  to_uuid     text,
  text        text        NOT NULL,
  tsv         tsvector    GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
  est_tokens  integer,
  rows        integer,
  source_hash text,
  imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memories_tsv_gin ON memories USING gin (tsv);
CREATE INDEX memories_sid     ON memories (sid, ordinal);
CREATE INDEX memories_ts      ON memories (ts);
