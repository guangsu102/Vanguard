-- =============================================================================
-- Vanguard Database Migration
-- Version: 008
-- Date: 2026-05-22
-- Description: Create guardian module tables (verification, coupon distribution)
-- =============================================================================

USE vanguard;

-- -----------------------------------------------------------------------------
-- Table: verification_session
-- Description: Verification sessions for group join
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS verification_session (
    id INT PRIMARY KEY AUTO_INCREMENT,
    session_id VARCHAR(64) NOT NULL UNIQUE COMMENT '会话ID',
    user_id BIGINT NOT NULL COMMENT '用户ID',
    chat_id BIGINT NOT NULL COMMENT '群组ID',
    verify_type ENUM('captcha', 'question') NOT NULL COMMENT '验证类型',
    state ENUM('pending', 'passed', 'failed', 'expired') DEFAULT 'pending' COMMENT '状态',
    question TEXT COMMENT '问题',
    answer VARCHAR(255) COMMENT '答案',
    captcha_code VARCHAR(10) COMMENT '验证码',
    attempt_count INT DEFAULT 0 COMMENT '尝试次数',
    max_attempts INT DEFAULT 3 COMMENT '最大尝试次数',
    expires_at DATETIME NOT NULL COMMENT '过期时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    INDEX idx_verify_user_chat (user_id, chat_id),
    INDEX idx_verify_session (session_id),
    INDEX idx_verify_expires (expires_at),
    INDEX idx_verify_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='入群验证会话表';

-- -----------------------------------------------------------------------------
-- Table: group_verification_config
-- Description: Group verification configuration
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS group_verification_config (
    id INT PRIMARY KEY AUTO_INCREMENT,
    group_id BIGINT NOT NULL UNIQUE COMMENT '群组ID',
    enable_verification TINYINT(1) DEFAULT 0 COMMENT '是否启用验证',
    verification_type ENUM('captcha', 'question') DEFAULT 'captcha' COMMENT '验证类型',
    questions TEXT COMMENT '问答配置JSON',
    welcome_message TEXT COMMENT '欢迎消息模板',
    timeout_minutes INT DEFAULT 5 COMMENT '超时时间(分钟)',
    whitelist_bypass TINYINT(1) DEFAULT 1 COMMENT '白名单跳过',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_group_id (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='群组验证配置表';

-- -----------------------------------------------------------------------------
-- Table: coupon_distribution
-- Description: Coupon and reward distribution records
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS coupon_distribution (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL COMMENT '用户ID',
    campaign_id INT COMMENT '活动ID',
    distribution_type VARCHAR(20) NOT NULL COMMENT '类型: trial/discount/gift',
    coupon_code VARCHAR(100) COMMENT '优惠码',
    trial_hours INT COMMENT '试用时长(小时)',
    traffic_gb INT COMMENT '流量(GB)',
    distributed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_coupon_user (user_id),
    INDEX idx_coupon_campaign (campaign_id),
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='优惠券分发记录表';

-- -----------------------------------------------------------------------------
-- Add columns to group table for verification settings
-- -----------------------------------------------------------------------------
ALTER TABLE `group` 
    ADD COLUMN IF NOT EXISTS enable_verification TINYINT(1) DEFAULT 0 COMMENT '是否启用入群验证' AFTER level_score,
    ADD COLUMN IF NOT EXISTS verification_type ENUM('captcha', 'question') DEFAULT 'captcha' COMMENT '验证类型' AFTER enable_verification;

-- -----------------------------------------------------------------------------
-- Add indexes to existing violation table for better performance
-- -----------------------------------------------------------------------------
ALTER TABLE violation
    ADD INDEX IF NOT EXISTS idx_violation_rule_type (rule_type);

-- Down Migration
-- ALTER TABLE `group` DROP COLUMN IF EXISTS enable_verification;
-- ALTER TABLE `group` DROP COLUMN IF EXISTS verification_type;
-- DROP TABLE IF EXISTS coupon_distribution;
-- DROP TABLE IF EXISTS group_verification_config;
-- DROP TABLE IF EXISTS verification_session;
