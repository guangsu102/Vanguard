-- =============================================================================
-- Vanguard Database Migration
-- Version: 004
-- Description: Create campaign tables
-- =============================================================================

-- Up Migration

-- Create campaign table
CREATE TABLE IF NOT EXISTS campaign (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    campaign_type ENUM('trial', 'promo', 'discount', 'gift_card') NOT NULL,
    trigger_timing VARCHAR(50) DEFAULT 'after_register',
    validity_hours INT DEFAULT 168,
    trial_plan_id INT,
    trial_hours INT DEFAULT 24,
    trial_traffic_gb INT DEFAULT 50,
    gift_card_template_id INT,
    enabled TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_name (name)
);

-- Create campaign_tracking table
CREATE TABLE IF NOT EXISTS campaign_tracking (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL,
    campaign_name VARCHAR(100),
    source VARCHAR(50),
    group_id BIGINT,
    keyword VARCHAR(100),
    bot_id VARCHAR(50),
    registered_at DATETIME,
    converted_at DATETIME,
    trial_granted TINYINT(1) DEFAULT 0,
    coupon_granted TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id),
    INDEX idx_source (source),
    INDEX idx_campaign (campaign_name),
    INDEX idx_registered_at (registered_at),
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
);

-- Down Migration
-- ALTER TABLE campaign_tracking DROP FOREIGN KEY campaign_tracking_ibfk_1;
-- DROP TABLE IF EXISTS campaign_tracking;
-- DROP TABLE IF EXISTS campaign;
