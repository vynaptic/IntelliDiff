-- The blob drain reads the queue's due addresses oldest first: by an index, not a scan and sort of the
-- whole queue, which a purged cycle of a million keys fills with two million addresses.
CREATE INDEX blob_gc_queued_idx ON ds.blob_gc (queued_at);
