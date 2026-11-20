-- Domain schema. Idempotent: applied on every startup.
-- Every tenant-owned table is keyed by (company_id, id) and every cross-table reference is a
-- COMPOSITE foreign key that includes company_id, so a row of one company can never point at a
-- row of another company. The database itself refuses cross-tenant references.

CREATE TABLE IF NOT EXISTS companies (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  timezone      TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  desk_phone    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_policies (
  company_id                    TEXT PRIMARY KEY REFERENCES companies(id),
  cutoff_minutes_pickup         INT  NOT NULL,
  cutoff_minutes_drop           INT  NOT NULL,
  cutoff_escalatable            BOOLEAN NOT NULL,
  max_seats_per_cab             INT  NOT NULL CHECK (max_seats_per_cab > 0),
  change_window_minutes         INT  NOT NULL,
  window_escalatable            BOOLEAN NOT NULL,
  max_changes_per_day           INT,
  daily_limit_escalatable       BOOLEAN NOT NULL DEFAULT FALSE,
  approval_timeout_minutes      INT  NOT NULL,
  alternatives_count            INT  NOT NULL DEFAULT 3,
  alternatives_window_minutes   INT  NOT NULL DEFAULT 180,
  version                       INT  NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS routes (
  company_id  TEXT NOT NULL REFERENCES companies(id),
  id          TEXT NOT NULL,
  name        TEXT NOT NULL,
  zones       TEXT[] NOT NULL,
  PRIMARY KEY (company_id, id)
);

CREATE TABLE IF NOT EXISTS employees (
  company_id  TEXT NOT NULL REFERENCES companies(id),
  id          TEXT NOT NULL,
  name        TEXT NOT NULL,
  zone        TEXT NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('employee','manager')),
  PRIMARY KEY (company_id, id)
);

CREATE TABLE IF NOT EXISTS trips (
  company_id         TEXT NOT NULL,
  id                 TEXT NOT NULL,
  route_id           TEXT NOT NULL,
  trip_type          TEXT NOT NULL CHECK (trip_type IN ('pickup','drop')),
  service_date       DATE NOT NULL,
  scheduled_at       TIMESTAMPTZ NOT NULL,
  vehicle_seats      INT NOT NULL,
  effective_capacity INT NOT NULL,
  seats_taken        INT NOT NULL,
  cab_label          TEXT NOT NULL,
  PRIMARY KEY (company_id, id),
  FOREIGN KEY (company_id, route_id) REFERENCES routes(company_id, id),
  -- Backstop for the atomic booking UPDATE: the DB never stores an over-booked cab.
  CHECK (seats_taken >= 0 AND seats_taken <= effective_capacity)
);

CREATE TABLE IF NOT EXISTS roster_entries (
  company_id    TEXT NOT NULL,
  id            TEXT NOT NULL,
  employee_id   TEXT NOT NULL,
  service_date  DATE NOT NULL,
  trip_type     TEXT NOT NULL CHECK (trip_type IN ('pickup','drop')),
  trip_id       TEXT,
  status        TEXT NOT NULL CHECK (status IN ('scheduled','cancelled')),
  version       INT  NOT NULL DEFAULT 1,
  PRIMARY KEY (company_id, id),
  UNIQUE (company_id, employee_id, service_date, trip_type),
  FOREIGN KEY (company_id, employee_id) REFERENCES employees(company_id, id),
  FOREIGN KEY (company_id, trip_id)     REFERENCES trips(company_id, id)
);

CREATE TABLE IF NOT EXISTS shift_requests (
  id               TEXT PRIMARY KEY,
  company_id       TEXT NOT NULL,
  employee_id      TEXT NOT NULL,
  roster_entry_id  TEXT NOT NULL,
  change_type      TEXT NOT NULL CHECK (change_type IN ('reschedule','cancel')),
  from_trip_id     TEXT,
  to_trip_id       TEXT,
  status           TEXT NOT NULL CHECK (status IN ('APPLIED','REJECTED','PENDING_APPROVAL',
                     'APPROVED_APPLIED','APPROVAL_REJECTED','APPROVED_BUT_UNAVAILABLE','EXPIRED','WITHDRAWN')),
  violations       JSONB NOT NULL DEFAULT '[]',
  idempotency_key  TEXT NOT NULL,
  thread_id        TEXT NOT NULL,
  roster_version   INT  NOT NULL,
  decision         TEXT CHECK (decision IN ('approved','rejected','expired','withdrawn')),
  decision_note    TEXT,
  decided_by       TEXT,
  decided_at       TIMESTAMPTZ,
  resumed_at       TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL,
  updated_at       TIMESTAMPTZ NOT NULL,
  UNIQUE (company_id, employee_id, idempotency_key),
  FOREIGN KEY (company_id, employee_id)     REFERENCES employees(company_id, id),
  FOREIGN KEY (company_id, roster_entry_id) REFERENCES roster_entries(company_id, id),
  FOREIGN KEY (company_id, from_trip_id)    REFERENCES trips(company_id, id),
  FOREIGN KEY (company_id, to_trip_id)      REFERENCES trips(company_id, id)
);

-- At most one open approval per roster entry.
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_entry
  ON shift_requests (company_id, roster_entry_id) WHERE status = 'PENDING_APPROVAL';

-- The sweeper scans open approvals only.
CREATE INDEX IF NOT EXISTS shift_requests_open
  ON shift_requests (status, decision, resumed_at) WHERE status = 'PENDING_APPROVAL';

CREATE TABLE IF NOT EXISTS audit_log (
  id             BIGSERIAL PRIMARY KEY,
  at             TIMESTAMPTZ NOT NULL,
  company_id     TEXT NOT NULL,
  actor_type     TEXT NOT NULL CHECK (actor_type IN ('employee','manager','agent','system')),
  actor_id       TEXT NOT NULL,
  action         TEXT NOT NULL,
  request_id     TEXT,
  thread_id      TEXT,
  correlation_id TEXT,
  payload        JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turn_metrics (
  id             BIGSERIAL PRIMARY KEY,
  at             TIMESTAMPTZ NOT NULL,
  company_id     TEXT NOT NULL,
  thread_id      TEXT NOT NULL,
  correlation_id TEXT NOT NULL,
  latency_ms     INT NOT NULL,
  llm_calls      INT NOT NULL,
  tool_calls     INT NOT NULL,
  outcome        TEXT
);
