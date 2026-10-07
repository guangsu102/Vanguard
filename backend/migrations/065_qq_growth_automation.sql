-- QQ automation: preserve existing connections and groups.

ALTER TABLE qq_bot_connection ADD COLUMN http_url VARCHAR(500), ADD COLUMN websocket_url VARCHAR(500), ADD COLUMN access_token_encrypted TEXT, ADD COLUMN join_api_url VARCHAR(500), ADD COLUMN join_api_token_encrypted TEXT, ADD COLUMN automation_enabled BOOLEAN NOT NULL DEFAULT FALSE, ADD COLUMN join_interval_seconds INTEGER NOT NULL DEFAULT 600, ADD COLUMN send_interval_seconds INTEGER NOT NULL DEFAULT 60, ADD COLUMN max_joins_per_day INTEGER NOT NULL DEFAULT 10, ADD COLUMN max_sends_per_day INTEGER NOT NULL DEFAULT 30, ADD COLUMN next_join_at TIMESTAMP, ADD COLUMN next_send_at TIMESTAMP;

ALTER TABLE qq_managed_group ADD COLUMN membership_status VARCHAR(20) NOT NULL DEFAULT 'unknown', ADD COLUMN membership_verified_at TIMESTAMP;

CREATE TABLE qq_ad_campaign (
	id SERIAL NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	auto_join_enabled BOOLEAN NOT NULL, 
	send_mode VARCHAR(20) NOT NULL, 
	min_wait_after_join_minutes INTEGER NOT NULL, 
	interval_minutes INTEGER NOT NULL, 
	scheduled_times_json TEXT NOT NULL, 
	timezone VARCHAR(60) NOT NULL, 
	max_sends_per_group_per_day INTEGER NOT NULL, 
	max_sends_per_account_per_day INTEGER NOT NULL, 
	start_at TIMESTAMP WITHOUT TIME ZONE, 
	end_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_qq_ad_campaign PRIMARY KEY (id), 
	CONSTRAINT uq_qq_ad_campaign_name UNIQUE (name)
);

CREATE TABLE qq_campaign_target (
	id SERIAL NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	group_number VARCHAR(20) NOT NULL, 
	local_name VARCHAR(255), 
	verify_message VARCHAR(500) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	CONSTRAINT pk_qq_campaign_target PRIMARY KEY (id), 
	CONSTRAINT uq_qq_campaign_target UNIQUE (campaign_id, group_number), 
	CONSTRAINT fk_qq_campaign_target_campaign_id_qq_ad_campaign FOREIGN KEY(campaign_id) REFERENCES qq_ad_campaign (id) ON DELETE CASCADE
);

CREATE TABLE qq_ad_binding (
	id SERIAL NOT NULL, 
	connection_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	creative_id INTEGER NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	priority INTEGER NOT NULL, 
	CONSTRAINT pk_qq_ad_binding PRIMARY KEY (id), 
	CONSTRAINT uq_qq_ad_binding UNIQUE (connection_id, campaign_id, creative_id), 
	CONSTRAINT fk_qq_ad_binding_connection_id_qq_bot_connection FOREIGN KEY(connection_id) REFERENCES qq_bot_connection (id) ON DELETE CASCADE, 
	CONSTRAINT fk_qq_ad_binding_campaign_id_qq_ad_campaign FOREIGN KEY(campaign_id) REFERENCES qq_ad_campaign (id) ON DELETE CASCADE, 
	CONSTRAINT fk_qq_ad_binding_creative_id_ad_creative FOREIGN KEY(creative_id) REFERENCES ad_creative (id) ON DELETE CASCADE
);

CREATE TABLE qq_join_task (
	id SERIAL NOT NULL, 
	connection_id INTEGER NOT NULL, 
	group_number VARCHAR(20) NOT NULL, 
	verify_message VARCHAR(500) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	attempt_count INTEGER NOT NULL, 
	error_message TEXT, 
	attempted_at TIMESTAMP WITHOUT TIME ZONE, 
	joined_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_qq_join_task PRIMARY KEY (id), 
	CONSTRAINT uq_qq_join_account_group UNIQUE (connection_id, group_number), 
	CONSTRAINT fk_qq_join_task_connection_id_qq_bot_connection FOREIGN KEY(connection_id) REFERENCES qq_bot_connection (id) ON DELETE CASCADE
);

CREATE INDEX idx_qq_join_task_status ON qq_join_task (status, connection_id);

CREATE TABLE qq_ad_schedule (
	id SERIAL NOT NULL, 
	connection_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	group_number VARCHAR(20) NOT NULL, 
	next_due_at TIMESTAMP WITHOUT TIME ZONE, 
	last_sent_at TIMESTAMP WITHOUT TIME ZONE, 
	status VARCHAR(30) NOT NULL, 
	error_message TEXT, 
	CONSTRAINT pk_qq_ad_schedule PRIMARY KEY (id), 
	CONSTRAINT uq_qq_ad_schedule UNIQUE (connection_id, campaign_id, group_number), 
	CONSTRAINT fk_qq_ad_schedule_connection_id_qq_bot_connection FOREIGN KEY(connection_id) REFERENCES qq_bot_connection (id) ON DELETE CASCADE, 
	CONSTRAINT fk_qq_ad_schedule_campaign_id_qq_ad_campaign FOREIGN KEY(campaign_id) REFERENCES qq_ad_campaign (id) ON DELETE CASCADE
);

CREATE INDEX idx_qq_ad_schedule_due ON qq_ad_schedule (status, next_due_at);

CREATE TABLE qq_automation_log (
	id VARCHAR(32) NOT NULL, 
	connection_id INTEGER NOT NULL, 
	campaign_id INTEGER, 
	creative_id INTEGER, 
	group_number VARCHAR(20) NOT NULL, 
	operation_type VARCHAR(20) NOT NULL, 
	operation_key VARCHAR(255) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	payload_json TEXT NOT NULL, 
	provider_message_id VARCHAR(255), 
	error_message TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	CONSTRAINT pk_qq_automation_log PRIMARY KEY (id), 
	CONSTRAINT fk_qq_automation_log_connection_id_qq_bot_connection FOREIGN KEY(connection_id) REFERENCES qq_bot_connection (id) ON DELETE CASCADE, 
	CONSTRAINT fk_qq_automation_log_campaign_id_qq_ad_campaign FOREIGN KEY(campaign_id) REFERENCES qq_ad_campaign (id) ON DELETE SET NULL, 
	CONSTRAINT fk_qq_automation_log_creative_id_ad_creative FOREIGN KEY(creative_id) REFERENCES ad_creative (id) ON DELETE SET NULL, 
	CONSTRAINT uq_qq_automation_log_operation_key UNIQUE (operation_key)
);

CREATE INDEX idx_qq_automation_log_account_time ON qq_automation_log (connection_id, created_at);

CREATE INDEX idx_qq_automation_log_group_time ON qq_automation_log (group_number, created_at);
