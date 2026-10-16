-- 0002_importer: which importer wrote each row.
--
-- `import --rebuild` removes rows the input no longer produces. It must remove only rows written by the same importer
-- under the same label: two importers may share a label on purpose (--source notes), and one must never delete the
-- other's rows. Rows imported before this migration have no importer (NULL) and are left alone by a rebuild until
-- they are imported again (`import --force`), which fills the column in.
ALTER TABLE messages ADD COLUMN importer text;
ALTER TABLE memories ADD COLUMN importer text;
CREATE INDEX messages_importer_source ON messages (importer, source);
