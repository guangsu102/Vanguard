-- =============================================================================
-- Vanguard Database Migration
-- Version: 005
-- Description: Create moderation tables
-- =============================================================================

-- Up Migration

-- Create violation table
CREATE TABLE IF NOT EXISTS violation (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL,
    group_id BIGINT NOT NULL,
    rule_type VARCHAR(50) NOT NULL,
    rule_pattern VARCHAR(255),
    content TEXT,
    action_taken ENUM('warn', 'mute', 'ban', 'kick') NOT NULL,
    action_duration INT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_group (user_id, group_id),
    INDEX idx_created_at (created_at),
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
);

-- Create moderation_rule table
CREATE TABLE IF NOT EXISTS moderation_rule (
    id INT PRIMARY KEY AUTO_INCREMENT,
    rule_type ENUM('keyword', 'domain', 'frequency', 'image') NOT NULL,
    pattern VARCHAR(255) NOT NULL,
    level ENUM('low', 'medium', 'high') DEFAULT 'medium',
    action ENUM('warn', 'mute', 'ban') DEFAULT 'warn',
    group_id BIGINT,
    enabled TINYINT(1) DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type_enabled (rule_type, enabled)
);

-- Create whitelist table
CREATE TABLE IF NOT EXISTS whitelist (
    id INT PRIMARY KEY AUTO_INCREMENT,
    whitelist_type VARCHAR(20) NOT NULL,
    value VARCHAR(255) NOT NULL,
    group_id BIGINT,
    expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_type_value (whitelist_type, value)
);

-- Down Migration
-- ALTER TABLE violation DROP FOREIGN KEY violation_ibfk_1;
-- DROP TABLE IF EXISTS violation;
-- DROP TABLE IF EXISTS moderation_rule;
-- DROP TABLE IF EXISTS whitelist;
