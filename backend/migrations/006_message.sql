-- =============================================================================
-- Vanguard Database Migration
-- Version: 006
-- Description: Create message table
-- =============================================================================

-- Up Migration
CREATE TABLE IF NOT EXISTS telegram_message (
    id INT PRIMARY KEY AUTO_INCREMENT,
    message_id INT NOT NULL,
    chat_id BIGINT NOT NULL,
    sender_id BIGINT NOT NULL,
    sender_name VARCHAR(255),
    message_type ENUM('group_text', 'group_photo', 'group_video', 'group_audio', 
                      'group_document', 'private_text', 'callback_query', 'command') NOT NULL,
    content TEXT,
    reply_to_message_id INT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_chat_timestamp (chat_id, timestamp),
    INDEX idx_sender (sender_id)
);

-- Down Migration
-- DROP TABLE IF EXISTS telegram_message;
