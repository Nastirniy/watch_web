# events_load: load generator for the stream of events

A load generator for `GET /api/v1/events/stream` (API.md, "Real-time events"; plan Task 14). Go, standard library
only. It simulates browsers: one browser is one account and one `http.Transport` (its own TLS connection and client
certificate), and its K streams (tabs) share that transport, multiplexed over HTTP/2 when nginx offers h2 (ALPN; plain
`http://` stays HTTP/1.1, one connection per stream).

A browser logs in when it opens its first tab (so tokens expire spread as the ramp), then opens its K tabs at their
places in the ramp. Each stream behaves as the web kit's starter (`api-contract/web-kit/src/api/events.ts`):

- parses the SSE blocks (`id:` / `event:` / `data:` / `retry:`, a block ends at an empty line), keeps the position
  of every block and sends it back as `Last-Event-ID`; skips an event identity already seen;
- reconnects after the server's `retry` when the stream ends; after a network error or a `429` / `502` / `503` /
  `504` waits a random time between `retry` and `min(60 s, retry x 2^n)`, at least `Retry-After` (plus up to 20%);
  watchdogs: 120 s for the answer, 65 s without a byte;
- `close token_expired`: one refresh per browser (single flight, `X-Activity: background`), then reconnects;
  `close session_ended`: logs in again (counted, the position is dropped as after any new login); `close restart`:
  waits the retry it carried; `204`: stops; `401`: refresh, else login, then backs off (never a reconnect loop);
- `reload`: counted, one refetch (`GET /auth/me`);
- every domain event: one REST GET after a random wait below `refetch_within_ms`, with `X-Activity: background`,
  chosen by type as a page would (`scope.changed` -> `GET /posts/`, `user.changed` -> `GET /users/{id}` or
  `GET /auth/me` for yourself, `role.changed` -> `GET /roles/{id}`, `dictionary.changed` -> the list it names,
  `certificate.changed`, `session.changed`, `move.changed`, `security.certificate_mismatch` likewise). Paths follow
  `app/backend/enums/routes.py` (constants at the top of `main.go`).

Alongside:

- **Emitter** (an administrator account, open loop at `-emit-rate`): `post` archives / restores posts it created
  (`scope.changed` at the post), `tag` renames a tag it created (`dictionary.changed` tags, to `tag.view` holders),
  `guide` renames a guide it created (`dictionary.changed` guides, every stream), `user` changes the alias of a load
  user (`user.changed`, to the user themself and `user.view` holders). It records when each write's answer arrived.
  The **lag** of an event at a stream is its receipt time minus that time (negative is counted as 0 and as
  `event_before_response`). Events carry no write id, so an event is bound to the write of its resource (the post id,
  the dictionary, the subject user) that waits longest; one write per resource is in flight at a time, and a
  conflated key (a dictionary, a user: the server keeps only the latest queued event of one, API.md "The
  administration") waits until its event reached some stream before the next write of that key. Someone else writing
  the same tag or guide during the run would confuse the binding (`event_unmatched` counts what nothing explains).
- **Deliveries** (`-expect`): for the emitter kinds named there, every stream that had connected once before the write
  must receive it within `-deliver-within` (30 s): live or by replay. `missed` counts the streams that did not;
  `covered_by_later` those of a conflated key that received a later write's event instead (by design);
  `excused_after_reset` those that lost their position meanwhile (`reload`, a new login). `guide` and `user` are always
  computable; `tag` and `post` only when every account sees them (accounts made by `prepare`: no home post, the base
  role at the root, every built-in type holds `tag.view` and `post.view`).
- **REST load**: open loop at `-rest-rate` (requests start on schedule, whatever the answers do; above
  `-rest-max-inflight` one is dropped and counted), a fixed mix (`-rest-mix`, default: posts, observations,
  `/auth/me`, tags, categories, guides), on the browsers' tokens and transports, without `X-Activity` (user activity:
  it keeps the sessions alive). p50 / p95 / p99 and status counts, in total and per path.
- **Abort** (`-abort-at`): at that second every connection of every browser is closed at once (as a network failure);
  the streams back off and reconnect with their positions. Reports the time until every stream received `ready`
  again, and the distribution per stream (`latency.abort.recovery`). Errors in the 5 s after the abort are counted
  apart (`*_abort`).

Output: a status line every `-status-every` (streams open, events/s, refetch/s, REST/s and p95, emit/s, lag p95,
reconnects, relogins, errors, heap), and a JSON summary in `-out` (`streams`, `deliveries`, `abort`, `counters` and
`latency` grouped by section, the generator's own memory). Per stream it keeps a few hundred bytes plus a 1 KB ring of
seen identities; pending refetches sit in a timing wheel (16 bytes each), histograms are fixed 20 KB buckets. Measured:
2,000 streams over HTTP/2 (200 browsers x 10) with events, refetches and an abort: Go heap at most 60 MB, the whole
process 135 MB; 115 MB heap / 321 MB process when the server was overloaded and 10,000 refetches were pending.

Summary fields worth reading first: `streams` (all connected, `all_open_after_s`), `latency.lag` (live deliveries)
and `latency.lag_replay` (events that came in the replay right after a reconnect: their lag is the time the stream
was away), `deliveries` (`missed` within the window, `missed_then_late` the part of it that came later by replay),
`abort`, `latency.rest` and `counters.rest_status`, `counters.stream_status`, `counters.auth`.

## Build

    go vet ./... && go build -o events_load .            # Go 1.24+, no modules to download
    docker build -t events-load tests/docker/events_load  # from the repository root; includes openssl

## Accounts

`-accounts DIR` holds one subdirectory per account (sorted by name; browser i uses account i, `-reuse-accounts`
cycles them when there are fewer, but nginx allows 32 streams per client certificate). Browsers of one account are
one user's devices: each logs in with its own session, and a `user` write expects its event on the streams of all of
them:

| File | Mode | Content |
|---|---|---|
| `cert.pem` | both | the client certificate (PEM) |
| `key.pem` | mtls | its private key (PEM, unencrypted: PKCS#8, PKCS#1 or EC) |
| `login.json` | plain, optional | the development login body (`certificate_cn`, `certificate_serial`, `certificate_fingerprint`); without it the body is computed from `cert.pem` (CN, serial in hex, SHA-1 fingerprint: what nginx passes) |

`-admin DIR` is the emitter's account in the same form: an account holding `post.*`, `tag.*`, `guide.update` /
`guide.archive` and `user.update` (the `system_administrator` type). In plain mode without `-admin` the seeded
development administrator of the e2e stack is used.

### Making them: `prepare`

`events_load prepare` logs in as `-admin` and creates `-count` users (`<prefix>_<stamp>_<n>`, type `-user-type`,
default `operator`, no home post unless `-post-id`) through `POST /users/`:

- plain mode: it generates a self-signed certificate per user, uploads it with the user (the development login
  matches it) and writes `cert.pem` + `key.pem`;
- mtls mode: it issues a certificate per user (`POST /users/{id}/certificates`, PKI must be configured): the API makes
  the key and answers a `.p12` and its password once; `prepare` writes `client.p12` + `password.txt`. Go's standard
  library has no PKCS#12, so turn them into PEM with openssl:

      sh p12_to_pem.sh accounts                                           # openssl on the host
      docker run --rm -v "$PWD/accounts:/accounts" --entrypoint p12_to_pem.sh events-load /accounts

  (`openssl pkcs12 -in client.p12 -passin file:password.txt -clcerts -nokeys | openssl x509 -out cert.pem`, and
  `-nocerts -nodes | openssl pkey -out key.pem`; both encryptions the API writes, AES-256 and legacy 3DES, work with
  OpenSSL 3.) The keys are unencrypted: keep the directory private and delete it after the test (the users and
  certificates stay in the database: archive or revoke them through the API).

The administrator's own `.p12` converts the same way (a directory with `client.p12` + `password.txt`).

## Run

### Plain mode against the local e2e stack (HTTP/1.1)

    docker build -t watch-web-evload .
    E2E_NGINX_PORT=18189 E2E_DIRECT_PORT=18190 docker compose -p watch-web-evload \
        -f tests/docker/compose.test.yml -f tests/docker/events_load/compose.events.yml up -d nginx media-worker
    cd tests/docker/events_load && go build -o events_load .
    ./events_load prepare -base http://127.0.0.1:18189 -out accounts -count 20
    ./events_load run -base http://127.0.0.1:18189 -accounts accounts -browsers 20 -streams 3 \
        -ramp 20s -hold 160s -emit-rate 2 -rest-rate 10 -abort-at 90s -expect guide,user,tag,post -out summary.json
    docker compose -p watch-web-evload -f tests/docker/compose.test.yml -f tests/docker/events_load/compose.events.yml down -v

`compose.events.yml` (an override of `compose.test.yml`) gives the stack its own image tag and a 2-minute access
token (`EVLOAD_TOKEN_MINUTES`), so `close token_expired` and the background refresh happen during a short run. The
first start of MySQL and the migrations take several minutes on Docker Desktop: run `up` again if it gives up.

### Local run over HTTP/2 with client certificates

The profile `tls` adds `nginx-tls`: the same locations (`nginx-app.conf`, `nginx-proxy.conf`) behind TLS with
`http2 on` and `ssl_verify_client optional_no_ca` (`nginx-tls.test.conf`), so the self-signed certificates of a
plain `prepare` act as client certificates and the per-certificate limits of the events location apply. The API stays
in development, so the login is still JSON (`-mode mtls -login json`):

    mkdir tls && docker run --rm -v "$PWD/tls:/out" --entrypoint openssl events-load req -x509 -newkey ec \
        -pkeyopt ec_paramgen_curve:P-256 -nodes -days 7 -subj /CN=localhost \
        -addext subjectAltName=DNS:localhost,IP:127.0.0.1 -keyout /out/server.key -out /out/server.pem
    EVLOAD_TLS_DIR=$PWD/tls docker compose -p watch-web-evload -f ../compose.test.yml -f compose.events.yml \
        --profile tls up -d nginx-tls
    ./events_load run -base https://127.0.0.1:18443 -ca tls/server.pem -mode mtls -login json -accounts accounts \
        -browsers 20 -streams 3 -ramp 20s -hold 160s -emit-rate 2 -rest-rate 10 -abort-at 90s \
        -expect guide,user,tag,post -out summary-h2.json

### mTLS mode on the test server

Accounts made with `prepare -mode mtls` (and `p12_to_pem.sh`), nginx's public server certificate (or `-ca` with
its chain), the login by certificate (`-login cert`, the default in mtls mode: an empty `POST /auth/login`, nginx
passes `X-SSL-Client-*`):

    ./events_load prepare -base https://watch.example -mode mtls -admin admin -out accounts -count 200
    sh p12_to_pem.sh accounts
    ./events_load run -base https://watch.example -mode mtls -admin admin -accounts accounts \
        -browsers 200 -streams 10 -ramp 120s -hold 10m -emit-rate 0 -rest-rate 0 -out t1.json          # test 1
    ./events_load run -base https://watch.example -mode mtls -admin admin -accounts accounts \
        -browsers 200 -streams 10 -ramp 120s -hold 30m -emit-rate 10 -rest-rate 50 \
        -expect guide,user,tag,post -out t2.json                                                      # test 2
    ./events_load run ... -abort-at 600s                                                              # recovery

In a container (the image has the binary and openssl):

    docker run --rm --network host --ulimit nofile=65536 -v "$PWD/load:/work" events-load run \
        -base https://watch.example -mode mtls -admin /work/admin -accounts /work/accounts ... -out /work/t2.json

`-ca` is needed only when the server certificate does not chain to the system roots of the machine (or image)
running the generator; `-insecure` skips the check (local only).

## Flags

`events_load run -h` and `events_load prepare -h` list them all. The main ones:

| Flag | Default | Meaning |
|---|---|---|
| `-base` | `http://127.0.0.1:18089` | nginx |
| `-ca`, `-insecure` | | the server certificate's CA file; no verification |
| `-mode` | `plain` | `plain` (no client certificate) or `mtls` |
| `-login` | json in plain, cert in mtls | `json`: body from the account (development); `cert`: empty body |
| `-accounts`, `-admin`, `-reuse-accounts` | `accounts` | account directories |
| `-browsers`, `-streams` | 20, 3 | N browsers x K streams |
| `-ramp`, `-hold` | 30s, 2m | streams open evenly over the ramp, then hold |
| `-emit-rate`, `-emit-mix`, `-emit-posts` | 2, `post=4,tag=2,guide=1,user=2`, 10 | emitter (runs from the end of the ramp to `-deliver-within` before the end) |
| `-expect`, `-deliver-within` | `guide,user`, 30s | delivery checks |
| `-rest-rate`, `-rest-mix`, `-rest-max-inflight` | 10, see above, 2000 | open-loop REST load |
| `-refetch-ratio`, `-refetch-max-inflight` | 1, 5000 | share of events refetched (a page refetches only what is on screen) |
| `-abort-at` | 0 | drop every connection at this second |
| `-out`, `-status-every` | `events_load_summary.json`, 10s | output |
| `-connect-timeout`, `-silence`, `-request-timeout` | 120s, 65s, 60s | watchdogs |
| `-cleanup` | true | archive the emitter's posts, tag and guide at the end |

## Limits

- Lag is measured on the generator's clock between the write's answer and the event's receipt: it includes the write's
  commit-to-answer time on the way back (small) and the generator's own scheduling. Run it on another host than the
  server when the server's CPU is what is measured; with thousands of streams watch the generator's CPU too.
- The binding needs the emitter to be the only writer of its tag and guide; foreign events of other resources are
  only counted (`event_unmatched`), refetched as any event.
- `tag` / `post` expectations assume every account sees them (see Deliveries); with other accounts use the default
  `-expect guide,user`.
- Plain `http://` gives HTTP/1.1: one TCP connection per stream (as a browser without h2, which the web client refuses
  to do). Go's standard library speaks h2 only over TLS, hence the `tls` profile for a local h2 check.
- `-abort-at` closes the TCP connections under the transports; the REST requests in flight on them fail too (counted
  in `*_abort`). With 2,000 streams the reconnect goes through nginx's delay of connects (40/s overall): expect about
  50 s until all are back, and with a short access token (the local 2-minute one) some connects that waited that long
  answer 401 `invalid_token` and back off once more.
- `-refetch-ratio 1` with every-stream events (guides, posts seen by everyone) is the worst case of plan tests 4 and
  5: at 2,000 streams every event asks the API for 2,000 GETs spread over at most `refetch_window_max_ms` (60 s),
  and the waves of overlapping events add up. Model what a page shows with a lower ratio for the other tests.
- A refresh whose answer is lost (network error) is never sent again: the token may be used already, so the browser
  logs in instead (as API.md asks); a 5xx refresh keeps the token and is retried after a back-off.
- Not covered: slow readers (plan test 13), an api restart (test 3: restart the container during a run and read
  `abort`-like recovery from the status line), late commits (test 6). Those need actions on the server side.
