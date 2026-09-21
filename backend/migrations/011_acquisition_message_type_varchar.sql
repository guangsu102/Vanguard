-- Keep acquisition business message categories independent from telegram_message.message_type.
-- The PostgreSQL enum named "messagetype" belongs to the core message router
-- (GROUP_TEXT, PRIVATE_TEXT, ...), while acquisition templates use values like
-- interaction/share/guide/qa.
ALTER TABLE acquisition_message
    ALTER COLUMN message_type TYPE VARCHAR(50)
    USING message_type::text;

ALTER TABLE acquisition_message_template
    ALTER COLUMN message_type TYPE VARCHAR(50)
    USING message_type::text;
