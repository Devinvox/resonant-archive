-- 0003_unit_key: which unit (file, session, conversation) of the input wrote each row.
--
-- `import --rebuild` may remove only rows that belong to a unit it actually re-read in that run and that the re-read no
-- longer produces. A unit it did not read (a project filter, a skipped symlink, an oversize file, a file since purged)
-- is not its business. That needs the link from a row to its unit. Rows imported before this migration have no unit
-- (NULL) and are never removed by a rebuild; `import --force` fills the column in.
ALTER TABLE messages ADD COLUMN unit_key text;
ALTER TABLE memories ADD COLUMN unit_key text;
CREATE INDEX messages_unit ON messages (importer, unit_key);
CREATE INDEX memories_unit ON memories (importer, unit_key);
