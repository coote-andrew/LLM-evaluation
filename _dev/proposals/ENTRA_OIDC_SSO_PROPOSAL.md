# Entra OIDC single sign-on proposal

## Objective

Add Entra ID OpenID Connect login without removing the current Django
username/password login. Initially, Entra Enterprise Application group
assignment is the application admission boundary. Existing detailed RBAC
remains attached to Django users and is unaffected by a successful SSO login.

## Scope and decisions

- Use `mozilla-django-oidc` authorization-code flow with PKCE.
- Keep the existing `/accounts/login/` password form and show an SSO button
  when SSO is enabled.
- Use the registered callback exactly as supplied:
  `https://cicada.apps.rmhopnstkd01a.ssg.org.au/entra/login-success`.
- A new, approved SSO user receives an active Django user with an unusable
  password and no staff, superuser, Django-group, or application permissions.
- Link identities by Entra tenant ID plus the immutable Entra `oid` claim.
  Do not use email or UPN as a continuing identity key.
- Retain at least one password-based superuser as a documented break-glass
  account. It must not be linked to Entra and must have a strong, separately
  stored password.

## Entra configuration required

The platform administrator must supply these values:

| Environment variable | Entra source |
| --- | --- |
| `ENTRA_CLIENT_ID` | App registration Application (client) ID |
| `ENTRA_TENANT_ID` | App registration Directory (tenant) ID |
| `ENTRA_CLIENT_SECRET` | Client-secret **Value**, never its Secret ID |
| `ENTRA_APPROVED_GROUP_ID` | Object ID of the group allowed into Cicada; needed when token group enforcement is enabled |

Configure a single-tenant web application with the production callback above.
Assign the access group to the Enterprise Application. This is the initial
upstream access boundary. Later, configure the group claim to appear in ID
tokens, preferably as “Groups assigned to the application,” then set
`ENTRA_REQUIRE_GROUP_CLAIM=true` for a second, application-side check.

Minimum claims consumed by the application are `iss`, `aud`, `tid`, and `oid`.
`groups` is additionally required only when `ENTRA_REQUIRE_GROUP_CLAIM=true`.
`preferred_username` and `email` are optional display/contact data.
No Microsoft Graph permissions are required in this first release.

### Group overage

Entra omits individual group IDs for users with very large group membership
and emits an overage indicator instead. This implementation deliberately
denies that login and logs `group overage requires a Graph lookup`; it does
not weaken the access boundary. If it occurs in PRD, a follow-up change can
use a least-privilege Graph membership lookup, subject to security review.

## Django implementation

`core.authentication.EntraOIDCAuthenticationBackend`:

1. validates the signed RS256 token using Entra JWKS, nonce, issuer, tenant ID,
   and client audience;
2. optionally requires the configured group ID in the signed ID-token
   `groups` claim when `ENTRA_REQUIRE_GROUP_CLAIM=true`;
3. resolves a user using `EntraIdentity(tenant_id, object_id)`;
4. creates an active but unprivileged local user when no link exists;
5. never grants staff, Django-group, or application-specific permissions.

`EntraIdentity` stores the stable Entra relation separately from `UserProfile`.
Existing access controls continue to refer to the same Django `User` primary
key, so project shares, ownership, audit records, and future per-user RBAC
survive a migrated login unchanged.

The OIDC backend uses the signed ID token rather than calling the Graph
userinfo endpoint. This keeps the minimum permission footprint and lets group
validation operate on token data. Access and ID tokens are not stored in the
session or application database.

The OIDC callback is `/entra/login-success` with no trailing slash. The
OpenShift router must set `X-Forwarded-Proto`; setting
`DJANGO_TRUST_X_FORWARDED_PROTO=true` tells Django to generate the registered
HTTPS URI rather than an HTTP callback.

## Existing-user migration

Do not auto-link an existing local account from a login email. That can join
the wrong account if an address or UPN is reassigned.

Prepare an administrator-reviewed CSV:

```csv
username,tenant_id,object_id,upn
existing_local_username,00000000-0000-0000-0000-000000000000,00000000-0000-0000-0000-000000000000,person@example.org
```

Preview it first:

```powershell
uv run python manage.py link_entra_identities .\entra-user-links.csv
```

Only apply a clean, reviewed mapping:

```powershell
uv run python manage.py link_entra_identities .\entra-user-links.csv --apply
```

The command rejects duplicate users, duplicate Entra IDs, already-linked users,
unknown users, malformed UUIDs, and any CSV with an error. New SSO accounts
created before migration remain unprivileged; assign their detailed RBAC in
Django after identity verification.

## Deployment and rollout

1. Copy `openshift/entra-oidc-secret.example.yaml` outside the repository,
   replace placeholders, and apply it to the PRD namespace.
2. Reference `cicada-entra-oidc` from the web Deployment using `envFrom` (or
   equivalent individual secret-key environment entries).
3. Run `uv run python manage.py migrate` as part of deployment.
4. Confirm `DJANGO_TRUST_X_FORWARDED_PROTO=true` only on trusted OpenShift
   router traffic.
5. Set `ENTRA_OIDC_ENABLED=true`; local password login remains visible.
6. Sign in with an approved non-admin test user. Confirm it has no elevated
   RBAC, then assign intended local permissions and retest.
7. Try an unassigned user, a user assigned to the app but outside the claimed
   group, a cancelled Entra sign-in, and a password-only break-glass admin.
8. Migrate existing users with the reviewed command and verify their project
   shares/ownership remain intact.

## Diagnostics and expected failures

The application logs outcome reasons under `core.authentication`,
`core.oidc_views`, and `mozilla_django_oidc`. Set
`ENTRA_OIDC_LOG_LEVEL=DEBUG` only temporarily during PRD diagnosis; do not log
authorization codes, ID tokens, access tokens, client secrets, email addresses,
or group membership lists.

| Symptom | Expected log / likely correction |
| --- | --- |
| Callback uses `http://` | Enable trusted router forwarding setting; confirm ingress sends `X-Forwarded-Proto: https`. |
| Entra rejects redirect URI | Match the exact no-trailing-slash callback and the tenant/app registration. |
| “no groups claim” | Configure group claims in the App Registration token configuration. |
| “not in configured access group” | Check group object ID, not its display name; check nested-group expectations. |
| “group overage” | Reduce emitted group scope or implement the separately reviewed Graph fallback. |
| Issuer/tenant/audience rejection | Confirm client ID and tenant ID belong to the same single-tenant app registration. |
| Generic login failure | Read the timestamp-correlated callback and backend log lines; the UI intentionally does not expose token validation details. |

## Security boundaries

- Client secret is supplied only through an OpenShift Secret and must never be
  put in `settings.py`, a committed YAML file, logs, or tickets.
- Initially, Entra Enterprise Application assignment is the admission
  boundary. Set `ENTRA_REQUIRE_GROUP_CLAIM=true` after Entra emits group claims
  to add a Django-side membership check.
- A disabled local account remains unable to sign in by SSO because Django
  checks `is_active` after backend resolution.
- Existing Django authorization remains the source of truth for detailed RBAC.
- The committed Django `SECRET_KEY` and Fernet `FIELD_ENCRYPTION_KEY` currently
  present in `config/settings.py` should be rotated and moved to OpenShift
  secrets in a separate security change before production rollout.

## Verification

Automated tests cover token claim validation, account creation, stable identity
resolution, preservation of an existing user’s local role data, local password
login, callback URL construction, and migration-command validation. PRD tests
must confirm the real Entra registration and OpenShift proxy behavior because
they cannot be proven by a unit test.
