#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""One-shot PostgreSQL bootstrap; runtime services never receive DBA secrets."""

import json
from pathlib import Path

import psycopg
from psycopg import sql


SECRETS = Path('/run/secrets/database-admin')
SCHEMA = Path(__file__).resolve().parents[1] / 'bridge/space_schema.sql'


def password_file(name):
    value = (SECRETS / name).read_text().strip()
    if len(value) < 32 or any(c.isspace() for c in value):
        raise ValueError('Invalid credential file')
    return value


def migrate():
    admin_password = password_file('postgres.password')
    app_password = password_file('app.password')
    # Disable statement/error-query logging only for this administrative session.
    # Passwords never enter argv, environment values or printed SQL/exceptions.
    options = (
        '-c log_statement=none -c log_min_error_statement=panic '
        '-c log_min_messages=panic -c log_min_duration_statement=-1 '
        '-c log_min_duration_sample=-1 -c log_transaction_sample_rate=0'
    )
    with psycopg.connect(host='database', port=5432, dbname='baton',
                        user='baton_bootstrap', password=admin_password,
                        connect_timeout=10, options=options) as conn:
        # A stable lock serializes duplicate/restarted migration jobs. All roles,
        # schema changes and grants below commit or roll back together.
        conn.execute('SELECT pg_advisory_xact_lock(%s, %s)', (1111577679, 1))
        roles = {row[0] for row in conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname IN ('baton_owner', 'baton_app')"
        )}
        if 'baton_owner' not in roles:
            conn.execute('CREATE ROLE baton_owner NOLOGIN')
        conn.execute('ALTER ROLE baton_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS')
        if 'baton_app' not in roles:
            conn.execute('CREATE ROLE baton_app NOLOGIN')
        conn.execute(sql.SQL(
            'ALTER ROLE baton_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}'
        ).format(sql.Literal(app_password)))
        if conn.execute("SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='baton_app')").fetchone():
            raise ValueError('Runtime role has unexpected membership')

        conn.execute('REVOKE ALL ON DATABASE baton FROM PUBLIC')
        conn.execute('GRANT CONNECT ON DATABASE baton TO baton_app')
        conn.execute('GRANT CREATE ON DATABASE baton TO baton_owner')
        current_owner = conn.execute(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='baton'"
        ).fetchone()
        if current_owner and current_owner[0] != 'baton_owner':
            raise ValueError('Schema has an unexpected owner')
        conn.execute('CREATE SCHEMA IF NOT EXISTS baton AUTHORIZATION baton_owner')
        conn.execute('SET LOCAL ROLE baton_owner')
        conn.execute(SCHEMA.read_text())
        conn.execute('RESET ROLE')

        conn.execute('REVOKE ALL ON SCHEMA baton FROM PUBLIC')
        conn.execute('REVOKE ALL ON ALL TABLES IN SCHEMA baton FROM PUBLIC')
        conn.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA baton FROM PUBLIC')
        conn.execute('REVOKE ALL ON SCHEMA baton FROM baton_app')
        conn.execute('GRANT USAGE ON SCHEMA baton TO baton_app')
        conn.execute('REVOKE ALL ON ALL TABLES IN SCHEMA baton FROM baton_app')
        conn.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA baton TO baton_app')
        conn.execute('REVOKE ALL ON TABLE baton.x_audit FROM baton_app')
        conn.execute('GRANT SELECT, INSERT ON TABLE baton.x_audit TO baton_app')
        conn.execute('REVOKE ALL ON TABLE baton.schema_version FROM baton_app')
        conn.execute('GRANT SELECT ON TABLE baton.schema_version TO baton_app')
        conn.execute('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA baton TO baton_app')
        if conn.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='baton' AND pg_get_userbyid(c.relowner) <> 'baton_owner'"
        ).fetchone():
            raise ValueError('Schema object has an unexpected owner')
    print(json.dumps({'status': 'ok', 'schema': 'baton', 'runtime_role': 'baton_app',
                      'schema_owner': 'baton_owner', 'secret_values_displayed': False}))


if __name__ == '__main__':
    try:
        migrate()
    except Exception:
        raise SystemExit('Database migration failed; SQL, credentials and connection details suppressed.') from None
