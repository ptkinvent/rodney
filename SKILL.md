---
name: rodney
description: >-
  Rodney builds self-contained static HTML micro-apps from a conversation and
  deploys each to its own live URL. Use this skill whenever the user mentions
  Rodney or wants to build, create, deploy, update, view, list, or delete a
  micro-app — e.g. "ask Rodney to build me a dashboard", "have Rodney make a
  mockup", "make me an app", "update my app".
---

# Rodney — personal micro-app builder

Rodney deploys a self-contained **static HTML app** to a live URL. You describe
what you want; you build the HTML and call Rodney's MCP tools to deploy it. No
local setup, build step or hosting is needed. When the user wants data that
persists or is shared between visitors, you can also give the app a **backend**
(a "full-stack" app) — see [Full-stack apps](#full-stack-apps).

> **Status: early.** Anyone with a Google account can sign in, and the service may change or be
> shut down without notice.

## Configuration

- **Site URL:** `https://ptkinvent.com`
- **Sign-in:** the user signs in with Google when they connect Rodney. Every tool call
  then acts as that person, who becomes the author of the apps they create. Authors
  appear as an anonymous handle (like `u-3f9a1c2b7e`), never an email address. Tools take
  no password or identity argument. Each person can have up to 50 apps. If a call fails with an authorization error, ask the
  user to reconnect the Rodney connector (Customize → Connectors).

An app named `team-standup` lives at:

- App: `https://ptkinvent.com/team-standup/`
- Admin: `https://ptkinvent.com/team-standup/admin/`

The tools return these full URLs; show them to the user as given.

## If the Rodney tools are not available

If the Rodney connector isn't connected, don't attempt tool calls. Tell the user
Rodney isn't connected, and offer to build the app as a Claude artifact instead
(no shareable URL, lives only in the chat).

## Tools

| Tool                    | What it does                                                                                        |
| ----------------------- | --------------------------------------------------------------------------------------------------- |
| `create-app`            | Deploy a **new** app. Takes `name`, optional `description`, and `html` **or** `files`. Add `fullStack: true` for a backend. |
| `get-app`               | Metadata (author, timestamps, `appType`) and the list of file paths. Does not return file contents. |
| `read-file`             | Read one file's content. **Always do this before modifying a file.**                                |
| `create-or-update-file` | Create or overwrite a single file in an existing app. Other files are untouched.                    |
| `delete-app`            | Permanently delete an app and its data. Cannot be undone — confirm with the user first.             |
| `list-my-apps`          | List the apps you created, with their URLs and whether each is static or full-stack.                |
| `update-app-settings`   | Give an existing app a backend (`fullStack: true`) or switch it off (the data is kept).             |
| `read-app-data`         | Look at the data a full-stack app has stored (summary, or up to 50 records of one collection).      |
| `clear-app-data`        | **Permanently erase** an app's stored data (one collection, or all). Confirm with the user first.   |

Every tool except `list-my-apps` takes the app's `name`.

### `create-app`

- **`name`** — a slug: 1–40 chars, lowercase letters, numbers and dashes, not
  starting or ending with a dash (e.g. `budget-tracker`). Names are unique;
  reusing one returns an error, so pick another. Reserved: `mcp`, `admin`,
  `assets`, `api`, `static`, `privacy`, `terms`.
- **`html`** — the complete `index.html`, for single-file apps.
- **`files`** — `[{path, content}, ...]` for multi-file apps; must include
  `index.html`. Use **either** `html` **or** `files`, never both.
- **`fullStack`** *(optional, default `false`)* — `true` gives the app a shared,
  persistent data API. Use it only as described in [Full-stack apps](#full-stack-apps).
- **`collectionName`** *(optional, full-stack only, default `records`)* — the
  primary collection, e.g. `tasks`. **`primaryField`** *(default `value`)* — the
  main display field, e.g. `title`. **`template`** — only `crud` exists. All three
  are ignored for static apps.

File paths are relative (`css/style.css`): no leading slash, no `..`, no
dotfiles, only letters, numbers, `.`, `-`, `_`, `/`. Max 4 MB per file.
**Text files only** — there is no binary upload, so use inline SVG or `data:`
URIs for images.

### Updating an app

1. `get-app` to see the file list.
2. `read-file` for each file you'll change.
3. `create-or-update-file` once per changed file.

Only the app's **author** can update or delete it, or change its settings and data.
Anyone signed in can read an app's files by name, but `list-my-apps` shows only your own.

## Full-stack apps

By default an app is static and its state lives in the user's browser
(`localStorage`), so it disappears with the browser and is never shared. Build a
**full-stack** app (`fullStack: true`) **only when the user asks for one, or clearly
wants data that persists or is shared between people** — for example "a todo list that
keeps my items", "a tracker my partner and I both edit", "a poll that counts votes". If
it's unclear whether the data should be shared or just stay in one browser, ask once. A
person's own single-device notes don't need a backend.

A full-stack app gets a data API on the same origin. The page calls it with
`fetch()` using an **absolute path** (this is the one place absolute paths are right):

| Operation | Request | Returns |
| --------- | ------- | ------- |
| List      | `GET /api/full-stack/<app>/data/<collection>` | `{ collection, records }` |
| Create    | `POST …/data/<collection>` body `{ data: { …fields } }` | `201 { collection, record, records }` |
| Update    | `PATCH …/data/<collection>/<id>` body `{ data: { …fields } }` | `{ collection, record, records }` — merges fields; add `replace: true` to overwrite |
| Increment | `PATCH …/data/<collection>/<id>` body `{ increment: { field: n } }` | same — atomic, for counters, tallies and votes |
| Delete    | `DELETE …/data/<collection>/<id>` | `204` |

A record is `{ id, data, createdAt, updatedAt }`. **The app's own fields live under
`record.data`** (e.g. `record.data.title`), not on the record. Collections come into
being on first write; pick names like `tasks` (lowercase letters, digits, `-`, `_`).

```js
const API = "/api/full-stack/my-app/data/tasks";
const json = { "Content-Type": "application/json" };
const load = async () => (await (await fetch(API)).json()).records;
const add = async (fields) =>
  (await (await fetch(API, { method: "POST", headers: json, body: JSON.stringify({ data: fields }) })).json()).record;
const change = (id, fields) =>
  fetch(`${API}/${id}`, { method: "PATCH", headers: json, body: JSON.stringify({ data: fields }) });
const bump = (id, field, n = 1) =>
  fetch(`${API}/${id}`, { method: "PATCH", headers: json, body: JSON.stringify({ increment: { [field]: n } }) });
const remove = (id) => fetch(`${API}/${id}`, { method: "DELETE", headers: json });
```

Rules for the page you write:

- Send `Content-Type: application/json` on **every** request, including `DELETE`.
- On load, call List and render from `record.data`. Check `res.ok` and show a friendly
  message on failure (`409` means a limit was reached; `503` means try again).
- **Data modelling:** shared singleton state (one counter, one setting, one toggle) is
  **one** record, created on first load and then updated in place — use `increment` for
  numbers so simultaneous clicks all count. Create a record per item only for genuine
  lists and logs (tasks, names, experiments). Never create a record per click for a counter.
- There is no live push. If several people use the app at once, re-run List every few
  seconds. Simultaneous edits to the same record are all kept (to different fields), and
  `increment` never loses a count; on a rare `503` reply just retry the request.
- Sorting, filtering and totals are done in the page, over the records List returns.

**Limits:** 1,000 records and 4 MB per collection, 20 KB per record, 10 collections per
app. Records are JSON: strings, numbers, booleans, null, lists and objects (up to 8 levels).

**The data is as public as the app.** Anyone who has the app's link can read and change its
data, so after deploying a full-stack app, **tell the user that**. Never store passwords,
secrets or sensitive personal information in it.

Later changes: use `update-app-settings` to add a backend to an existing app (then update its
files to use it) or switch it off — switching off keeps the data. `read-app-data` shows what is
stored; `clear-app-data` erases it for good, so confirm with the user first. Deleting an app
deletes its data.

## Admin page

Every app automatically gets `/<name>/admin/` — a static page showing the app's
name and author. It is generated by Rodney. **Do not write to the `admin/` path**;
it is reserved and the tool will refuse.

## Building good apps

- Make each app fully self-contained: inline CSS/JS or relative multi-file
  assets. External CDN scripts/fonts are fine but add an outside dependency.
- Use **relative** URLs for assets (`css/style.css`, not `/css/style.css`) —
  the app lives under `/<name>/`, not at the site root. (Data API calls are the
  exception: they use the absolute `/api/full-stack/…` path.)
- Apps are static by default: no server-side code. State lives in the user's
  browser (`localStorage`) and is **not shared** between viewers or devices. For
  data that persists or is shared, build a [full-stack app](#full-stack-apps).
- Make apps responsive and usable on a phone.

## Intake

Don't over-ask. If the user's request is clear enough to build something
sensible, pick a slug, confirm in one line what you're building, and deploy. Ask
a single follow-up question only when the request is too vague to build
anything meaningful ("What should it do?").

## After deploying

Share both links from the tool result: the **live app** and the **admin
page**. Mention how to change it later ("just ask me to update it").

## Responsible use

Apps are not connected to any real financial, pricing or compliance systems.
For anything that looks like financial calculations, personal data collection
or advice, build it as asked, then add one short note after deploying that the
numbers are illustrative and the app shouldn't store sensitive personal data
(SSN, account numbers, etc.). Full-stack data is readable and editable by anyone
with the link, so never use it for sensitive or personal information.

## Current limitations

- **Anyone with an app's URL can view it** — there is no viewer auth. Don't put
  secrets in an app. (Sign-in only protects the tools that create, change and
  delete apps.)
- Apps are static unless made full-stack; a backend means shared, **public-by-link** data
  with the limits above — no per-user data, no private data, no live push.
- No server-side code, scheduled jobs, email, or calls to AI models.
- Text files only; 4 MB per file.
- Names are unique and are first-come-first-served.
