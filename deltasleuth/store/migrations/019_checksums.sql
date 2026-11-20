-- Each applied migration's checksum: the SHA-256 of its statements, comments and layout aside. Every start
-- compares it with the build's file and refuses a changed statement. The process that applies this
-- migration fills it in for the versions applied before it, from the files of its own build.
ALTER TABLE ds.schema_version ADD COLUMN checksum text CHECK (checksum ~ '^[0-9a-f]{64}$');
