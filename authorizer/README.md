# Authorizer

What it does: Checks if people have permission to use the API directly.

## In simple terms

This is like a bouncer at a club. It looks at the "ID" (token) people show and decides whether to let them in.

- If you're a GitHub or GitLab webhook, you don't need an ID - the bouncer just checks the secret handshake
- If you're calling the API yourself, you need a valid Cognito token

## Why it matters

Keeps the system secure while allowing Git providers to work smoothly without needing API keys.