# Optional WhatsApp integration

Piko Pi Agent Release 2 includes an experimental FlareSend adapter. It is **disabled by default** and is not needed to use the web dashboard. This public guide intentionally excludes deployment-specific infrastructure and account configuration.

## Before enabling

1. Set up your own provider account and review its current webhook/API contract.
2. Use your own dedicated integration credentials and an explicitly authorized sender. Keep credentials in a service secret store, never in Git or issue reports.
3. Review `whatsapp.env.example` for supported configuration names. Replace every placeholder locally; an example file does not enable the adapter.
4. Review and test the receiver and adapter in your isolated environment before enabling processing. Keep the dashboard private and separate from webhook ingress.
5. Obtain authorization before sending real messages. Use synthetic events for development.

The receiver and adapter authenticate requests, validate configured identities, reject self/group/outgoing messages where applicable, deduplicate events and fail closed on invalid configuration. These controls do not make the service safe for arbitrary untrusted users. Provider schema compatibility and real delivery must be verified for your own deployment.

## Task and approval behavior

WhatsApp tasks use the same command-safety model as the dashboard. Ordinary command grants are conversation-scoped; detected sudo requires fresh approval. `EXIT` ends the conversation grant. Corrections do not silently replace active work: replacement uses a one-use confirmation. Stopping a task does not undo completed changes.

Replies and optional media use bounded output and delivery tracking. Failed, blocked or uncertain sends must not be treated as successful delivery. Do not automatically retry uncertain sends. File access is restricted by the media tool's checks, not a general operating-system sandbox.

## Offline checks

Use the virtual environment from the main README. Tests use synthetic identities and mocked model/provider calls; no real account is needed.

```sh
python -m unittest test_whatsapp test_whatsapp_app test_whatsapp_approvals test_whatsapp_conversation test_whatsapp_media test_whatsapp_summary test_provider_bridge test_pi_receiver -v
node --test public_receiver/webhook.test.js
```

Browser-only voice tests are separate. These offline checks do not establish live provider delivery, Pi deployment readiness or real audio timing.

## Source map

- `whatsapp.py`: adapter configuration, sender validation, tasks and delivery state
- `public_receiver/pi_receiver.py`: optional incoming-provider normalization
- `whatsapp_media.py`: media validation and send controls
- `whatsapp_summary.py`: bounded outgoing summaries
- `whatsapp.env.example`: disabled placeholder configuration

See [README.md](README.md) for the core setup, security limits and MIT licensing. Keep private operational runbooks outside the public repository.
