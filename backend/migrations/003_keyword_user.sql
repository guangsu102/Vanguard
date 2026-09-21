-- =============================================================================
-- Vanguard Database Migration
-- Version: 003
-- Description: Create keyword and user tables
-- =============================================================================

-- Up Migration

-- Create keyword table
CREATE TABLE IF NOT EXISTS keyword (
    id INT PRIMARY KEY AUTO_INCREMENT,
    text VARCHAR(255) NOT NULL,
    type ENUM('demand', 'inquiry', 'price', 'competitor') NOT NULL,
    status ENUM('pending', 'approved', 'executing', 'completed', 'discarded') DEFAULT 'pending',
    match_mode ENUM('exact', 'fuzzy', 'regex') DEFAULT 'fuzzy',
    trigger_count INT DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type_status (type, status),
    INDEX idx_trigger_count (trigger_count)
);

-- Create user table
CREATE TABLE IF NOT EXISTS user (
    id INT PRIMARY KEY AUTO_INCREMENT,
    telegram_id BIGINT NOT NULL,
    xboard_user_id INT,
    username VARCHAR(100),
    state ENUM('new', 'pending', 'active', 'silent', 'churned', 'blocked') DEFAULT 'new',
    warning_count INT DEFAULT 0,
    muted_until DATETIME,
    trial_started_at DATETIME,
    trial_expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_telegram_id (telegram_id),
    INDEX idx_state (state)
);

-- Down Migration
-- DROP TABLE IF EXISTS keyword;
-- DROP TABLE IF EXISTS user;
