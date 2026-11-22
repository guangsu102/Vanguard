-- Run only against a disposable PostgreSQL database:
-- psql -v ON_ERROR_STOP=1 -f tests/integration/sql/account_delete_migration.sql
BEGIN;
CREATE SCHEMA account_delete_regression;
SET LOCAL search_path TO account_delete_regression;
CREATE TABLE telegram_account (id INTEGER PRIMARY KEY);
CREATE TABLE "group" (id INTEGER PRIMARY KEY);
CREATE TABLE group_account_membership (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE CASCADE
);
\ir ../../../migrations/060_add_group_qualification_audit.sql
INSERT INTO telegram_account VALUES (1), (2);
INSERT INTO "group" VALUES (1);
INSERT INTO group_account_membership VALUES (1, 1, 1), (2, 2, 1);
INSERT INTO group_qualification_audit
    (batch_id, membership_id, account_id, group_id, policy_version)
VALUES ('review', 1, 1, 1, 'v1'), ('review', 2, 2, 1, 'v1');

-- The historical schema must reproduce both failures.
DO $$ BEGIN
    BEGIN
        DELETE FROM telegram_account WHERE id=1;
        RAISE EXCEPTION 'Legacy account deletion unexpectedly succeeded';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    BEGIN
        DELETE FROM group_account_membership WHERE id=1;
        RAISE EXCEPTION 'Legacy membership deletion unexpectedly succeeded';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
END $$;
\ir ../../../migrations/075_fix_account_qualification_delete.sql

-- Reproduce ORM-generated constraint names as well as the SQL baseline names.
ALTER TABLE group_qualification_audit
    DROP CONSTRAINT group_qualification_audit_account_id_fkey,
    DROP CONSTRAINT group_qualification_audit_membership_id_fkey,
    ADD CONSTRAINT fk_group_qualification_audit_account_id_telegram_account
        FOREIGN KEY (account_id) REFERENCES telegram_account(id),
    ADD CONSTRAINT fk_group_qualification_audit_membership_id_group_account_membership
        FOREIGN KEY (membership_id) REFERENCES group_account_membership(id);
\ir ../../../migrations/075_fix_account_qualification_delete.sql
\ir ../../../migrations/075_fix_account_qualification_delete.sql
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM group_qualification_audit) = 2,
        'Migration changed existing history';
    ASSERT (SELECT count(*) FROM pg_constraint
        WHERE conrelid='group_qualification_audit'::regclass AND confdeltype='c') = 2,
        'Both foreign keys must cascade';
END $$;

-- Deleting one account cleans its own member/history and keeps the shared group.
DELETE FROM telegram_account WHERE id=1;
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT 1 FROM group_account_membership WHERE id=1);
    ASSERT NOT EXISTS (SELECT 1 FROM group_qualification_audit WHERE account_id=1);
    ASSERT EXISTS (SELECT 1 FROM telegram_account WHERE id=2);
    ASSERT EXISTS (SELECT 1 FROM group_account_membership WHERE id=2);
    ASSERT EXISTS (SELECT 1 FROM group_qualification_audit WHERE account_id=2);
    ASSERT EXISTS (SELECT 1 FROM "group" WHERE id=1);
END $$;

-- Removing a membership also cleans its history without deleting its account.
DELETE FROM group_account_membership WHERE id=2;
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT 1 FROM group_qualification_audit);
    ASSERT EXISTS (SELECT 1 FROM telegram_account WHERE id=2);
    ASSERT EXISTS (SELECT 1 FROM "group" WHERE id=1);
END $$;
ROLLBACK;
SELECT 'account deletion migration regression passed' AS result;
