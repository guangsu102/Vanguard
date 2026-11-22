-- Qualification evidence belongs to its account/member, like the membership itself.
-- Preserve historical migration checksums and handle both SQL- and ORM-named FKs.
-- No history is removed by this migration; cascades run only on an explicit delete.
SET LOCAL lock_timeout = '10s';
-- Wait at most 10s for the first relation so continuous readers cannot starve
-- the migration. Acquire the remaining locks without waiting, before any DDL,
-- to avoid deadlocks with workers that read these relations in another order.
LOCK TABLE telegram_account IN ACCESS EXCLUSIVE MODE;
LOCK TABLE group_account_membership, group_qualification_audit
    IN ACCESS EXCLUSIVE MODE NOWAIT;

DO $$
DECLARE
    fk RECORD;
BEGIN
    FOR fk IN
        SELECT c.conname, a.attname, c.confrelid::regclass AS parent_table
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        WHERE c.conrelid = 'group_qualification_audit'::regclass
          AND c.contype = 'f'
          AND cardinality(c.conkey) = 1
          AND a.attname IN ('account_id', 'membership_id')
          AND c.confdeltype <> 'c'
    LOOP
        EXECUTE format(
            'ALTER TABLE group_qualification_audit DROP CONSTRAINT %I, '
            'ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES %s(id) ON DELETE CASCADE',
            fk.conname, fk.conname, fk.attname, fk.parent_table
        );
    END LOOP;
END $$;
