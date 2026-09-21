-- =============================================================================
-- Vanguard Database Migration
-- Version: 001
-- Description: Initial database schema
-- =============================================================================

-- Up Migration
-- Create telegram_account table
CREATE TABLE IF NOT EXISTS telegram_account (
    id INT PRIMARY KEY AUTO_INCREMENT,
    phone VARCHAR(20) NOT NULL,
    api_id VARCHAR(50) NOT NULL,
    api_hash VARCHAR(100) NOT NULL,
    session_name VARCHAR(100) NOT NULL,
    proxy_id INT,
    fingerprint_id VARCHAR(50),
    status ENUM('offline', 'online', 'working', 'idle', 'error', 'banned') DEFAULT 'offline',
    last_active_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_phone (phone),
    INDEX idx_status (status)
);

-- Create proxy table
CREATE TABLE IF NOT EXISTS proxy (
    id INT PRIMARY KEY AUTO_INCREMENT,
    proxy_type ENUM('residential', 'datacenter', 'mobile') NOT NULL,
    host VARCHAR(100) NOT NULL,
    port INT NOT NULL,
    username VARCHAR(100),
    password VARCHAR(100),
    protocol VARCHAR(10) DEFAULT 'http',
    is_active TINYINT(1) DEFAULT 1,
    success_rate DECIMAL(5,2) DEFAULT 1.00,
    avg_latency INT DEFAULT 0,
    last_checked DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Add foreign key constraint
ALTER TABLE telegram_account 
    ADD CONSTRAINT fk_telegram_account_proxy 
    FOREIGN KEY (proxy_id) REFERENCES proxy(id) ON DELETE SET NULL;

-- Down Migration
-- ALTER TABLE telegram_account DROP FOREIGN KEY fk_telegram_account_proxy;
-- DROP TABLE IF EXISTS telegram_account;
-- DROP TABLE IF EXISTS proxy;
