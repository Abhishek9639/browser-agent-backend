-- Runs once, on first start of an empty postgres volume.
-- Creates the three databases this project needs.
--
--   agent_core       the backend's own database (spec section 5)
--   agent_core_test  the integration test target; never the live database
--   payments         owned by browser-agent-payments (spec section 7), created
--                    here so one local Postgres serves both repos
--
-- POSTGRES_DB has already been created by the entrypoint, so each statement is
-- guarded: re-creating it would abort the whole script.

SELECT 'CREATE DATABASE agent_core'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'agent_core')\gexec

SELECT 'CREATE DATABASE agent_core_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'agent_core_test')\gexec

SELECT 'CREATE DATABASE payments'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'payments')\gexec
