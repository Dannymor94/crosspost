-- Frozen schema from ede92c0; do not regenerate from current models.

CREATE TABLE checkpoints (
	profile_id INTEGER NOT NULL,
	publication_id VARCHAR(64) NOT NULL,
	channel VARCHAR(64) NOT NULL,
	target VARCHAR(64) NOT NULL,
	external_id VARCHAR(255),
	PRIMARY KEY (profile_id, publication_id, channel, target)
)

;

CREATE TABLE profiles (
	id INTEGER NOT NULL,
	name VARCHAR(255) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
)

;

CREATE TABLE channel_targets (
	id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL,
	channel VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	value_blob TEXT NOT NULL,
	topic_id INTEGER,
	topic_title VARCHAR(255),
	peer_access_hash BIGINT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE CASCADE
)

;
CREATE INDEX ix_channel_target ON channel_targets (profile_id, channel);
CREATE INDEX ix_channel_targets_profile_id ON channel_targets (profile_id);

CREATE TABLE connections (
	id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL,
	channel VARCHAR(64) NOT NULL,
	state VARCHAR(32) NOT NULL,
	updated_at DATETIME NOT NULL,
	expires_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_connection UNIQUE (profile_id, channel),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE CASCADE
)

;
CREATE INDEX ix_connections_profile_id ON connections (profile_id);

CREATE TABLE credentials (
	id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL,
	channel VARCHAR(64) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	blob TEXT NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_credential UNIQUE (profile_id, channel, kind),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE CASCADE
)

;
CREATE INDEX ix_credentials_profile_id ON credentials (profile_id);

CREATE TABLE logs (
	id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL,
	channel VARCHAR(64),
	publication_id VARCHAR(64),
	level VARCHAR(16) NOT NULL,
	message TEXT NOT NULL,
	screenshot_path TEXT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE CASCADE
)

;
CREATE INDEX ix_logs_profile_id ON logs (profile_id);

CREATE TABLE profile_settings (
	id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL,
	"key" VARCHAR(64) NOT NULL,
	value JSON NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_profile_setting UNIQUE (profile_id, "key"),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE CASCADE
)

;
CREATE INDEX ix_profile_settings_profile_id ON profile_settings (profile_id);

CREATE TABLE publications (
	id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL,
	publication_id VARCHAR(64) NOT NULL,
	channel VARCHAR(64) NOT NULL,
	target VARCHAR(64) NOT NULL,
	status VARCHAR(32) NOT NULL,
	external_id VARCHAR(255),
	error TEXT,
	attempt_count INTEGER NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_publication UNIQUE (profile_id, publication_id, channel, target),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE CASCADE
)

;
CREATE INDEX ix_publications_profile_id ON publications (profile_id);
CREATE INDEX ix_publications_publication_id ON publications (publication_id);

CREATE TABLE scheduled_posts (
	id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL,
	content_type VARCHAR(32) NOT NULL,
	text TEXT NOT NULL,
	article_text TEXT,
	link_phrase VARCHAR(128),
	title VARCHAR(255),
	media_paths JSON NOT NULL,
	channels JSON NOT NULL,
	scheduled_at DATETIME NOT NULL,
	status VARCHAR(32) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	published_at DATETIME,
	reject_reason TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE CASCADE
)

;
CREATE INDEX ix_scheduled_posts_profile_id ON scheduled_posts (profile_id);
CREATE INDEX ix_scheduled_report ON scheduled_posts (profile_id, status, published_at);

CREATE TABLE users (
	id INTEGER NOT NULL,
	login VARCHAR(64) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	role VARCHAR(16) NOT NULL,
	profile_id INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(profile_id) REFERENCES profiles (id) ON DELETE SET NULL
)

;
CREATE UNIQUE INDEX ix_users_login ON users (login);
CREATE INDEX ix_users_profile_id ON users (profile_id);
