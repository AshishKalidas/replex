# Redaction

Cassettes are committed to your repository, so they must never contain secrets. RepleX redacts
every event **before** it is written; an unredacted value never touches disk, not even in a
temporary file.

## What is redacted by default

**Headers and query parameters**, replaced outright with `[REDACTED]`:

- `authorization`, `proxy-authorization`, `x-api-key`, `api-key`, `cookie`, `set-cookie`;
- any name matching `*key*`, `*token*` or `*secret*` (case-insensitive), e.g.
  `anthropic-api-key`, `x-auth-token`, `?access_token=...`.

**Text patterns**, in every string of request bodies, response bodies, streamed events, URLs,
tool arguments, tool results and tool error messages:

| Kind | Matches |
|---|---|
| `BEARER` | `Bearer <token>` (the token part) |
| `API_KEY` | Provider-style keys: `sk-ant-…`, `sk-…`/`sk-proj-…`, AWS `AKIA…`, GitHub `ghp_…`, Slack `xox…-`, Google `AIza…` |
| `EMAIL` | Email addresses |
| `PHONE` | International (`+44 20 7946 0958`) and North American (`(555) 123-4567`, `555-123-4567`) phone numbers |

## Stable tokens

A matched value becomes `[REDACTED_<KIND>_<n>]`, numbered per cassette. The same value always
becomes the same token within a cassette, so a conversation stays internally consistent: the
email the customer gave in turn one is the same `[REDACTED_EMAIL_1]` the agent repeats in turn
three.

Redaction also runs **before the replay match key is computed**. During replay the live request
is redacted the same way and compared with the recording, so a request that contains a secret
still matches its redacted recording.

Note that replayed values are the redacted ones: in replay a tool result that contained an email
returns `[REDACTED_EMAIL_1]`. Assert on behaviour, not on personal data.

## Custom rules

```toml
[tool.replex]
# Regexes. A bare string gives [REDACTED_CUSTOM_n]; a table names the token kind.
redact_patterns = [
  "INT-\\d{8}",
  { name = "ticket", pattern = "TCK-\\d{6}" },     # -> [REDACTED_TICKET_1]
]
# Dotted JSON paths whose whole value is replaced ([REDACTED_FIELD_n]). `*` matches any key
# or list index. Applied to request and response bodies, stream events and tool values.
redact_paths = [
  "metadata.user_id",
  "messages.*.content.*.source.data",              # e.g. uploaded images
  "customer.address",
]
```

Invalid regexes are rejected with a `ConfigError` when the configuration loads. Custom
patterns add to the defaults; they never replace them.

## Checking committed cassettes

```bash
replex redact --check tests/cassettes
```

Scans every `*.replex.json` under the directory (or one file) with the default and configured
rules. It reports any sensitive header whose value is not `[REDACTED]` and any string that
still matches a pattern, with a masked excerpt, and exits 1:

```text
2 possible leak(s)
 file                        location                              kind     excerpt
 cassettes/refund_request…   events[0].request.headers.x-api-key   HEADER   sk-a…
 cassettes/refund_request…   events[0].request.headers.x-api-key   API_KEY  sk-a…
```

With nothing to report:

```text
ok: 4 cassette(s) in tests/cassettes, nothing unredacted
```

`--json` prints the findings as a list for tooling. Run the check in CI on every push (see
[CI.md](CI.md)); it catches cassettes edited by hand or recorded with an older configuration.

## Guarantees and tests

- Release gate: a test records with a fake `sk-ant-…` key in the `x-api-key` header and an
  email address in the conversation, then asserts that neither appears anywhere in the written
  cassette. Further tests cover every sensitive header name, query parameters and custom rules.
- Property tests (`hypothesis`) check that generated secrets never survive redaction, that the
  same secret always gets the same token, and that redaction is idempotent.
- `replex redact --check` runs in CI on the example's committed cassettes.

## What redaction cannot know

The default patterns cover credentials and common contact data, not every kind of personal
data. Names, street addresses and account numbers in free text are not detected. If your agent
handles such data, add `redact_paths` for the fields that carry it, or record against
synthetic fixtures.
