# Syncro MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Syncro](https://syncromsp.com)
(SyncroMSP) REST API, the PSA + RMM platform for MSPs. It lets an AI agent read customers,
contacts, tickets with their comments, assets and invoices and, on explicit request, create
customers, contacts, tickets and invoices, update tickets, add ticket comments and email
invoices.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Syncro-Subdomain` | Yes | string | none | account name (`yourcompany`, `yourcompany.syncromsp.com` or its URL) | The `yourcompany` in `https://yourcompany.syncromsp.com` | `yourcompany` |
| `X-Syncro-API-Key` | Yes | string | none | - | Syncro API token (a leading `Bearer ` is accepted and ignored) | `your-syncro-api-key` |

Where to get them:

- In Syncro open **Admin -> API Tokens** and create a token with the permissions the agent
  needs: view rights on Customers, Contacts, Tickets, Assets and Invoices for the read tools;
  create / edit rights only for the areas it should change.
- The subdomain is the first part of the address you log in to Syncro with.

How the key is sent to Syncro: as `Authorization: Bearer <token>` to
`https://{subdomain}.syncromsp.com/api/v1/...`, never as a query parameter, so it does not
appear in URLs or access logs. Values that look like unresolved templates (`${...}`) are
treated as missing.

A request to `/mcp` without both headers gets HTTP 401 listing them; a subdomain that is not a
plain account name gets HTTP 400.

## Tools

All tools return a JSON string. Syncro pages its lists with a fixed size per list
(customers 100, tickets 50, contacts 50, invoices 25, assets 25; the response's `meta.per_page`
is used when it differs) and, except for invoices, does not let the caller choose it. List
tools take `per_page` (1-100, default: Syncro's own size) and either `page` or `offset`, and
map that window onto Syncro pages, so any size works. Results carry
`{count, total_entries, total_pages, page, per_page, offset, has_more, next_page, next_offset}`;
follow `next_page` (or `next_offset`) until `has_more` is false - no record is skipped or
repeated, also when a large page is cut to fit the response size limit (then only
`next_offset` is returned). A few filters the Syncro API does not offer are applied by this
server over up to 5 Syncro pages; those results report `scanned_pages`, `has_more` and
`next_page` (a Syncro page number) instead of totals, and return every match on the pages read.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `syncro_navigate` | Describe the tools of one area (changes nothing) | **domain** (`customers`, `tickets`, `assets`, `contacts`, `invoices`) |
| `syncro_status` | Check the connection: subdomain, API user and permissions | - |
| `syncro_customers_list` | List customers with contact details | query, business_name, email, phone, include_disabled, page, per_page, offset |
| `syncro_customers_get` | One customer with addresses, contacts and properties | **customer_id** |
| `syncro_customers_search` | Find customers by name, business, email or phone | **query**, limit |
| `syncro_customers_create` | Create a customer (**write**) | business_name, firstname, lastname, email, phone, mobile, address, address_2, city, state, zip, notes |
| `syncro_tickets_list` | List tickets | customer_id, contact_id, status, user_id, problem_type*, resolved*, query, since, page, per_page, offset |
| `syncro_tickets_get` | One ticket with its comments | **ticket_id** |
| `syncro_tickets_create` | Create a ticket, optionally with a first comment (**write**) | **customer_id**, **subject**, problem_type, status, contact_id, user_id, due_date, comment_body, comment_subject |
| `syncro_tickets_update` | Change subject, problem type, status, technician or due date (**write**) | **ticket_id**, subject, problem_type, status, user_id, due_date |
| `syncro_tickets_add_comment` | Add a comment; public comments may email the customer (**write**) | **ticket_id**, **body**, subject, hidden, do_not_email |
| `syncro_assets_list` | List assets (devices) | customer_id, asset_type*, asset_serial*, query, page, per_page, offset |
| `syncro_assets_get` | One asset with type, serial and RMM properties | **asset_id** |
| `syncro_assets_search` | Find assets by name or exact serial | query, asset_serial, customer_id, limit |
| `syncro_contacts_list` | List contacts | customer_id, query*, page, per_page, offset |
| `syncro_contacts_get` | One contact | **contact_id** |
| `syncro_contacts_create` | Add a contact to a customer (**write**) | **customer_id**, **name**, email, phone, mobile, address1, address2, city, state, zip, notes |
| `syncro_invoices_list` | List invoices | customer_id*, status (`paid`, `unpaid`, `partial`*), since_date, query*, page, per_page, offset |
| `syncro_invoices_get` | One invoice with line items, totals and balance | **invoice_id** |
| `syncro_invoices_create` | Create an empty invoice numbered by Syncro (**write**) | **customer_id**, date (default today), due_date, notes |
| `syncro_invoices_email` | Email an invoice to its customer (**write**) | **invoice_id**, cc_emails, subject, message (accepted, ignored) |

\* applied by this server, as described above.

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Syncro returned HTTP 401 for me: the API key was rejected ...", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Syncro allows 180 requests per minute per source IP
address, so every account served from the same egress IP shares that budget; reads are
retried with backoff on 429 (honouring `Retry-After`), 5xx and network errors, writes only on
429.

## Run locally

```bash
uv sync
uv run python -m syncro_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t syncro-mcp:dev .
docker run --rm -p 8080:8080 syncro-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Syncro-Subdomain: yourcompany" \
  -H "X-Syncro-API-Key: your-syncro-api-key" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# open tickets of one customer
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Syncro-Subdomain: yourcompany" \
  -H "X-Syncro-API-Key: your-syncro-api-key" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"syncro_tickets_list","arguments":{"customer_id":123,"resolved":false}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Built on the published API reference.** Every call follows Syncro's OpenAPI document
  (`api-docs.syncromsp.com`). Where the older version sent filters that endpoint does not
  have, they are now mapped to the documented ones (`since` -> `since_updated_at`, invoice
  `paid` / `unpaid`) or applied by this server and flagged with `scanned_pages`.
- **Tool count (21).** Tool names and required parameters are unchanged.
- **Invoice states.** Syncro exposes paid / unpaid amounts, not draft / sent / viewed / void
  states; those values now return a clear `invalid_argument` instead of being ignored.
- **Accepted but ignored.** `syncro_invoices_email` still takes `cc_emails`, `subject` and
  `message` for compatibility, but does not send them: `POST /invoices/{id}/email` has no
  request body (it uses the account's invoice email settings); the result lists them under
  `ignored_params`.
- **Write tools.** The 7 tools marked **write** change records or contact the customer; their
  descriptions tell the agent to use them only on explicit request. There are no delete tools.
- **No interactive prompts.** Older versions asked the user for a date range before an
  unfiltered ticket list, which a stateless gateway cannot relay; lists now page instead.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
