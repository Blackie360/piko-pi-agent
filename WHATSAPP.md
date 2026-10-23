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

## Verification

Test files are not included on the current branch. The original offline suite remains in Git history and the immutable `v2.0.0` release. No live messaging validation is implied by source publication.

Before enabling the optional adapter, validate its authentication, sender restrictions, approval behavior and provider compatibility in an isolated environment using synthetic input. Do not send real messages without authorization. See [README.md](README.md) for source-syntax checks and deployment limits.

## Source map

- `whatsapp.py`: adapter configuration, sender validation, tasks and delivery state
- `public_receiver/pi_receiver.py`: optional incoming-provider normalization
- `whatsapp_media.py`: media validation and send controls
- `whatsapp_summary.py`: bounded outgoing summaries
- `whatsapp.env.example`: disabled placeholder configuration

See [README.md](README.md) for the core setup, security limits and MIT licensing. Keep private operational runbooks outside the public repository.
