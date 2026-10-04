# Rodney (personal)

Static-HTML micro-apps at `<base>/<slug>/`, deployed by Claude through an MCP server.

    src/rodney/validate.py     slug + path rules, content types
    src/rodney/store.py        Store interface; S3Store (real) and MemoryStore (tests)
    src/rodney/auth.py         anonymous author handles; the block list
    src/rodney/oauth.py        OAuth server for Claude: discovery, registration, login, tokens
    src/rodney/tokens.py       signed tokens (HS256) and PKCE
    src/rodney/authstore.py    single-use codes and refresh tokens (DynamoDB)
    src/rodney/google.py       Google sign-in
    src/rodney/apps.py         create / get / read / write / delete / list
    src/rodney/admin_page.py   the static /<slug>/admin/ page
    src/rodney/mcp.py          MCP protocol (JSON-RPC) + tool definitions
    src/rodney/handler.py      AWS Lambda entry point and router
    infra/                   AWS CDK (Python): bucket, Lambda, CloudFront, Route 53, certificate
    site/, scripts/          landing page, and the script that publishes it plus the skill zip

## Sign-in

Claude signs users in through OAuth, with Google as the identity provider. Rodney is the OAuth
server (`oauth.py`). **Anyone with a Google account can sign in.** Each person is identified by an
anonymous handle (`u-` plus a hash of their Google account id), so emails never appear on apps
or admin pages. Each author may have 50 apps (`MAX_APPS_PER_AUTHOR` in `apps.py`).

One-time setup, outside this repo:

1. Create a Google OAuth client (type: Web application) with the redirect URI
   `https://mcp.<domain>/callback`, and publish the consent screen to **In production** so people
   outside your test-user list can sign in. Put the client ID in `cdk.json` as `googleClientId`.
2. Store the secrets in SSM Parameter Store as SecureStrings:

       aws ssm put-parameter --name /rodney/google-client-secret --type SecureString --value ...
       aws ssm put-parameter --name /rodney/signing-key --type SecureString --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

## Operating it

Every sign-in and write is logged with the author handle (sign-ins also log the email, privately):

    aws logs tail /aws/lambda/<function-name> --since 1d | grep audit

- **Block one person:** add their handle to `blockedAuthors` (comma-separated, in `cdk.json` or
  `-c blockedAuthors=u-aaa,u-bbb`) and deploy. Their tokens stop working immediately.
- **Emergency off switch** for the MCP server and sign-in (apps keep being served):

      aws lambda put-function-concurrency --function-name <function-name> --reserved-concurrent-executions 0

  Turn it back on with `aws lambda delete-function-concurrency --function-name <function-name>`.
- **Hand an app to someone else:** `.venv/bin/python scripts/reassign_app.py <app> <handle>`.

## Setup and local checks (no AWS needed)

    uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
    npm install                      # only for the `cdk` CLI, which is a Node program
    .venv/bin/pytest
    npx cdk synth -c googleClientId=test

## Deploy

The CloudFormation stack is still named `Spin`; CloudFormation can't rename a stack.

    npx cdk diff     # read-only preview
    npx cdk deploy

`domainName` and `hostedZoneId` are set in `cdk.json`, so a plain deploy keeps the domain.
