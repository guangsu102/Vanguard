-- Stage 2 audit events use resource_type='message_execution' (17 characters)
-- and reference a BIGSERIAL message execution id. The orchestration table
-- originally allowed only VARCHAR(16) and INTEGER. Widen only the legacy
-- types so replay never narrows a future wider column.
DO $$
DECLARE
    current_length INTEGER;
    resource_id_udt TEXT;
BEGIN
    SELECT character_maximum_length
    INTO current_length
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'owned_group_audit_events'
      AND column_name = 'resource_type';

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'owned_group_audit_events.resource_type does not exist; apply migration 044 first';
    END IF;

    IF current_length IS NOT NULL AND current_length < 32 THEN
        ALTER TABLE owned_group_audit_events
            ALTER COLUMN resource_type TYPE VARCHAR(32);
    END IF;

    SELECT udt_name
    INTO resource_id_udt
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'owned_group_audit_events'
      AND column_name = 'resource_id';

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'owned_group_audit_events.resource_id does not exist; apply migration 044 first';
    END IF;

    IF resource_id_udt = 'int4' THEN
        ALTER TABLE owned_group_audit_events
            ALTER COLUMN resource_id TYPE BIGINT USING resource_id::bigint;
    ELSIF resource_id_udt <> 'int8' THEN
        RAISE EXCEPTION
            'owned_group_audit_events.resource_id has unexpected type %', resource_id_udt;
    END IF;
END
$$;
