# Piko Pi Agent — Release 2

A self-hosted personal AI assistant for a Raspberry Pi or Linux computer. Piko combines a private web dashboard, voice, tool-driven tasks and an optional FlareSend WhatsApp adapter.

**Experimental personal-administrator software, not a sandbox or multi-user service.** Commands run with the operating-system privileges of the account running Piko. Use a dedicated unprivileged account; do not run as root or give it passwordless sudo. Keep the dashboard on your private network behind HTTPS. API usage is billed by your model provider.

## Release 2

- Private dashboard built with plain HTML, CSS and JavaScript; Python/FastAPI backend and OpenAI Agents SDK.
- A coordinator with up to two bounded research workers, task cancellation and explicit command approval controls.
- Durable command audit records, bounded history and opt-in scoped file diffs.
- Browser voice and command readback; optional, disabled-by-default WhatsApp integration.
- Generic deployment configuration, private runtime-data exclusions and an MIT-licensed source snapshot.

This release starts a clean public history. It contains no private deployment history or saved conversations. It is not a claim of production readiness or a comprehensive security audit.

## Setup

Requires Linux, Python 3.11 or newer, and a private HTTPS reverse proxy. Run one application process. System telemetry uses Linux `/proc`; health checks expect common Linux tools, with optional Docker/systemd/Tailscale integration.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
mkdir -p "$HOME/piko-workspace"
export PI_AGENT_ALLOWED_ORIGINS='https://piko.example.com'
export PI_AGENT_WORKSPACE="$HOME/piko-workspace"
uvicorn backend.piko.app:app --host 127.0.0.1 --port 8765 --workers 1
```

Replace the example origin with your exact private HTTPS dashboard origin, including its port if nonstandard. Proxy that origin to `127.0.0.1:8765`; do not bind the dashboard to a public interface. Session cookies remain Secure/HttpOnly. Configure HTTPS before signing in; plain HTTP is not a supported login setup.

See the single `.env.example` for core and optional WhatsApp settings. The app does **not** automatically load `.env` files: export values in your shell or use your service manager's environment configuration. Do not commit real settings or secrets.

At first start, Piko creates a local `data/login-token` in the repository root. Read that file **locally on your own host**, use the access code to sign in, then add your OpenAI API key through the private dashboard Settings. Do not paste the token or key into an issue or chat. `PI_AGENT_DATA` can select a different private runtime-data directory. Keep it outside version control and readable only by the service account. Back it up securely if you need to retain conversations.

## Security and behavior

- Authentication and the explicit origin allowlist protect the dashboard. No wildcard origins or public anonymous access are provided.
- Generated commands follow the saved approval policy; detected sudo commands always require fresh approval. Command inspection is not an OS privilege sandbox: arbitrary scripts can evade a keyword-based distinction. Review every proposed command and rely on OS least privilege.
- A workspace setting chooses a default working directory, **not** a filesystem access boundary. File-diff snapshots are opt-in and incomplete; they are not rollback.
- Task/chats, command output and audit records persist locally. Deleting a chat does not undo prior commands or remove durable audit records.
- Stop Task cancels current work but does not reverse completed changes. Restart interrupts active work rather than replaying it.
- Voice captures audio only after you start it. Ending voice does not automatically stop an active task; there is no wake-word or speaker authentication.
- Inputs, output and file content can contain malicious instructions. Never use this project as an unrestricted public agent endpoint.

## Optional WhatsApp

The adapter is disabled by default and is not needed for the dashboard. Its variables are grouped in the single `.env.example`. Replace placeholders locally and explicitly export the settings or configure your service manager; the app does not automatically read `.env`.

Use your own provider account, dedicated credentials and explicitly authorized sender. Verify the provider's current webhook contract before enabling processing. The separate receiver in `receiver/pi_receiver.py` must never expose the dashboard API. It validates incoming events and passes authenticated messages to the private adapter; it is not a general public agent endpoint. Never commit real credentials, sender identities or provider payloads.

**Credential modes are unchanged:** the Python receiver requires systemd credential mounts. The adapter also uses credential mounts when `CREDENTIALS_DIRECTORY` is present and will not fall back to environment secrets if a mount is missing. Its environment-secret variables are for non-systemd development only; putting them in `.env` is not a replacement for receiver credential mounts. Model credentials are still entered through the private dashboard, not the example file.

Conversation command grants, fresh sudo approval, deduplication and uncertain-delivery handling remain in place. `EXIT` clears the conversation grant. Task cancellation does not undo completed commands. Corrections require one-use confirmation; do not automatically retry uncertain sends. Verify configuration with synthetic input before any authorized live messages.

### Deployment boundaries

- **Dashboard:** from the repository root run the Uvicorn command above, one process, loopback only behind private HTTPS.
- **Python receiver:** `python receiver/pi_receiver.py`. The service template is `deploy/systemd/piko-webhook.service`; its example paths assume the repository is installed at `/opt/piko-pi-agent`. Review paths and configure your own credential mounts/environment before installation. Processing remains disabled by default.
- **Vercel stub:** `deploy/vercel/` is a separate deployment root. Its webhook is intentionally fail-closed, not an active forwarding bridge. Moving these files does not enable or deploy anything.

No real provider, reverse proxy, systemd deployment or audio hardware was configured by this refactor.

## Verification

The test suite has been removed from the current branch at the maintainer's request. It remains recoverable from Git history and the preserved `v2.0.0` release. No automated CI is configured.

You can check source syntax without starting the application:

```sh
python -m compileall -q backend receiver
node --check frontend/assets/js/app.js
node --check deploy/vercel/api/webhook.js
```

Syntax checks are not runtime or integration tests. Validate HTTPS/authentication, command approvals, real-device audio and any messaging configuration in your own isolated deployment before use.

## Project layout

```text
backend/piko/        Python web app, configuration, agent, commands and voice
  integrations/whatsapp/  Optional adapter, media and summaries
frontend/           index.html and assets/{js,css,fonts,icons}
receiver/           Independently deployed Python webhook receiver
deploy/             systemd service and separate Vercel stub
```

`backend/piko/app.py` is the web entrypoint. Audit/history helpers live beside it. The frontend remains plain HTML/CSS/JavaScript with no build step; the backend serves its assets at `/static`. Runtime data stays in root `data/` by default or the existing `PI_AGENT_DATA` override. This reorganization does not migrate, delete or publish runtime data.

For an existing checkout, update its launch command and service paths before switching versions; keep the same explicit `PI_AGENT_DATA` path if your deployment uses one. Do not copy secrets into the repository. The `v2.0.0` tag is unchanged.

## Contributing and license

Small, focused pull requests with clear verification steps are welcome. Do not include credentials, private hostnames, conversation exports or real webhook payloads. Experimental community project: no support SLA. For security concerns, avoid posting exploit details or secrets publicly; use GitHub's private vulnerability reporting when available.

Project code is licensed under [MIT](LICENSE). Bundled fonts retain their separate SIL Open Font License notices in `frontend/assets/fonts/OFL-*.txt`.
