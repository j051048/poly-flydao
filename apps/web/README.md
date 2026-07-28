# Polybot Web Console

Next.js control plane for the tenant-scoped Zeabur backend. The browser never
persists or repeatedly forwards AI keys or wallet private keys.

## Vercel environment

Copy `.env.example` and configure:

- `NEXT_PUBLIC_API_BASE_URL`: HTTPS Zeabur API origin.
- `NEXT_PUBLIC_SUPABASE_URL`: public Supabase project URL.
- `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`: public publishable key. The legacy
  anon key variable is also supported.

Do not add the Supabase service-role key, AI provider keys, EVM keys, seed
phrases, CLOB credentials, or shared administrator tokens to Vercel.

Add these URLs to the Supabase Auth redirect allow-list:

```text
http://localhost:3000/auth/callback
https://YOUR_VERCEL_DOMAIN/auth/callback
```

When the two Supabase public variables are absent, production builds still
succeed and the registration/login forms show a disabled configuration notice.

## Session and API boundary

The middleware calls `supabase.auth.getUser()` and writes refreshed auth cookies
before protected pages render. Browser API calls obtain the current Supabase
session and send only:

```text
Authorization: Bearer <Supabase JWT>
Content-Type: application/json
Idempotency-Key: <random UUID>  # mutating jobs/actions
```

The old `polybot_settings` local-storage item is deleted without being read.
Secrets submitted from the credential page live in React memory until the
one-time HTTPS request completes, then the input state is cleared.

## Verification

```bash
npm ci
npm test
npm run build
```

The build is intentionally verified both without Supabase variables and with
syntactically valid public test values.
