-- =============================================================================
-- Admin User Table Migration
-- Description: Create admin user table for authentication
-- =============================================================================

CREATE TABLE IF NOT EXISTS admin_user (
    id INT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(50) NOT NULL UNIQUE COMMENT '用户名',
    password VARCHAR(255) NOT NULL COMMENT '密码(bcrypt hash)',
    role ENUM('admin', 'operator', 'viewer') DEFAULT 'operator' COMMENT '角色',
    email VARCHAR(100) COMMENT '邮箱',
    avatar VARCHAR(255) COMMENT '头像URL',
    is_active TINYINT(1) DEFAULT 1 COMMENT '是否激活',
    last_login_at DATETIME COMMENT '最后登录时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_username (username),
    INDEX idx_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='管理员用户表';

-- Insert default admin user
-- Username: admin
-- Password: admin123 (bcrypt hash)
INSERT INTO admin_user (username, password, role, email) VALUES
('admin', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYqVqN4XQXK', 'admin', 'admin@vanguard.local');
