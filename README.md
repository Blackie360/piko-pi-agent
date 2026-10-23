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
uvicorn app:app --host 127.0.0.1 --port 8765 --workers 1
```

Replace the example origin with your exact private HTTPS dashboard origin, including its port if nonstandard. Proxy that origin to `127.0.0.1:8765`; do not bind the dashboard to a public interface. Session cookies remain Secure/HttpOnly. Configure HTTPS before signing in; plain HTTP is not a supported login setup.

See `.env.example` for settings. The app does **not** automatically load `.env` files: export values in your shell or use your service manager's environment configuration. Do not commit real settings or secrets.

At first start, Piko creates a local `data/login-token`. Read that file **locally on your own host**, use the access code to sign in, then add your OpenAI API key through the private dashboard Settings. Do not paste the token or key into an issue or chat. `PI_AGENT_DATA` can select a different private runtime-data directory. Keep it outside version control and readable only by the service account. Back it up securely if you need to retain conversations.

## Security and behavior

- Authentication and the explicit origin allowlist protect the dashboard. No wildcard origins or public anonymous access are provided.
- Generated commands follow the saved approval policy; detected sudo commands always require fresh approval. Command inspection is not an OS privilege sandbox: arbitrary scripts can evade a keyword-based distinction. Review every proposed command and rely on OS least privilege.
- A workspace setting chooses a default working directory, **not** a filesystem access boundary. File-diff snapshots are opt-in and incomplete; they are not rollback.
- Task/chats, command output and audit records persist locally. Deleting a chat does not undo prior commands or remove durable audit records.
- Stop Task cancels current work but does not reverse completed changes. Restart interrupts active work rather than replaying it.
- Voice captures audio only after you start it. Ending voice does not automatically stop an active task; there is no wake-word or speaker authentication.
- Inputs, output and file content can contain malicious instructions. Never use this project as an unrestricted public agent endpoint.

## Optional WhatsApp

WhatsApp is disabled by default. [WHATSAPP.md](WHATSAPP.md) contains the provider contract, receiver topology, credential setup, approval behavior and offline tests. Configure your own provider account and verify the delivery contract before enabling processing. Webhook ingress must never expose the dashboard API. Nothing in this repository configures or deploys a live account.

## Checks

Tests use synthetic credentials and mocked provider/model calls; they must not use a real Pi, microphone or provider account.

```sh
python -m unittest discover -p 'test_*.py' -v
node --test public_receiver/webhook.test.js
```

Optional browser checks require Playwright and Chromium installed separately:

```sh
node test_ui.cjs
node test_voice_control.cjs
```

`PI_AGENT_PLAYWRIGHT_MODULE`, `PI_AGENT_CHROMIUM`, `PI_AGENT_SOURCE` and `PI_AGENT_QA_DIR` let you point these checks at existing local tools and artifact directories. Browser suites intercept network requests and use synthetic audio objects. Real-device audio, proxy setup and live messaging still need deployment-specific validation.

## Project layout

- `app.py`: web API, coordinator, voice and approval orchestration
- `command_audit.py`, `conversation_context.py`: local audit/history helpers
- `static/`: no-build dashboard, fonts and icons
- `whatsapp*.py`, `public_receiver/`: optional messaging integration
- `test_*`: offline regression tests
- `README.md`, `WHATSAPP.md`: the two project guides

This release intentionally keeps the existing flat code structure; a package refactor is not included.

## Contributing and license

Small, focused pull requests with offline tests are welcome. Do not include credentials, private hostnames, conversation exports or real webhook payloads. Experimental community project: no support SLA. For security concerns, avoid posting exploit details or secrets publicly; use GitHub's private vulnerability reporting when available.

Project code is licensed under [MIT](LICENSE). Bundled fonts retain their separate SIL Open Font License notices in `static/fonts/OFL-*.txt`.
