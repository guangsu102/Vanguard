ALTER TABLE "group"
    ADD COLUMN IF NOT EXISTS ad_delivery_account_id INTEGER NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_group_ad_delivery_account'
    ) THEN
        ALTER TABLE "group"
            ADD CONSTRAINT fk_group_ad_delivery_account
            FOREIGN KEY (ad_delivery_account_id)
            REFERENCES telegram_account(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_group_ad_delivery_account
    ON "group" (ad_delivery_account_id);

COMMENT ON COLUMN "group".ad_delivery_account_id
    IS 'Dedicated advertisement delivery account after manual handover';
