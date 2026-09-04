-- Applied transactionally by migrate(), under SET ROLE hal_store_owner.
-- Remove the owner's global default: a per-schema REVOKE cannot undo it.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE honest_alpha.schema_version (
    version integer PRIMARY KEY, checksum text NOT NULL, installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE honest_alpha.runs (
    run_id text PRIMARY KEY CHECK (length(run_id) BETWEEN 1 AND 200),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    policy jsonb NOT NULL CHECK (jsonb_typeof(policy) = 'object'),
    budget jsonb NOT NULL CHECK (jsonb_typeof(budget) = 'object'),
    snapshot_hash text NOT NULL, policy_hash text NOT NULL,
    max_trials integer NOT NULL CHECK (max_trials > 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    created_by name NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE honest_alpha.run_state (
    run_id text PRIMARY KEY REFERENCES honest_alpha.runs,
    reserved_trials integer NOT NULL DEFAULT 0 CHECK (reserved_trials >= 0),
    last_sequence bigint NOT NULL DEFAULT 0,
    last_hash text NOT NULL DEFAULT 'GENESIS'
);
CREATE TABLE honest_alpha.candidates (
    run_id text NOT NULL REFERENCES honest_alpha.runs,
    candidate_hash text NOT NULL,
    specification jsonb NOT NULL CHECK (jsonb_typeof(specification) = 'object'),
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    created_by name NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, candidate_hash)
);
CREATE TABLE honest_alpha.trials (
    trial_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id text NOT NULL, candidate_hash text NOT NULL,
    reservation_index integer NOT NULL,
    created_by name NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id, candidate_hash) REFERENCES honest_alpha.candidates,
    UNIQUE (run_id, candidate_hash), UNIQUE (run_id, reservation_index)
);
CREATE TABLE honest_alpha.records (
    run_id text NOT NULL REFERENCES honest_alpha.runs,
    sequence bigint NOT NULL CHECK (sequence > 0),
    previous_hash text NOT NULL, envelope jsonb NOT NULL, entry_hash text NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE honest_alpha.graph_events (
    run_id text NOT NULL REFERENCES honest_alpha.runs,
    event_key text NOT NULL CHECK (length(event_key) BETWEEN 1 AND 200),
    event jsonb NOT NULL CHECK (jsonb_typeof(event) = 'object'),
    created_by name NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, event_key)
);
CREATE TABLE honest_alpha.queue (
    trial_id uuid PRIMARY KEY REFERENCES honest_alpha.trials,
    run_id text NOT NULL REFERENCES honest_alpha.runs,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'completed', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner name, lease_token uuid, lease_until timestamptz,
    result jsonb,
    CHECK (state <> 'running' OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX queue_pending ON honest_alpha.queue (available_at, trial_id) WHERE state = 'pending';
CREATE INDEX queue_expired ON honest_alpha.queue (lease_until, trial_id) WHERE state = 'running';
CREATE INDEX queue_by_run ON honest_alpha.queue (run_id);
CREATE VIEW honest_alpha.queue_status AS
    SELECT trial_id, run_id, state, attempts, max_attempts, available_at, lease_owner, lease_until
    FROM honest_alpha.queue;
CREATE TABLE honest_alpha_sealed.records (
    trial_id uuid NOT NULL REFERENCES honest_alpha.trials,
    record_key text NOT NULL CHECK (length(record_key) BETWEEN 1 AND 200),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_hash text NOT NULL, created_by name NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (trial_id, record_key)
);

CREATE FUNCTION honest_alpha._immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'immutable research record' USING ERRCODE = '55000';
END $$;
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['schema_version', 'runs', 'candidates', 'trials', 'records', 'graph_events'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON honest_alpha.%I FOR EACH STATEMENT EXECUTE FUNCTION honest_alpha._immutable()', t);
    END LOOP;
END $$;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON honest_alpha_sealed.records
    FOR EACH STATEMENT EXECUTE FUNCTION honest_alpha._immutable();

-- Every public mutation checks the original authenticated login, even after SET ROLE.
CREATE FUNCTION honest_alpha._authorize(required_role text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE role_count integer;
BEGIN
    SELECT count(*) INTO role_count FROM unnest(ARRAY['hal_proposer','hal_worker','hal_validator']) r
        WHERE pg_has_role(session_user, r, 'MEMBER');
    IF role_count <> 1 OR NOT pg_has_role(session_user, required_role, 'USAGE')
       OR pg_has_role(session_user, 'hal_store_owner', 'MEMBER')
       OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = session_user
                  AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'unauthorized database session' USING ERRCODE = '42501';
    END IF;
END $$;

-- JSONB already normalizes object ordering; trim numeric scale recursively as well.
CREATE FUNCTION honest_alpha._normalize(value jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE result jsonb;
BEGIN
    CASE jsonb_typeof(value)
    WHEN 'number' THEN RETURN to_jsonb(trim_scale(value::numeric));
    WHEN 'object' THEN
        SELECT coalesce(jsonb_object_agg(key, honest_alpha._normalize(v)), '{}'::jsonb)
            INTO result FROM jsonb_each(value) e(key, v);
    WHEN 'array' THEN
        SELECT coalesce(jsonb_agg(honest_alpha._normalize(v) ORDER BY n), '[]'::jsonb)
            INTO result FROM jsonb_array_elements(value) WITH ORDINALITY e(v, n);
    ELSE RETURN value;
    END CASE;
    RETURN result;
END $$;
CREATE FUNCTION honest_alpha._hash(value jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
    SELECT encode(sha256(convert_to(value::text, 'UTF8')), 'hex')
$$;
CREATE FUNCTION honest_alpha._append(p_run text, p_kind text, p_payload jsonb) RETURNS bigint
LANGUAGE plpgsql SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE s honest_alpha.run_state; e jsonb; h text;
BEGIN
    SELECT * INTO STRICT s FROM honest_alpha.run_state WHERE run_id = p_run FOR UPDATE;
    e := jsonb_build_object('kind', p_kind, 'payload', p_payload, 'actor', session_user,
                           'recorded_at', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
    h := honest_alpha._hash(jsonb_build_array(p_run, s.last_sequence + 1, s.last_hash, e));
    INSERT INTO honest_alpha.records VALUES (p_run, s.last_sequence + 1, s.last_hash, e, h);
    UPDATE honest_alpha.run_state SET last_sequence = s.last_sequence + 1, last_hash = h WHERE run_id = p_run;
    RETURN s.last_sequence + 1;
END $$;

CREATE FUNCTION honest_alpha.create_run(p_run text, p_snapshot jsonb, p_policy jsonb, p_budget jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE r honest_alpha.runs; trials numeric; attempts numeric;
BEGIN
    PERFORM honest_alpha._authorize('hal_validator');
    IF p_snapshot IS NULL OR p_policy IS NULL OR p_budget IS NULL
       OR jsonb_typeof(p_snapshot) <> 'object' OR jsonb_typeof(p_policy) <> 'object'
       OR jsonb_typeof(p_budget) <> 'object' OR jsonb_typeof(p_budget->'max_trials') IS DISTINCT FROM 'number'
       OR (p_budget ? 'max_attempts' AND jsonb_typeof(p_budget->'max_attempts') IS DISTINCT FROM 'number') THEN
        RAISE EXCEPTION 'snapshot, policy and budget objects with numeric max_trials required' USING ERRCODE = '22023';
    END IF;
    trials := (p_budget->>'max_trials')::numeric;
    attempts := coalesce((p_budget->>'max_attempts')::numeric, 3);
    IF trials <> trunc(trials) OR trials < 1 OR trials > 2147483647
       OR attempts <> trunc(attempts) OR attempts NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'invalid trial or attempt budget' USING ERRCODE = '22023';
    END IF;
    INSERT INTO honest_alpha.runs(run_id, snapshot, policy, budget, snapshot_hash, policy_hash, max_trials, max_attempts, created_by)
        VALUES (p_run, p_snapshot, p_policy, p_budget,
                honest_alpha._hash(honest_alpha._normalize(p_snapshot)), honest_alpha._hash(honest_alpha._normalize(p_policy)),
                trials::integer, attempts::integer, session_user)
        ON CONFLICT (run_id) DO NOTHING;
    IF NOT FOUND THEN
        SELECT * INTO STRICT r FROM honest_alpha.runs WHERE run_id = p_run;
        IF r.snapshot <> p_snapshot OR r.policy <> p_policy OR r.budget <> p_budget THEN
            RAISE EXCEPTION 'run already exists with different immutable inputs' USING ERRCODE = '23505';
        END IF;
        RETURN to_jsonb(r);
    END IF;
    INSERT INTO honest_alpha.run_state(run_id) VALUES (p_run);
    SELECT * INTO STRICT r FROM honest_alpha.runs WHERE run_id = p_run;
    PERFORM honest_alpha._append(p_run, 'run_created', to_jsonb(r));
    RETURN to_jsonb(r);
END $$;

CREATE FUNCTION honest_alpha.reserve_trial(p_run text, p_specification jsonb, p_metadata jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE s honest_alpha.run_state; r honest_alpha.runs; t honest_alpha.trials;
        spec jsonb; h text; c honest_alpha.candidates;
BEGIN
    PERFORM honest_alpha._authorize('hal_proposer');
    IF p_specification IS NULL OR jsonb_typeof(p_specification) <> 'object'
       OR p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object' THEN
        RAISE EXCEPTION 'candidate specification and metadata must be objects' USING ERRCODE = '22023';
    END IF;
    spec := honest_alpha._normalize(p_specification);
    h := honest_alpha._hash(spec);
    SELECT * INTO STRICT s FROM honest_alpha.run_state WHERE run_id = p_run FOR UPDATE;
    SELECT * INTO t FROM honest_alpha.trials WHERE run_id = p_run AND candidate_hash = h;
    IF FOUND THEN
        IF (SELECT specification FROM honest_alpha.candidates WHERE run_id = p_run AND candidate_hash = h) <> spec THEN
            RAISE EXCEPTION 'candidate hash collision' USING ERRCODE = '23505';
        END IF;
        RETURN to_jsonb(t) || jsonb_build_object('created', false);
    END IF;
    SELECT * INTO STRICT r FROM honest_alpha.runs WHERE run_id = p_run;
    IF s.reserved_trials >= r.max_trials THEN
        RAISE EXCEPTION 'trial budget exhausted' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO honest_alpha.candidates VALUES (p_run, h, spec, p_metadata, session_user, clock_timestamp()) RETURNING * INTO c;
    UPDATE honest_alpha.run_state SET reserved_trials = reserved_trials + 1 WHERE run_id = p_run;
    INSERT INTO honest_alpha.trials(run_id, candidate_hash, reservation_index, created_by)
        VALUES (p_run, h, s.reserved_trials + 1, session_user) RETURNING * INTO t;
    INSERT INTO honest_alpha.queue(trial_id, run_id, max_attempts) VALUES (t.trial_id, p_run, r.max_attempts);
    PERFORM honest_alpha._append(p_run, 'candidate_registered', to_jsonb(c));
    PERFORM honest_alpha._append(p_run, 'trial_reserved', to_jsonb(t));
    RETURN to_jsonb(t) || jsonb_build_object('created', true);
END $$;

CREATE FUNCTION honest_alpha.claim(p_run text, p_lease_seconds integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE q honest_alpha.queue;
BEGIN
    PERFORM honest_alpha._authorize('hal_worker');
    IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 1 AND 86400 THEN
        RAISE EXCEPTION 'lease seconds must be between 1 and 86400' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO q FROM honest_alpha.queue
        WHERE state = 'pending' AND available_at <= clock_timestamp() AND attempts < max_attempts
          AND (p_run IS NULL OR run_id = p_run)
        ORDER BY available_at, trial_id LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN RETURN NULL; END IF;
    -- Allocate the deadline only after any wait for the per-run audit lock.
    PERFORM 1 FROM honest_alpha.run_state WHERE run_id = q.run_id FOR UPDATE;
    UPDATE honest_alpha.queue SET state = 'running', attempts = attempts + 1,
        lease_owner = session_user, lease_token = gen_random_uuid(),
        lease_until = clock_timestamp() + make_interval(secs => p_lease_seconds)
        WHERE trial_id = q.trial_id RETURNING * INTO q;
    PERFORM honest_alpha._append(q.run_id, 'trial_claimed', jsonb_build_object('trial_id', q.trial_id, 'attempt', q.attempts));
    RETURN to_jsonb(q) || jsonb_build_object('specification',
        (SELECT c.specification FROM honest_alpha.trials t JOIN honest_alpha.candidates c USING (run_id, candidate_hash)
         WHERE t.trial_id = q.trial_id));
END $$;

CREATE FUNCTION honest_alpha.heartbeat(p_trial uuid, p_token uuid, p_lease_seconds integer)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE q honest_alpha.queue; expires timestamptz;
BEGIN
    PERFORM honest_alpha._authorize('hal_worker');
    IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 1 AND 86400 THEN
        RAISE EXCEPTION 'lease seconds must be between 1 and 86400' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO STRICT q FROM honest_alpha.queue WHERE trial_id = p_trial FOR UPDATE;
    IF q.state <> 'running' OR q.lease_owner IS DISTINCT FROM session_user
       OR q.lease_token IS DISTINCT FROM p_token OR q.lease_until <= clock_timestamp() THEN
        RAISE EXCEPTION 'stale or foreign lease' USING ERRCODE = '55000';
    END IF;
    expires := greatest(q.lease_until, clock_timestamp() + make_interval(secs => p_lease_seconds));
    UPDATE honest_alpha.queue SET lease_until = expires WHERE trial_id = p_trial;
    RETURN expires;
END $$;

CREATE FUNCTION honest_alpha.reclaim(p_run text, p_limit integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE q honest_alpha.queue; n integer := 0; next_state text;
BEGIN
    PERFORM honest_alpha._authorize('hal_worker');
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'reclaim limit must be between 1 and 1000' USING ERRCODE = '22023';
    END IF;
    FOR q IN SELECT * FROM honest_alpha.queue
        WHERE state = 'running' AND lease_until <= clock_timestamp() AND (p_run IS NULL OR run_id = p_run)
        -- Acquire multiple run heads in a consistent order within a batch.
        ORDER BY run_id, lease_until, trial_id LIMIT p_limit FOR UPDATE SKIP LOCKED
    LOOP
        next_state := CASE WHEN q.attempts >= q.max_attempts THEN 'failed' ELSE 'pending' END;
        UPDATE honest_alpha.queue SET state = next_state, available_at = clock_timestamp(),
            lease_owner = NULL, lease_token = NULL, lease_until = NULL WHERE trial_id = q.trial_id;
        PERFORM honest_alpha._append(q.run_id, 'trial_reclaimed', jsonb_build_object(
            'trial_id', q.trial_id, 'attempt', q.attempts, 'state', next_state));
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;

CREATE FUNCTION honest_alpha.complete(p_trial uuid, p_token uuid, p_result jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE q honest_alpha.queue;
BEGIN
    PERFORM honest_alpha._authorize('hal_worker');
    IF p_result IS NULL OR jsonb_typeof(p_result) <> 'object' THEN
        RAISE EXCEPTION 'result must be an object' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO STRICT q FROM honest_alpha.queue WHERE trial_id = p_trial FOR UPDATE;
    IF q.lease_owner IS DISTINCT FROM session_user OR q.lease_token IS DISTINCT FROM p_token THEN
        RAISE EXCEPTION 'stale or foreign lease' USING ERRCODE = '55000';
    END IF;
    IF q.state = 'completed' THEN
        IF q.result <> p_result THEN
            RAISE EXCEPTION 'completion conflicts with immutable result' USING ERRCODE = '23505';
        END IF;
        RETURN q.result;
    END IF;
    IF q.state <> 'running' OR q.lease_until <= clock_timestamp() THEN
        RAISE EXCEPTION 'stale or foreign lease' USING ERRCODE = '55000';
    END IF;
    UPDATE honest_alpha.queue SET state = 'completed', result = p_result, lease_until = NULL WHERE trial_id = p_trial;
    PERFORM honest_alpha._append(q.run_id, 'trial_completed', jsonb_build_object(
        'trial_id', p_trial, 'attempt', q.attempts, 'result', p_result));
    RETURN p_result;
END $$;

CREATE FUNCTION honest_alpha.fail(p_trial uuid, p_token uuid, p_error jsonb, p_retry_seconds integer)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE q honest_alpha.queue; next_state text;
BEGIN
    PERFORM honest_alpha._authorize('hal_worker');
    IF p_error IS NULL OR jsonb_typeof(p_error) <> 'object'
       OR p_retry_seconds IS NULL OR p_retry_seconds NOT BETWEEN 0 AND 86400 THEN
        RAISE EXCEPTION 'error object and retry delay between 0 and 86400 required' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO STRICT q FROM honest_alpha.queue WHERE trial_id = p_trial FOR UPDATE;
    IF q.state <> 'running' OR q.lease_owner IS DISTINCT FROM session_user
       OR q.lease_token IS DISTINCT FROM p_token OR q.lease_until <= clock_timestamp() THEN
        RAISE EXCEPTION 'stale or foreign lease' USING ERRCODE = '55000';
    END IF;
    next_state := CASE WHEN q.attempts >= q.max_attempts THEN 'failed' ELSE 'pending' END;
    UPDATE honest_alpha.queue SET state = next_state,
        available_at = clock_timestamp() + make_interval(secs => p_retry_seconds),
        lease_owner = NULL, lease_token = NULL, lease_until = NULL WHERE trial_id = p_trial;
    PERFORM honest_alpha._append(q.run_id, 'trial_failed', jsonb_build_object(
        'trial_id', p_trial, 'attempt', q.attempts, 'state', next_state, 'error', p_error));
    RETURN next_state;
END $$;

CREATE FUNCTION honest_alpha.append_graph_event(p_run text, p_key text, p_event jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE old honest_alpha.graph_events;
BEGIN
    -- These are proposal/lineage notes, not validation or promotion decisions.
    PERFORM honest_alpha._authorize('hal_proposer');
    IF p_event IS NULL OR jsonb_typeof(p_event) <> 'object' THEN
        RAISE EXCEPTION 'graph event must be an object' USING ERRCODE = '22023';
    END IF;
    PERFORM 1 FROM honest_alpha.run_state WHERE run_id = p_run FOR UPDATE;
    INSERT INTO honest_alpha.graph_events VALUES (p_run, p_key, p_event, session_user, clock_timestamp())
        ON CONFLICT (run_id, event_key) DO NOTHING;
    IF NOT FOUND THEN
        SELECT * INTO STRICT old FROM honest_alpha.graph_events WHERE run_id = p_run AND event_key = p_key;
        IF old.event <> p_event OR old.created_by <> session_user THEN
            RAISE EXCEPTION 'graph event key conflict' USING ERRCODE = '23505';
        END IF;
        RETURN old.event;
    END IF;
    PERFORM honest_alpha._append(p_run, 'graph_event', jsonb_build_object('event_key', p_key, 'event', p_event));
    RETURN p_event;
END $$;

CREATE FUNCTION honest_alpha.seal_result(p_trial uuid, p_key text, p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, honest_alpha, pg_temp AS $$
DECLARE q honest_alpha.queue; old honest_alpha_sealed.records;
BEGIN
    PERFORM honest_alpha._authorize('hal_validator');
    IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'sealed payload must be an object' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO STRICT q FROM honest_alpha.queue WHERE trial_id = p_trial FOR UPDATE;
    IF q.state <> 'completed' THEN
        RAISE EXCEPTION 'only completed trials may receive sealed results' USING ERRCODE = '55000';
    END IF;
    INSERT INTO honest_alpha_sealed.records(trial_id, record_key, payload, payload_hash, created_by)
        VALUES (p_trial, p_key, p_payload, honest_alpha._hash(honest_alpha._normalize(p_payload)), session_user)
        ON CONFLICT (trial_id, record_key) DO NOTHING;
    SELECT * INTO STRICT old FROM honest_alpha_sealed.records WHERE trial_id = p_trial AND record_key = p_key;
    IF old.payload <> p_payload OR old.created_by <> session_user THEN
        RAISE EXCEPTION 'sealed record key conflict' USING ERRCODE = '23505';
    END IF;
    RETURN to_jsonb(old);
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA honest_alpha, honest_alpha_sealed FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA honest_alpha FROM PUBLIC;
GRANT USAGE ON SCHEMA honest_alpha TO hal_proposer, hal_worker, hal_validator;
GRANT SELECT ON honest_alpha.runs, honest_alpha.run_state, honest_alpha.candidates, honest_alpha.trials,
    honest_alpha.records, honest_alpha.graph_events, honest_alpha.queue_status TO hal_proposer, hal_worker, hal_validator;
GRANT EXECUTE ON FUNCTION honest_alpha.create_run(text,jsonb,jsonb,jsonb), honest_alpha.seal_result(uuid,text,jsonb) TO hal_validator;
GRANT EXECUTE ON FUNCTION honest_alpha.reserve_trial(text,jsonb,jsonb), honest_alpha.append_graph_event(text,text,jsonb) TO hal_proposer;
GRANT EXECUTE ON FUNCTION honest_alpha.claim(text,integer), honest_alpha.heartbeat(uuid,uuid,integer),
    honest_alpha.reclaim(text,integer), honest_alpha.complete(uuid,uuid,jsonb), honest_alpha.fail(uuid,uuid,jsonb,integer) TO hal_worker;
GRANT USAGE ON SCHEMA honest_alpha_sealed TO hal_validator;
GRANT SELECT ON honest_alpha_sealed.records TO hal_validator;
