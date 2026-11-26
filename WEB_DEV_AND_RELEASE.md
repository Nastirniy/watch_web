# Web team: the backend on your machine, and publishing to production

For the developers of the web client. Part 1 runs the whole backend locally (API, media worker, imgproxy, nginx,
MySQL) in Docker, so the web client is developed and tested against exactly the release it will meet in production.
Part 2 is the checklist for publishing a version to the production server, backend and web client together.
Related: WEB_TOOLING.md (types from the contract, translations, the dev server proxy), API.md (the contract in
words), RELEASE.md (the operator's runbook for backend releases).

## Part 1. The backend on your machine

### What you get

`tests/docker/compose.test.yml` starts the production pipeline on one machine (Docker Desktop on Windows or macOS,
Docker on Linux): the API in development mode, the media worker, imgproxy, nginx with the production locations
(`nginx-app.conf`, `nginx-proxy.conf`: thumbnails, video streaming, uploads, the event stream behave as in
production), and MySQL. Differences from production: plain http, no client certificates (the login takes the
certificate fields in a JSON body), everything on `127.0.0.1:18089`.

### What you need

- Docker Desktop (Windows: WSL 2 backend), about 6 GB free disk.
- The backend files of the release you test: a checkout of the backend repository at that commit, or the files
  archive the backend team gives you (`watch-web-<tag>-files.tar.gz`, unpack it into an empty folder).
- The backend image of that release, one of:
  - **the release archive** from the backend team (`watch-web-<tag>.tgz` and `watch-web-<tag>.tgz.sha256`, the
    same files that go to the server), or
  - **a build** from the checkout (needs internet, about 10 minutes the first time).

The tag looks like `2026.11.24-2a8c1e7` (date and commit). Ask the backend team which tag production runs or will
run; test against that one.

### Get the image

From the release archive (in Git Bash, WSL or a Linux / macOS shell, in the folder with both files):

```
sha256sum -c watch-web-<tag>.tgz.sha256        # must print: watch-web-<tag>.tgz: OK
gunzip -c watch-web-<tag>.tgz | docker load     # prints: Loaded image: watch-web:<tag> (and imgproxy)
```

Or build it from the checkout:

```
docker build -t watch-web:<tag> .               # or -t watch-web:e2e and leave WATCH_WEB_TAG out below
```

`docker image ls watch-web` lists what you have.

### Start, check, stop

From the backend folder. Bash / Git Bash / macOS:

```
export WATCH_WEB_TAG=<tag>
docker compose -f tests/docker/compose.test.yml up -d
docker compose -f tests/docker/compose.test.yml ps          # wait until api and imgproxy show "healthy"
curl -s http://127.0.0.1:18089/health                        # "version": "<tag>", "status": "healthy"
```

Windows PowerShell: `$env:WATCH_WEB_TAG = "<tag>"` instead of `export`, the rest is the same.

The first start creates the database and runs every migration: about 3 minutes on Docker Desktop (measured
2026-11-26), `ps` shows the api `starting` meanwhile. If `up` stops with `dependency failed to start: container
watch-web-e2e-mysql-1 is unhealthy` on the very first run (MySQL initialises its data directory slowly), run the same
`up -d` again. Without `WATCH_WEB_TAG` the stack runs `watch-web:e2e`.

- Logs: `docker compose -f tests/docker/compose.test.yml logs -f api media-worker`
- Stop, keep the data: `docker compose -f tests/docker/compose.test.yml down`
- Stop and erase the data (database, uploaded media): `docker compose -f tests/docker/compose.test.yml down -v`
- API documentation of the running release: http://127.0.0.1:18089/api/v1/docs

Port taken (Windows reserves ranges below 11000; another program on 18089): `E2E_NGINX_PORT=18189` before `up`,
and the same port in your dev server proxy.

### Point the web dev server at it

As WEB_TOOLING.md section 8 says, with the target `http://127.0.0.1:18089` (the stack's nginx, not the API
directly, so thumbnails, streaming and events go the production way):

```ts
// vite.config.ts
export default defineConfig({
  server: {
    proxy: Object.fromEntries(
      ["/api", "/problems", "/health", "/media-images"]
        .map((path) => [path, { target: "http://127.0.0.1:18089", changeOrigin: true }]),
    ),
  },
});
```

These are every path the backend publishes: the API (video streams and uploads included, under `/api/`), the
problem pages, `/health`, and `/media-images/` (the signed thumbnail and preview URLs the API hands out, relative
because `storage.imgproxy.public_base_url` is empty). Call everything with relative URLs.

### Log in

Development mode takes the certificate fields from the login body. The seeded administrator:

```
POST /api/v1/auth/login
{"certificate_cn": "admin@radiomonitoring.local",
 "certificate_serial": "ABC123DEF456",
 "certificate_fingerprint": "12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78"}
```

Create other users, roles, posts and units through your own administration pages or the API, as in production;
log in as such a user with the fields of a certificate registered for them (API.md, "Client certificates": upload
any self-signed certificate for the user and use its CN, serial and SHA-1 fingerprint). Issuing certificates
answers 503 here (no certificate authority in the stack); that is expected.

### Types and translations of the same release

Generate the types from the contract of the release you test, never from another branch:

```
npx openapi-typescript <backend folder>/api-contract/v1/openapi.json -o src/api/schema.d.ts --default-non-nullable false
```

(or from the running stack: `http://127.0.0.1:18089/api/v1/openapi.json`, the same document). Then `tsc` and the
translation check as WEB_TOOLING.md sections 3 to 5 describe. `CHANGELOG.md` of the backend lists what changed
for the client in each release.

### Move to a newer backend release

Load or build the new image, then start the stack with the new tag; the API migrates the database at its start
and your data stays:

```
export WATCH_WEB_TAG=<new tag>
docker compose -f tests/docker/compose.test.yml up -d
```

Going back to the older tag works as on the server: the older release runs on the newer database unless a
"contract" migration came in between; then its API logs `SCHEMA CHECK FAILED (api): ...` and stops. Locally the
fix is `down -v` (a fresh database) and `up -d` with the older tag.

### When something is wrong

| What you see | What to do |
|--------------|------------|
| `pull access denied for watch-web` or `No such image` | the image of `WATCH_WEB_TAG` is not loaded: `docker image ls watch-web`, load or build it |
| `dependency failed to start: ... is unhealthy` on the first `up` | run `up -d` again: the first MySQL start or the first migration took longer than its check waited |
| api stays `starting` / `unhealthy` | `docker compose -f tests/docker/compose.test.yml logs api`; the first start migrates for about 3 minutes |
| `SCHEMA CHECK FAILED (api)` in the api log | the database is from a newer release than the image: use that release, or `down -v` |
| 502 from nginx | the api is not up yet, or restarted: wait, read its log |
| `bind: address already in use` / port forbidden | another port: `E2E_NGINX_PORT=18189` (and `E2E_DIRECT_PORT=18190`) |
| a type error after generating | the contract changed: read CHANGELOG.md of the backend for that release |

## Part 2. Publishing to production

The backend operator follows RELEASE.md; the web team delivers the client build. Backend first, then the web
client: within API version v1 the backend only adds (new routes, new optional fields, new codes, `deprecated` on
old routes), so the web client in production keeps working on the new backend for the minutes between the two
steps.

### Before (web team and backend team together)

- [ ] The backend tag is chosen (`watch-web:<tag>`), its CHANGELOG entries read: new or changed operations,
      problem codes, events.
- [ ] The web client is generated from `api-contract/v1/openapi.json` of that tag, `tsc` passes, every new problem
      code and event has its translation and handling (WEB_TOOLING.md sections 3 to 5 and 11).
- [ ] The web client was tested locally against that tag (Part 1): log in, the changed screens, an upload, a
      thumbnail, a video, the event stream (a change in one tab shows in another).
- [ ] The web build is made: `npm ci && npm run build` (or your build command); keep the output folder as one
      archive, `watch-web-client-<version>.tar.gz`, with `index.html` at its top.
- [ ] A time with few users is agreed: the backend restart is about 30 seconds without service.

### Step 1: the backend (operator, RELEASE.md)

1. `deploy/release.sh build` on the build machine; `save` and the files archive ("Move a release to the server").
2. On the server: unpack the files, `deploy/release.sh load`, then `deploy/release.sh deploy <tag> --dry-run` and
   `deploy/release.sh deploy <tag>`.
3. Verify (RELEASE.md, "Verify"): `deploy/release.sh status`; on the server `curl -s http://127.0.0.1:8000/health`
   shows `"version": "<tag>"`.

### Step 2: the web client

The server's nginx serves the client from `/var/www/watch_web_client` (`root` of `location /` in `nginx.conf`;
`index.html` and the assets, client-side routes fall back to `index.html`). Replace the folder in one move, keeping
the previous one:

```
sudo mkdir /var/www/watch_web_client.new
sudo tar -xzf watch-web-client-<version>.tar.gz -C /var/www/watch_web_client.new
sudo chmod -R a+rX /var/www/watch_web_client.new
sudo rm -rf /var/www/watch_web_client.prev
sudo mv /var/www/watch_web_client /var/www/watch_web_client.prev
sudo mv /var/www/watch_web_client.new /var/www/watch_web_client
```

No nginx reload is needed for new files. Browsers that have the old `index.html` open load the new one at their
next navigation; hashed asset names (the default of Vite and webpack) keep old and new assets apart.

### Step 3: verify in production

- [ ] Open the site in a browser with a user certificate; the login works without a password.
- [ ] The screens changed by this release work; one upload, one thumbnail, one video plays.
- [ ] The event stream connects (the network panel shows `/api/v1/events/stream` open) and a change made in another
      tab appears.
- [ ] No error answers in the browser console beyond what you expect (each error is a problem with a `code`).

### Rolling back

- **Only the web client is wrong**: swap the folders back.
  ```
  sudo mv /var/www/watch_web_client /var/www/watch_web_client.bad
  sudo mv /var/www/watch_web_client.prev /var/www/watch_web_client
  ```
- **The backend is wrong**: if the new web client uses something only the new backend has, swap the web client back
  first (above), then the operator runs `deploy/release.sh rollback` (RELEASE.md, "Roll back"). It needs no
  database restore unless the release had a contract migration; release.sh says so before it changes anything.

Report what you saw with the request id: every API answer carries `X-Request-ID`, and an error body
(`application/problem+json`) has `code` and, for a 500, `error_id`: the backend team finds the log line by it.
