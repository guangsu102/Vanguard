-- =============================================================================
-- Vanguard Database Migration
-- Version: 002
-- Description: Create group table
-- =============================================================================

-- Up Migration
CREATE TABLE IF NOT EXISTS `group` (
    id INT PRIMARY KEY AUTO_INCREMENT,
    group_id BIGINT NOT NULL,
    title VARCHAR(255),
    username VARCHAR(100),
    member_count INT DEFAULT 0,
    level ENUM('A', 'B', 'C', 'unrated') DEFAULT 'unrated',
    level_score DECIMAL(5,2) DEFAULT 0,
    rule_score INT DEFAULT 0,
    admin_score INT DEFAULT 0,
    history_score INT DEFAULT 0,
    convert_score INT DEFAULT 0,
    activity_score INT DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_group_id (group_id),
    INDEX idx_level (level)
);

-- Down Migration
-- DROP TABLE IF EXISTS `group`;
