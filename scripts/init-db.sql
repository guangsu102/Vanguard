-- Vanguard PostgreSQL bootstrap script.
-- Docker runs this only when the database volume is first created. Keep it
-- limited to database-level prerequisites; application tables are managed by
-- SQLAlchemy in development and by migrations in deployed environments.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
