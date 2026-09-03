# CloudFormation

What it does: The blueprint for building the system.

## In simple terms

This is the architectural drawing that tells AWS what to build:
- One API endpoint for webhooks
- Two Lambdas (one for receiving, one for reviewing)
- SQS queue to buffer requests
- DynamoDB for tracking costs
- WAF and Cognito for security

## Why it matters

Everything is defined in code, so you can:
- Rebuild the system in seconds
- See exactly what resources are being used
- Keep your infrastructure under version control