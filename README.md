# Rodney (personal)

Static-HTML micro-apps at `<base>/<slug>/`, deployed by Claude through an MCP server.

    src/rodney/validate.py     slug + path rules, content types
    src/rodney/store.py        Store interface; S3Store (real) and MemoryStore (tests)
    src/rodney/auth.py         password -> author
    src/rodney/apps.py         create / get / read / write / delete / list
    src/rodney/admin_page.py   the static /<slug>/admin/ page
    src/rodney/mcp.py          MCP protocol (JSON-RPC) + tool definitions
    src/rodney/handler.py      AWS Lambda entry point
    infra/                   AWS CDK (Python): bucket, Lambda, CloudFront, Route 53, certificate
    site/, scripts/          landing page, and the script that publishes it plus the skill zip

Auth is a stopgap: `RODNEY_USERS='{"ptkinvent":"<pw>","friend":"<pw>"}'`. Every tool takes `password`,
which also determines the app's author.

## Setup and local checks (no AWS needed)

    uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
    npm install                      # only for the `cdk` CLI, which is a Node program
    .venv/bin/pytest
    RODNEY_USERS='{"a":"x"}' npx cdk synth

## Deploy

The CloudFormation stack is still named `Spin`; CloudFormation can't rename a stack.

    RODNEY_USERS='{...}' npx cdk diff     # read-only preview
    RODNEY_USERS='{...}' npx cdk deploy [-c domainName=... -c hostedZoneId=... -c baseUrl=...]
