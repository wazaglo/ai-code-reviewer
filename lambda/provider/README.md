# Provider Layer

What it does: Lets the system work with both GitHub and GitLab.

## In simple terms

This is like a translator that speaks both GitHub and GitLab. The main review logic only needs to "speak English" (the common interface), and the provider handles translating to/from each platform.

## Files
- `github.py` - Speaks GitHub
- `gitlab.py` - Speaks GitLab
- `factory.py` - Figures out which language to use

## Why it matters

One review system can support multiple platforms without duplicating code.