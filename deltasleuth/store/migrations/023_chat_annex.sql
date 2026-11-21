-- Only an answer to a built-in question asked with no private conversation is shared in a report.
-- Older messages have no such provenance and remain in their creator's session.
ALTER TABLE ds.chat_messages ADD COLUMN report_prompt text
    CHECK (report_prompt IS NULL OR (role = 'assistant' AND report_prompt IN ('summary', 'root_cause')));
