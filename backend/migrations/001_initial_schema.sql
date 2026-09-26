-- =============================================================================
-- Vanguard Database Migration Script
-- Version: 1.0.0
-- Date: 2026-05-20
-- Description: Initial database schema for XBoard Telegram Bot Matrix
-- =============================================================================

-- Create database if not exists
CREATE DATABASE IF NOT EXISTS vanguard CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE vanguard;

-- -----------------------------------------------------------------------------
-- Table: telegram_account
-- Description: Telegram user accounts
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telegram_account (
    id INT PRIMARY KEY AUTO_INCREMENT,
    phone VARCHAR(20) NOT NULL COMMENT '手机号',
    api_id VARCHAR(50) NOT NULL COMMENT 'API ID',
    api_hash VARCHAR(100) NOT NULL COMMENT 'API Hash',
    session_name VARCHAR(100) NOT NULL COMMENT '会话名称',
    proxy_id INT COMMENT '代理ID',
    fingerprint_id VARCHAR(50) COMMENT '指纹ID',
    status ENUM('offline', 'online', 'working', 'idle', 'error', 'banned') DEFAULT 'offline' COMMENT '账号状态',
    last_active_at DATETIME COMMENT '最后活跃时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_phone (phone),
    UNIQUE KEY uk_session_name (session_name),
    INDEX idx_status (status),
    INDEX idx_proxy (proxy_id),
    FOREIGN KEY (proxy_id) REFERENCES proxy(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Telegram账号表';

-- -----------------------------------------------------------------------------
-- Table: proxy
-- Description: Proxy configurations
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proxy (
    id INT PRIMARY KEY AUTO_INCREMENT,
    proxy_type ENUM('residential', 'datacenter', 'mobile') NOT NULL COMMENT '代理类型',
    host VARCHAR(100) NOT NULL COMMENT '主机地址',
    port INT NOT NULL COMMENT '端口',
    username VARCHAR(100) COMMENT '用户名',
    password VARCHAR(100) COMMENT '密码',
    protocol VARCHAR(10) DEFAULT 'http' COMMENT '协议: http, socks5',
    is_active TINYINT(1) DEFAULT 1 COMMENT '是否激活',
    success_rate DECIMAL(5,2) DEFAULT 1.00 COMMENT '成功率',
    avg_latency INT DEFAULT 0 COMMENT '平均延迟(ms)',
    last_checked DATETIME COMMENT '最后检查时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type_active (proxy_type, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='代理配置表';

-- Add foreign key after proxy table exists
ALTER TABLE telegram_account
    ADD CONSTRAINT fk_proxy
    FOREIGN KEY (proxy_id) REFERENCES proxy(id) ON DELETE SET NULL;

-- -----------------------------------------------------------------------------
-- Table: group
-- Description: Telegram groups
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `group` (
    id INT PRIMARY KEY AUTO_INCREMENT,
    group_id BIGINT NOT NULL COMMENT 'Telegram群组ID',
    title VARCHAR(255) COMMENT '群名称',
    username VARCHAR(100) COMMENT '群用户名',
    member_count INT DEFAULT 0 COMMENT '成员数',
    level ENUM('A', 'B', 'C', 'unrated') DEFAULT 'unrated' COMMENT '等级',
    level_score DECIMAL(5,2) DEFAULT 0 COMMENT '评分',
    rule_score INT DEFAULT 0 COMMENT '群规管控分(0-100)',
    admin_score INT DEFAULT 0 COMMENT '管理员态度分(0-100)',
    history_score INT DEFAULT 0 COMMENT '历史表现分(0-100)',
    convert_score INT DEFAULT 0 COMMENT '转化效果分(0-100)',
    activity_score INT DEFAULT 0 COMMENT '活跃度分(0-100)',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_group_id (group_id),
    INDEX idx_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='群组表';

-- -----------------------------------------------------------------------------
-- Table: keyword
-- Description: Keywords for message matching
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS keyword (
    id INT PRIMARY KEY AUTO_INCREMENT,
    text VARCHAR(255) NOT NULL COMMENT '关键词',
    type ENUM('demand', 'inquiry', 'price', 'competitor') NOT NULL COMMENT '关键词类型',
    status ENUM('pending', 'approved', 'executing', 'completed', 'discarded') DEFAULT 'pending' COMMENT '状态',
    match_mode ENUM('exact', 'fuzzy', 'regex') DEFAULT 'fuzzy' COMMENT '匹配模式',
    trigger_count INT DEFAULT 0 COMMENT '触发次数',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type_status (type, status),
    INDEX idx_trigger_count (trigger_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='关键词表';

-- -----------------------------------------------------------------------------
-- Table: user
-- Description: User tracking and lifecycle
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user (
    id INT PRIMARY KEY AUTO_INCREMENT,
    telegram_id BIGINT NOT NULL COMMENT 'Telegram用户ID',
    xboard_user_id INT COMMENT 'XBoard用户ID',
    username VARCHAR(100) COMMENT '用户名',
    state ENUM('new', 'pending', 'active', 'silent', 'churned', 'blocked') DEFAULT 'new' COMMENT '用户状态',
    warning_count INT DEFAULT 0 COMMENT '警告次数',
    muted_until DATETIME COMMENT '禁言截止时间',
    trial_started_at DATETIME COMMENT '试用开始时间',
    trial_expires_at DATETIME COMMENT '试用过期时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_telegram_id (telegram_id),
    INDEX idx_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- -----------------------------------------------------------------------------
-- Table: campaign
-- Description: Marketing campaigns
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL COMMENT '活动名称',
    campaign_type ENUM('trial', 'promo', 'discount', 'gift_card') NOT NULL COMMENT '活动类型',
    trigger_timing VARCHAR(50) DEFAULT 'after_register' COMMENT '触发时机',
    validity_hours INT DEFAULT 168 COMMENT '有效期(小时)',
    trial_plan_id INT COMMENT '试用套餐ID',
    trial_hours INT DEFAULT 24 COMMENT '试用时长(小时)',
    trial_traffic_gb INT DEFAULT 50 COMMENT '试用流量(GB)',
    gift_card_template_id INT COMMENT '礼品卡模板ID',
    enabled TINYINT(1) DEFAULT 0 COMMENT '是否启用',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='活动表';

-- -----------------------------------------------------------------------------
-- Table: campaign_tracking
-- Description: Campaign conversion tracking
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_tracking (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL COMMENT '用户ID',
    campaign_name VARCHAR(100) COMMENT '活动名称',
    source VARCHAR(50) COMMENT '来源: tg_group, tg_private, search',
    group_id BIGINT COMMENT '群组ID',
    keyword VARCHAR(100) COMMENT '触发关键词',
    bot_id VARCHAR(50) COMMENT 'Bot账号ID',
    registered_at DATETIME COMMENT '注册时间',
    converted_at DATETIME COMMENT '转化时间',
    trial_granted TINYINT(1) DEFAULT 0 COMMENT '是否发放试用',
    coupon_granted TINYINT(1) DEFAULT 0 COMMENT '是否发放优惠卷',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id),
    INDEX idx_source (source),
    INDEX idx_campaign (campaign_name),
    INDEX idx_registered_at (registered_at),
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='活动追踪表';

-- -----------------------------------------------------------------------------
-- Table: violation
-- Description: Violation records
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS violation (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL COMMENT '用户ID',
    group_id BIGINT NOT NULL COMMENT '群组ID',
    rule_type VARCHAR(50) NOT NULL COMMENT '违规类型',
    rule_pattern VARCHAR(255) COMMENT '匹配规则',
    content TEXT COMMENT '违规内容',
    action_taken ENUM('warn', 'mute', 'ban', 'kick') NOT NULL COMMENT '处理动作',
    action_duration INT COMMENT '惩罚时长(秒)',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_group (user_id, group_id),
    INDEX idx_created_at (created_at),
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='违规记录表';

-- -----------------------------------------------------------------------------
-- Table: moderation_rule
-- Description: Moderation rules
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS moderation_rule (
    id INT PRIMARY KEY AUTO_INCREMENT,
    rule_type ENUM('keyword', 'domain', 'frequency', 'image') NOT NULL COMMENT '规则类型',
    pattern VARCHAR(255) NOT NULL COMMENT '规则模式',
    level ENUM('low', 'medium', 'high') DEFAULT 'medium' COMMENT '违规等级',
    action ENUM('warn', 'mute', 'ban') DEFAULT 'warn' COMMENT '处理动作',
    group_id BIGINT COMMENT 'NULL表示全局规则',
    enabled TINYINT(1) DEFAULT 1 COMMENT '是否启用',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type_enabled (rule_type, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审核规则表';

-- -----------------------------------------------------------------------------
-- Table: whitelist
-- Description: Whitelist for users and domains
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whitelist (
    id INT PRIMARY KEY AUTO_INCREMENT,
    whitelist_type VARCHAR(20) NOT NULL COMMENT '类型: user, domain, path',
    value VARCHAR(255) NOT NULL COMMENT '值',
    group_id BIGINT COMMENT '群组ID',
    expires_at DATETIME COMMENT '过期时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_type_value (whitelist_type, value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='白名单表';

-- -----------------------------------------------------------------------------
-- Table: telegram_message
-- Description: Message records
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telegram_message (
    id INT PRIMARY KEY AUTO_INCREMENT,
    message_id INT NOT NULL COMMENT '消息ID',
    chat_id BIGINT NOT NULL COMMENT '群组ID',
    sender_id BIGINT NOT NULL COMMENT '发送者ID',
    sender_name VARCHAR(255) COMMENT '发送者名称',
    message_type ENUM('group_text', 'group_photo', 'group_video', 'group_audio',
                      'group_document', 'private_text', 'callback_query', 'command') NOT NULL COMMENT '消息类型',
    content TEXT COMMENT '消息内容',
    reply_to_message_id INT COMMENT '回复的消息ID',
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_chat_timestamp (chat_id, timestamp),
    INDEX idx_sender (sender_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='消息记录表';

-- =============================================================================
-- Default Data
-- =============================================================================

-- Insert default moderation rules
INSERT INTO moderation_rule (rule_type, pattern, level, action, enabled) VALUES
-- Competitor keywords (high severity)
('keyword', 'XX机场', 'high', 'ban', 1),
('keyword', 'XXVPN', 'high', 'ban', 1),
('keyword', 'XX加速器', 'high', 'ban', 1),
-- Generic competitor terms (medium severity)
('keyword', '机场|节点|梯子|VPN', 'medium', 'warn', 1),
('keyword', '翻墙|科学上网', 'medium', 'warn', 1),
('keyword', '节点出售|卖节点', 'medium', 'warn', 1),
-- Suspicious domains (low severity)
('domain', '\\.vip$', 'low', 'warn', 1),
('domain', '\\.xyz$', 'low', 'warn', 1),
('domain', '\\.top$', 'low', 'warn', 1),
('domain', 'bit\\.ly', 'low', 'warn', 1),
('domain', 'goo\\.gl', 'low', 'warn', 1);

-- Insert default whitelist domains
INSERT INTO whitelist (whitelist_type, value) VALUES
('domain', 'github.com'),
('domain', 'youtube.com'),
('domain', 'twitter.com'),
('domain', 'xboard');

-- Insert default campaign
INSERT INTO campaign (name, campaign_type, trigger_timing, validity_hours, trial_hours, trial_traffic_gb, enabled) VALUES
('默认试用活动', 'trial', 'after_register', 168, 24, 50, 1),
('新用户优惠', 'discount', 'after_register', 72, 0, 0, 1);
