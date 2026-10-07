# Optional Render Free + Neon deployment (candidate)

This is an **un-deployed candidate**, not a claim that unattended school login,
free-tier reliability, or a real provider deployment has been verified. The
existing local SQLite/Vault workflow remains the default.

## Design

- `DATABASE_URL` selects PostgreSQL for OAuth state. If it is absent, SQLite is
  used. A PostgreSQL error never falls back to SQLite. Use a dedicated database
  for one student/instance; do not share it across independent issuers.
- The OAuth payloads remain Fernet-encrypted using a key derived from the separate
  `PLMS_CONNECT_PASSWORD`. Raw bearer/refresh/code values are hashed for lookup.
  Encrypted OAuth client registration can contain client secrets, so this database
  is still sensitive. Keep its password/salt/check and connection password stable.
- With explicit `PLMS_ENV_LOGIN=1`, the HTTP server reads the school username and
  password from the host's secret environment. School credentials, cookies and
  student snapshots stay in process memory and are **not** sent to PostgreSQL.
- The database also holds a non-secret credential generation and a durable
  failed/in-progress login claim. An atomic claim commits before school login.
  A verified session and a PLMS cookie are required before the claim is cleared.
  Failure, MFA, cancellation, process termination or an uncertain database commit
  leaves login disabled for that generation. Restarting never re-enables it.
- There is no startup login, password-guessing loop, cron job or keep-alive ping.
  The first authenticated PLMS read after restart may request one school login.
  Cold starts lose cookies, so successful calls on separate days can each require
  a school login. Frequent sleeps/restarts can therefore increase login frequency.
- Run one worker. Database transactions use a PostgreSQL transaction advisory
  lock for deployment-overlap safety; consent rate limits remain process-local.

## Owner-controlled setup, only after approval

1. Confirm the current Render/Neon free plans and their limits. Do not create any
   paid resource, Render cron job or paid disk. This template uses the latest Python 3.12 patch version via `.python-version`, has `plan: free`
   and disables automatic deployment. It creates no database automatically.
2. Create a dedicated Neon database/role with only the needed schema privileges.
   Creating ongoing access and accepting provider agreements need owner approval.
3. Enter secrets directly in Render's secure environment settings, using a supported
   secure handoff. Never paste passwords, database URLs or connection secrets in
   chat, repository files, shell arguments, build commands, screenshots or logs.
   Required settings are:
   - `DATABASE_URL`: Neon PostgreSQL connection URL. The driver overrides SSL
     settings to `verify-full` and system trust roots; install `.[postgres]`.
   - `PLMS_CONNECT_PASSWORD`: separate strong MCP connection secret, at least
     20 characters. This is not the school password.
   - `PLMS_USERNAME` and `PLMS_PASSWORD`: approved school account credentials.
   - `PLMS_CREDENTIAL_GENERATION`: stable unique non-secret label, e.g. an owner
     chosen reconnect revision. Never generate a new one automatically per boot.
   - `PLMS_ENV_LOGIN=1`: explicit permission to use the configured environment
     credentials for read-only login. Optional `PLMS_LOGIN_METHOD=sso|direct`.
4. Render sets `RENDER_EXTERNAL_URL`; `scripts/render_start.py` uses it as the
   OAuth issuer. Set `PLMS_ISSUER_URL` only for a verified custom HTTPS origin.
5. Deploy this reviewed source only with approval. Check `/healthz`, anonymous
   `/mcp` rejection, OAuth metadata and a complete PKCE client connection.
   Health checks do not contact PLMS or verify school-login readiness.
6. With approval for the real school login, verify a read, then restart/sleep and
   verify the same OAuth connection, token refresh and another read. Test failure
   and MFA handling with synthetic fixtures, not repeated real wrong passwords.
7. Only after these checks, update the connected plugin endpoint and verify the
   intended daily read. Existing grants in a local SQLite database do not migrate
   automatically; clients must reconnect to the new issuer.

## Failure/reconnect behavior

If a generation becomes disabled, stop and inspect the cause. MFA or school
browser action may make unattended login impossible. There is deliberately no
automatic latch reset. After an explicit owner-approved reconnect/credential
change, update the secrets and choose a **new, never-used generation** in Render.
Do not rotate generation repeatedly to retry a failing password. Keep old rows so
an older overlapping deployment cannot re-enable a failed generation.

Database operations are bounded by connection/statement/lock timeouts. If an idle
database connection has died, that request fails closed and closes the connection;
a later independent request can reconnect. A partially completed write/commit is
never replayed. A failure to read or commit the durable claim prevents a new school login.

Render Free may sleep and a cold start can exceed an MCP client's timeout. A dot
scheduled read can wake it but cannot guarantee the provider's response time. A
retry, if needed, must be a bounded read, not a password retry. No paid cron is
required. Usage limits and any account-level charges still need owner review.

## Validation before production use

Run all existing tests, the new cloud-storage tests, Ruff, build, dependency audit
and the container checks. The `postgres-integration` CI job runs `tests/test_postgres_integration.py`
against an ephemeral PostgreSQL 17 service with a temporary test certificate and
synthetic credentials. It reuses OAuth/storage/login regression cases and tests
concurrent initialization and connection loss. The service is localhost-only;
each case creates and drops only its own randomly named test database. No school
or production credentials are used. The ordinary CI jobs still run full pytest,
Ruff, build, audit and container checks. These jobs have not been triggered by
preparing this candidate.

`PLMS_POSTGRES_SSLROOTCERT` can specify an explicit CA file (the ephemeral CI
certificate in tests); production defaults to system roots. Hostname verification
remains mandatory in either case. Never disable TLS verification for Neon.

The included driver-contract tests use a fake driver; the real integration job
must pass before deployment, and live Neon pooling/restart checks remain separate. Then perform the provider restart/OAuth/read
checks above. Passing SQLite or fake-driver tests is not evidence that Neon,
pooling, TLS roots, Render builds or the real school SSO flow work.

References: [Render Blueprint](https://render.com/docs/blueprint-spec),
[Render Free](https://render.com/docs/free),
[Psycopg connections](https://www.psycopg.org/psycopg3/docs/api/connections.html).
