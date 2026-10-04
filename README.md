# Rodney (personal)

Static-HTML micro-apps at `<base>/<slug>/`, deployed by Claude through an MCP server.

    src/rodney/validate.py     slug + path rules, content types
    src/rodney/store.py        Store interface; S3Store (real) and MemoryStore (tests)
    src/rodney/auth.py         the allowlist: author name -> Google email
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
server (`oauth.py`); only the Google accounts listed in `RODNEY_USERS` get in, and each maps to an
author name: `RODNEY_USERS='{"ptkinvent":"you@gmail.com","friend":"them@gmail.com"}'`.

One-time setup, outside this repo:

1. Create a Google OAuth client (type: Web application) with the redirect URI
   `https://mcp.<domain>/callback`. Put its client ID in `cdk.json` as `googleClientId`.
2. Store the secrets in SSM Parameter Store as SecureStrings:

       aws ssm put-parameter --name /rodney/google-client-secret --type SecureString --value ...
       aws ssm put-parameter --name /rodney/signing-key --type SecureString --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

## Setup and local checks (no AWS needed)

    uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
    npm install                      # only for the `cdk` CLI, which is a Node program
    .venv/bin/pytest
    RODNEY_USERS='{"a":"a@example.com"}' npx cdk synth -c googleClientId=test

## Deploy

The CloudFormation stack is still named `Spin`; CloudFormation can't rename a stack.

    RODNEY_USERS='{...}' npx cdk diff     # read-only preview
    RODNEY_USERS='{...}' npx cdk deploy

`domainName` and `hostedZoneId` are set in `cdk.json`, so a plain deploy keeps the domain.
