-- Licensed to the Apache Software Foundation (ASF) under one or more
-- contributor license agreements.  See the NOTICE file distributed with
-- this work for additional information regarding copyright ownership.
-- The ASF licenses this file to You under the Apache License, Version 2.0
-- (the "License"); you may not use this file except in compliance with
-- the License.  You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.
-- Apply with a migration owner. Runtime roles must not own this schema.
CREATE SCHEMA IF NOT EXISTS gateway_config;
CREATE TABLE IF NOT EXISTS gateway_config.revisions (
    revision bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gateway_id text NOT NULL CHECK (gateway_id ~ '^[a-zA-Z0-9_.-]+$'),
    config jsonb NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    checksum text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (gateway_id, revision)
);
CREATE TABLE IF NOT EXISTS gateway_config.publications (
    gateway_id text PRIMARY KEY,
    generation bigint NOT NULL DEFAULT 0,
    revision bigint,
    published_by text,
    published_at timestamptz,
    FOREIGN KEY (gateway_id, revision)
        REFERENCES gateway_config.revisions(gateway_id, revision)
);
CREATE TABLE IF NOT EXISTS gateway_config.audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gateway_id text NOT NULL,
    generation bigint NOT NULL,
    revision bigint NOT NULL,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS gateway_config.node_status (
    gateway_id text NOT NULL,
    node_id text NOT NULL,
    status jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (gateway_id, node_id)
);
CREATE OR REPLACE FUNCTION gateway_config.reject_revision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'configuration revisions are immutable; create a new revision';
END;
$$;
DROP TRIGGER IF EXISTS immutable_revision ON gateway_config.revisions;
CREATE TRIGGER immutable_revision BEFORE UPDATE OR DELETE
ON gateway_config.revisions FOR EACH ROW
EXECUTE FUNCTION gateway_config.reject_revision_mutation();
