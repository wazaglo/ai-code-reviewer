# Lambda Functions

What it does: The actual workhorses of the system.

## In simple terms

Two Lambda functions work together:

**1. Ingest Lambda** - The receptionist
- Receives webhooks from GitHub/GitLab
- Checks they're legitimate (using secret signatures)
- Puts them in a queue to wait their turn

**2. Worker Lambda** - The reviewer
- Takes reviews from the queue
- Fetches the changed code from GitHub/GitLab
- Sends it to the AI for analysis
- Posts the review comments back to the PR
- Sometimes auto-merges or closes based on severity

## Why it matters

This separation means:
- Webhooks are accepted instantly (no waiting for AI)
- Reviews happen in the background
- The system can handle many PRs at once