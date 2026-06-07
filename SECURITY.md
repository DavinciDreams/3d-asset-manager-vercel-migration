# Security Guidelines

## Secrets

Keep production secrets in Coolify environment variables, not in git.

Critical values:

- `DATABASE_URL`: Postgres connection string
- `MINIO_ROOT_USER`: MinIO admin/access key
- `MINIO_ROOT_PASSWORD`: MinIO admin/secret key
- `SECRET_KEY`: Flask session signing key
- `TELLUS_PERSISTENCE_API_TOKEN`: shared token for Tellus Durable Object writes
- `MONGODB_URI`: source database URL used only during the one-time migration

Generate strong tokens with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Deployment

- Use HTTPS for the public asset manager API.
- Use a private Postgres network when Coolify and Postgres are on the same host.
- Keep MinIO API access private to the app network unless a specific public
  access pattern is designed.
- Do not expose the MinIO console publicly without strong credentials and HTTPS.
- Rotate `TELLUS_PERSISTENCE_API_TOKEN` if it is ever exposed.
- Remove `MONGODB_URI` from production after migration is complete.

## Version Control

Never commit:

- `.env` files
- production database URLs
- API keys
- service tokens
- private asset credentials

Safe to commit:

- `.env.example`
- documentation with placeholder values
- non-secret deployment templates
