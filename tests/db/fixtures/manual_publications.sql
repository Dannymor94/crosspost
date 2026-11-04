-- Exact CREATE TABLE from ede92c0 db/engine.py::_migrate_publications_target.
-- Apply to the empty ede92c0.sql schema; retain inline REFERENCES syntax.
DROP TABLE publications;
CREATE TABLE publications_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
            publication_id VARCHAR(64) NOT NULL,
            channel VARCHAR(64) NOT NULL,
            target VARCHAR(64) NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL,
            external_id VARCHAR(255),
            error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT uq_publication UNIQUE (profile_id, publication_id, channel, target)
        );
ALTER TABLE publications_new RENAME TO publications;
CREATE INDEX ix_publications_publication_id ON publications (publication_id);
CREATE INDEX ix_publications_profile_id ON publications (profile_id);
