# Deploy / Infra Rules

Binding for Dockerfile, docker-compose, and AWS deploy work. Specialists (`deploy-infra`) and reviewers (`deploy-review`) load this before acting.

- **Secrets never live in committed config or the image.** They come from environment variables or a secrets manager at deploy time. If a value looks like a credential, connection string, or API key, it does not appear in a Dockerfile, compose file, or committed YAML.
- **One image, config-driven environments.** Dev/prod differences are selected by an environment variable pointing at environment-specific config, not separate builds or separate binaries. An empty/unset value disables a feature rather than requiring a build flag.
- **Pin base image versions.** No `:latest` in anything meant to be reproducible.
- **Idempotency key + pre-check before any external write** (e.g. calling an embedding API, writing to a managed service). Before creating something externally, check if it already exists for a stable key — makes retries safe.
- **The Fargate decision (DECISIONS #1) is binding.** A switch to Lambda or any other target requires a new DECISIONS.md entry and Amber's sign-off — not a unilateral change mid-build.
- **The Day-4 checkpoint in PLAN.md is binding.** If Fargate/RDS networking isn't converging by end of Day 4, invoke the documented fallback (PLAN.md section 3) rather than continuing to sink time into it.
