// Command events_load is a load generator for the stream of events, GET /api/v1/events/stream (API.md,
// "Real-time events"). Standard library only. README.md tells how to run it.
//
// One simulated browser = one account = one http.Transport (its own TLS connection and client certificate); its K
// streams (tabs) share it, multiplexed over HTTP/2 when the server offers h2. Each stream behaves as the web kit's
// starter (api-contract/web-kit/src/api/events.ts): it keeps the position, reconnects with Last-Event-ID after the
// server's retry, backs off on errors and Retry-After, refreshes on close token_expired, logs in again on
// session_ended, and refetches through REST after a random wait below refetch_within_ms.
//
// Subcommands:
//
//	events_load run     [flags]   the load test (default when the first argument is a flag)
//	events_load prepare [flags]   create load users through the API and write an account directory
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha1"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"hash/fnv"
	"io"
	"log"
	"math"
	"math/big"
	"math/bits"
	mrand "math/rand/v2"
	"mime/multipart"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ---------------------------------------------------------------------------------------------------------------
// Paths: APIVersionRoutes.V1 + the group's BASE + the member, exactly as app/backend/enums/routes.py declares them.
// A collection root (ROOT = "/") keeps its trailing slash: it is part of the published path.
// ---------------------------------------------------------------------------------------------------------------

const (
	pathHealth           = "/health"                       // SystemRoutes.HEALTH
	pathLogin            = "/api/v1/auth/login"            // AuthRoutes.LOGIN
	pathRefresh          = "/api/v1/auth/refresh"          // AuthRoutes.REFRESH
	pathMe               = "/api/v1/auth/me"               // AuthRoutes.ME
	pathMySessions       = "/api/v1/auth/sessions"         // AuthRoutes.SESSIONS
	pathStream           = "/api/v1/events/stream"         // EventRoutes.STREAM
	pathPosts            = "/api/v1/posts/"                // PostRoutes.ROOT
	pathPostArchive      = "/api/v1/posts/%d/archive"      // PostRoutes.POST_ARCHIVE
	pathPostRestore      = "/api/v1/posts/%d/restore"      // PostRoutes.POST_RESTORE
	pathTagCreate        = "/api/v1/tags/create"           // TagRoutes.CREATE
	pathTagsList         = "/api/v1/tags/list-all"         // TagRoutes.LIST_ALL
	pathTagByID          = "/api/v1/tags/%d"               // TagRoutes.TAG_BY_ID
	pathTagArchive       = "/api/v1/tags/%d/archive"       // TagRoutes.TAG_ARCHIVE
	pathGuides           = "/api/v1/guides/"               // GuideRoutes.ROOT
	pathGuideByID        = "/api/v1/guides/%d"             // GuideRoutes.GUIDE_BY_ID
	pathGuideArchive     = "/api/v1/guides/%d/archive"     // GuideRoutes.GUIDE_ARCHIVE
	pathUsers            = "/api/v1/users/"                // UserRoutes.ROOT
	pathUserByID         = "/api/v1/users/%d"              // UserRoutes.USER_BY_ID
	pathUserSessions     = "/api/v1/users/%d/sessions"     // UserRoutes.USER_SESSIONS
	pathUserCertificates = "/api/v1/users/%d/certificates" // UserRoutes.USER_CERTIFICATES
	pathRoleByID         = "/api/v1/roles/%d"              // RoleRoutes.ROLE_BY_ID
	pathMoveByID         = "/api/v1/moves/%d"              // MoveRoutes.MOVE_BY_ID
	pathAuditByID        = "/api/v1/audit-logs/%d"         // AuditRoutes.ENTRY_BY_ID
	pathCategoriesList   = "/api/v1/categories/list-all"   // CategoryRoutes.LIST_ALL
	pathSignalTypesList  = "/api/v1/signal-types/list-all" // SignalTypeRoutes.LIST_ALL
	pathMediaFormatsList = "/api/v1/media-formats/list-all"
	pathObservationsList = "/api/v1/observations/list-all" // ObservationRoutes.LIST_ALL
)

// The default open-loop REST mix: pages every built-in user type may read (post.view, observation.view, tag.view,
// category.view, a login)
const defaultRestMix = pathPosts + "=3," + pathObservationsList + "=3," + pathMe + "=2," + pathTagsList + "=1," +
	pathCategoriesList + "=1," + pathGuides + "=1"

// The seeded administrator of a development server (tests/docker/e2e.py): the default emitter in plain mode
var devAdminLogin = []byte(`{"certificate_cn":"admin@radiomonitoring.local","certificate_serial":"ABC123DEF456",` +
	`"certificate_fingerprint":"12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78"}`)

// ---------------------------------------------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------------------------------------------

type config struct {
	base               string
	caFile             string
	insecure           bool
	mode               string // plain | mtls
	login              string // json | cert
	accountsDir        string
	adminDir           string
	reuse              bool
	browsers           int
	streams            int
	ramp               time.Duration
	hold               time.Duration
	emitRate           float64
	emitMix            string
	emitPosts          int
	expect             string
	deliverWithin      time.Duration
	restRate           float64
	restMix            string
	restMaxInflight    int
	refetchRatio       float64
	refetchMaxInflight int
	abortAt            time.Duration
	out                string
	statusEvery        time.Duration
	connectTimeout     time.Duration
	silence            time.Duration
	requestTimeout     time.Duration
	cleanup            bool
}

func commonFlags(fs *flag.FlagSet, c *config) {
	fs.StringVar(&c.base, "base", "http://127.0.0.1:18089", "base URL of nginx (https://host for mTLS)")
	fs.StringVar(&c.caFile, "ca", "", "PEM file of the CA that signed the server certificate (default: system roots)")
	fs.BoolVar(&c.insecure, "insecure", false, "do not verify the server certificate (local tests only)")
	fs.StringVar(&c.mode, "mode", "plain", "plain: no client certificate; mtls: each account presents its cert.pem/key.pem")
	fs.StringVar(&c.login, "login", "", "json: login body from the account (development server); cert: empty body, nginx passes the client certificate (default: json in plain mode, cert in mtls mode)")
	fs.StringVar(&c.adminDir, "admin", "", "account directory of the administrator (emitter, prepare); empty in json login: the seeded development administrator")
	fs.DurationVar(&c.requestTimeout, "request-timeout", 60*time.Second, "timeout of one REST request")
}

func (c *config) finish() error {
	c.base = strings.TrimRight(c.base, "/")
	if c.mode != "plain" && c.mode != "mtls" {
		return fmt.Errorf("-mode must be plain or mtls")
	}
	if c.login == "" {
		if c.mode == "mtls" {
			c.login = "cert"
		} else {
			c.login = "json"
		}
	}
	if c.login != "json" && c.login != "cert" {
		return fmt.Errorf("-login must be json or cert")
	}
	if c.login == "cert" && c.mode != "mtls" {
		return fmt.Errorf("-login cert needs -mode mtls (nginx takes the identity from the client certificate)")
	}
	return nil
}

// ---------------------------------------------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------------------------------------------

type account struct {
	name  string
	cert  *tls.Certificate // mtls: presented to nginx
	login []byte           // json login: the request body; nil: empty body (cert login)
}

// loadAccount reads one account directory: cert.pem + key.pem (mTLS), and for a json login either login.json or the
// values of cert.pem (CN, serial, SHA-1 fingerprint: what nginx would pass)
func loadAccount(dir string, c *config) (*account, error) {
	a := &account{name: filepath.Base(dir)}
	certFile, keyFile := filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	if c.mode == "mtls" {
		pair, err := tls.LoadX509KeyPair(certFile, keyFile)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", dir, err)
		}
		a.cert = &pair
	}
	if c.login == "json" {
		if data, err := os.ReadFile(filepath.Join(dir, "login.json")); err == nil {
			a.login = data
		} else {
			certPEM, err := os.ReadFile(certFile)
			if err != nil {
				return nil, fmt.Errorf("%s: neither login.json nor cert.pem", dir)
			}
			body, err := loginBodyFromPEM(certPEM)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", dir, err)
			}
			a.login = body
		}
	}
	return a, nil
}

func loginBodyFromPEM(data []byte) ([]byte, error) {
	for {
		var block *pem.Block
		block, data = pem.Decode(data)
		if block == nil {
			return nil, errors.New("no CERTIFICATE block in cert.pem")
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, err
		}
		fp := sha1.Sum(cert.Raw)
		return json.Marshal(map[string]string{
			"certificate_cn":          cert.Subject.CommonName,
			"certificate_serial":      strings.ToLower(cert.SerialNumber.Text(16)),
			"certificate_fingerprint": hex.EncodeToString(fp[:]),
		})
	}
}

func loadAccounts(c *config) ([]*account, error) {
	entries, err := os.ReadDir(c.accountsDir)
	if err != nil {
		return nil, err
	}
	adminAbs, _ := filepath.Abs(c.adminDir)
	var out []*account
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		dir := filepath.Join(c.accountsDir, e.Name())
		if abs, _ := filepath.Abs(dir); c.adminDir != "" && abs == adminAbs {
			continue
		}
		a, err := loadAccount(dir, c)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, nil
}

func adminAccount(c *config) (*account, error) {
	if c.adminDir != "" {
		return loadAccount(c.adminDir, c)
	}
	if c.login != "json" {
		return nil, errors.New("-admin is required with -login cert")
	}
	return &account{name: "dev-admin", login: devAdminLogin}, nil
}

// ---------------------------------------------------------------------------------------------------------------
// Transport: one per browser, every connection tracked so that an abort can drop them all
// ---------------------------------------------------------------------------------------------------------------

type connTracker struct {
	mu    sync.Mutex
	conns map[*trackedConn]struct{}
	d     net.Dialer
}

type trackedConn struct {
	net.Conn
	t    *connTracker
	once sync.Once
}

func (c *trackedConn) Close() error {
	c.once.Do(func() {
		c.t.mu.Lock()
		delete(c.t.conns, c)
		c.t.mu.Unlock()
	})
	return c.Conn.Close()
}

func (t *connTracker) dial(ctx context.Context, network, addr string) (net.Conn, error) {
	c, err := t.d.DialContext(ctx, network, addr)
	if err != nil {
		return nil, err
	}
	tc := &trackedConn{Conn: c, t: t}
	t.mu.Lock()
	t.conns[tc] = struct{}{}
	t.mu.Unlock()
	return tc, nil
}

// dropAll closes every connection under the transport, as a network failure would
func (t *connTracker) dropAll() int {
	t.mu.Lock()
	list := make([]*trackedConn, 0, len(t.conns))
	for c := range t.conns {
		list = append(list, c)
	}
	t.mu.Unlock()
	for _, c := range list {
		_ = c.Close()
	}
	return len(list)
}

func newTransport(c *config, a *account, roots *x509.CertPool) (*http.Transport, *connTracker) {
	tracker := &connTracker{conns: map[*trackedConn]struct{}{}, d: net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}}
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots, InsecureSkipVerify: c.insecure}
	if a != nil && a.cert != nil {
		tlsConfig.Certificates = []tls.Certificate{*a.cert}
	}
	tr := &http.Transport{
		Proxy:               nil,
		DialContext:         tracker.dial,
		TLSClientConfig:     tlsConfig,
		ForceAttemptHTTP2:   true, // h2 when nginx offers it (ALPN); plain http stays HTTP/1.1
		MaxIdleConns:        64,
		MaxIdleConnsPerHost: 32,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 30 * time.Second,
	}
	return tr, tracker
}

func loadRoots(c *config) (*x509.CertPool, error) {
	if c.caFile == "" {
		return nil, nil
	}
	data, err := os.ReadFile(c.caFile)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(data) {
		return nil, fmt.Errorf("%s: no PEM certificate", c.caFile)
	}
	return pool, nil
}

// ---------------------------------------------------------------------------------------------------------------
// Metrics: counters and log-bucketed latency histograms (about 1.5% precision, 20 KB each, lock-free)
// ---------------------------------------------------------------------------------------------------------------

const histSize = 128 + 40*64

type hist struct {
	b   [histSize]atomic.Uint64
	n   atomic.Uint64
	sum atomic.Int64 // microseconds
	max atomic.Int64 // microseconds
}

func histIndex(us uint64) int {
	if us < 128 {
		return int(us)
	}
	e := bits.Len64(us) - 1
	m := (us >> uint(e-6)) & 63
	i := 128 + (e-7)*64 + int(m)
	if i >= histSize {
		i = histSize - 1
	}
	return i
}

func histValue(i int) float64 {
	if i < 128 {
		return float64(i)
	}
	e := (i-128)/64 + 7
	m := uint64((i - 128) % 64)
	low := float64(uint64(1)<<uint(e) | m<<uint(e-6))
	return low + float64(uint64(1)<<uint(e-6))/2
}

func (h *hist) add(d time.Duration) {
	us := d.Microseconds()
	if us < 0 {
		us = 0
	}
	h.b[histIndex(uint64(us))].Add(1)
	h.n.Add(1)
	h.sum.Add(us)
	for {
		cur := h.max.Load()
		if us <= cur || h.max.CompareAndSwap(cur, us) {
			return
		}
	}
}

// quantile in milliseconds
func (h *hist) quantile(q float64) float64 {
	total := h.n.Load()
	if total == 0 {
		return 0
	}
	target := uint64(math.Ceil(q * float64(total)))
	if target < 1 {
		target = 1
	}
	var cum uint64
	for i := range h.b {
		cum += h.b[i].Load()
		if cum >= target {
			v := histValue(i)
			if mx := float64(h.max.Load()); v > mx {
				v = mx
			}
			return v / 1000
		}
	}
	return float64(h.max.Load()) / 1000
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }

func (h *hist) summary() map[string]any {
	n := h.n.Load()
	out := map[string]any{"count": n}
	if n == 0 {
		return out
	}
	out["mean_ms"] = round1(float64(h.sum.Load()) / float64(n) / 1000)
	out["p50_ms"] = round1(h.quantile(0.50))
	out["p95_ms"] = round1(h.quantile(0.95))
	out["p99_ms"] = round1(h.quantile(0.99))
	out["max_ms"] = round1(float64(h.max.Load()) / 1000)
	return out
}

type metrics struct {
	counters sync.Map // name -> *atomic.Int64
	hists    sync.Map // name -> *hist
}

func (m *metrics) add(name string, v int64) {
	c, ok := m.counters.Load(name)
	if !ok {
		c, _ = m.counters.LoadOrStore(name, new(atomic.Int64))
	}
	c.(*atomic.Int64).Add(v)
}

func (m *metrics) inc(name string) { m.add(name, 1) }

func (m *metrics) get(name string) int64 {
	if c, ok := m.counters.Load(name); ok {
		return c.(*atomic.Int64).Load()
	}
	return 0
}

func (m *metrics) h(name string) *hist {
	h, ok := m.hists.Load(name)
	if !ok {
		h, _ = m.hists.LoadOrStore(name, new(hist))
	}
	return h.(*hist)
}

// nested groups "section.rest" names into {section: {rest: value}}
func (m *metrics) nested() (map[string]map[string]int64, map[string]map[string]any) {
	counters := map[string]map[string]int64{}
	m.counters.Range(func(k, v any) bool {
		section, rest := splitName(k.(string))
		if counters[section] == nil {
			counters[section] = map[string]int64{}
		}
		counters[section][rest] = v.(*atomic.Int64).Load()
		return true
	})
	hists := map[string]map[string]any{}
	m.hists.Range(func(k, v any) bool {
		section, rest := splitName(k.(string))
		if hists[section] == nil {
			hists[section] = map[string]any{}
		}
		hists[section][rest] = v.(*hist).summary()
		return true
	})
	return counters, hists
}

func splitName(name string) (string, string) {
	if i := strings.IndexByte(name, '.'); i >= 0 {
		return name[:i], name[i+1:]
	}
	return name, "total"
}

// ---------------------------------------------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------------------------------------------

type runner struct {
	cfg      *config
	roots    *x509.CertPool
	m        metrics
	start    time.Time
	end      time.Time
	browsers []*browser
	streams  []*stream
	expect   map[string]bool
	matcher  *matcher
	wheel    *wheel

	open            atomic.Int64 // streams that received ready and did not end since
	everOpen        atomic.Int64
	allOpenAfter    atomic.Int64 // nanoseconds since start
	domainEvents    atomic.Int64
	reconnects      atomic.Int64
	relogins        atomic.Int64
	errorsN         atomic.Int64
	restDone        atomic.Int64
	refetchDone     atomic.Int64
	emitDone        atomic.Int64
	restInflight    atomic.Int64
	refetchInflight atomic.Int64
	refetchPending  atomic.Int64

	abortTime    atomic.Int64 // unix nanos
	abortPending atomic.Int64
	abortAllBack atomic.Int64 // nanoseconds after the abort
	abortDropped atomic.Int64

	maxHeap atomic.Uint64

	logMu     sync.Mutex
	logCounts map[string]int
}

func (r *runner) logf(class string, format string, args ...any) {
	r.logMu.Lock()
	r.logCounts[class]++
	n := r.logCounts[class]
	r.logMu.Unlock()
	if n <= 5 || n%1000 == 0 {
		log.Printf("[%s #%d] %s", class, n, fmt.Sprintf(format, args...))
	}
}

// inAbortWindow: errors right after the abort are its own, counted apart
func (r *runner) inAbortWindow() bool {
	at := r.abortTime.Load()
	return at != 0 && time.Now().UnixNano()-at < int64(5*time.Second)
}

func (r *runner) streamError(class string, err error) {
	if r.inAbortWindow() {
		r.m.inc("stream_error_abort." + class)
		return
	}
	r.m.inc("stream_error." + class)
	r.errorsN.Add(1)
	r.logf("stream "+class, "%v", err)
}

// ---------------------------------------------------------------------------------------------------------------
// Browser: an account, its transport, its tokens; refresh and login are single-flight
// ---------------------------------------------------------------------------------------------------------------

type flight struct {
	done chan struct{}
	err  error
}

type browser struct {
	r       *runner
	idx     int
	acct    *account
	client  *http.Client
	tracker *connTracker
	streams []*stream
	uid     atomic.Int64
	busy    atomic.Bool // the emitter is changing this user

	mu      sync.Mutex
	access  string
	refresh string
	gen     int64
	flight  *flight
}

func newBrowser(r *runner, idx int, a *account) *browser {
	tr, tracker := newTransport(r.cfg, a, r.roots)
	return &browser{r: r, idx: idx, acct: a, client: &http.Client{Transport: tr}, tracker: tracker}
}

func (b *browser) creds() (string, int64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.access, b.gen
}

type problem struct {
	Code string `json:"code"`
}

func problemCode(body []byte) string {
	var p problem
	_ = json.Unmarshal(body, &p)
	return p.Code
}

func (b *browser) doLogin() error {
	r := b.r
	ctx, cancel := context.WithTimeout(context.Background(), r.cfg.requestTimeout)
	defer cancel()
	var body io.Reader
	if b.acct.login != nil {
		body = bytes.NewReader(b.acct.login)
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, r.cfg.base+pathLogin, body)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")
	t0 := time.Now()
	resp, err := b.client.Do(req)
	if err != nil {
		r.m.inc("auth.login_error")
		return err
	}
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	resp.Body.Close()
	r.m.h("auth.login").add(time.Since(t0))
	if resp.StatusCode != http.StatusOK {
		r.m.inc(fmt.Sprintf("auth.login_status_%d", resp.StatusCode))
		return fmt.Errorf("login %s: %d %s", b.acct.name, resp.StatusCode, problemCode(data))
	}
	var lr struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		UserID       int64  `json:"user_id"`
	}
	if err := json.Unmarshal(data, &lr); err != nil || lr.AccessToken == "" {
		return fmt.Errorf("login %s: unreadable answer", b.acct.name)
	}
	b.mu.Lock()
	b.access, b.refresh = lr.AccessToken, lr.RefreshToken
	b.gen++
	b.mu.Unlock()
	b.uid.Store(lr.UserID)
	r.m.inc("auth.logins")
	return nil
}

var errAuth = errors.New("refresh refused")

func (b *browser) doRefresh(refresh string) error {
	r := b.r
	ctx, cancel := context.WithTimeout(context.Background(), r.cfg.requestTimeout)
	defer cancel()
	body, _ := json.Marshal(map[string]string{"refresh_token": refresh})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, r.cfg.base+pathRefresh, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Activity", "background") // a refresh for the stream is no activity (API.md)
	t0 := time.Now()
	resp, err := b.client.Do(req)
	if err != nil {
		// The answer is lost: the refresh may have happened, so the token may be used already. Never send it again
		// (it would end the session as refresh_token_reused): the next renew logs in (API.md, "Authentication")
		r.m.inc("auth.refresh_answer_lost")
		b.burn(refresh)
		return err
	}
	data, readErr := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	resp.Body.Close()
	r.m.h("auth.refresh").add(time.Since(t0))
	if readErr != nil {
		r.m.inc("auth.refresh_answer_lost")
		b.burn(refresh)
		return readErr
	}
	if resp.StatusCode == http.StatusUnauthorized {
		r.m.inc("auth.refresh_refused_" + problemCode(data))
		return errAuth
	}
	if resp.StatusCode != http.StatusOK {
		// 5xx: the refresh was rolled back, the token still works; the caller backs off and tries again
		r.m.inc(fmt.Sprintf("auth.refresh_status_%d", resp.StatusCode))
		return fmt.Errorf("refresh %s: %d %s", b.acct.name, resp.StatusCode, problemCode(data))
	}
	var lr struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
	}
	if err := json.Unmarshal(data, &lr); err != nil || lr.AccessToken == "" {
		return errAuth
	}
	b.mu.Lock()
	b.access, b.refresh = lr.AccessToken, lr.RefreshToken
	b.mu.Unlock()
	r.m.inc("auth.refreshes")
	return nil
}

// burn forgets a refresh token whose refresh lost its answer
func (b *browser) burn(refresh string) {
	b.mu.Lock()
	if b.refresh == refresh {
		b.refresh = ""
	}
	b.mu.Unlock()
}

// renew refreshes the tokens once for every caller that saw the access token seen (a refresh token works once:
// never two refreshes in parallel); a refused refresh (401), or none left to send, logs in again
func (b *browser) renew(seen string) error {
	for {
		b.mu.Lock()
		if b.access != seen {
			b.mu.Unlock()
			return nil
		}
		if f := b.flight; f != nil {
			b.mu.Unlock()
			<-f.done
			if f.err != nil {
				return f.err
			}
			continue
		}
		f := &flight{done: make(chan struct{})}
		b.flight = f
		refresh := b.refresh
		b.mu.Unlock()
		err := errAuth
		if refresh != "" {
			err = b.doRefresh(refresh)
		}
		if errors.Is(err, errAuth) {
			b.r.m.inc("auth.login_after_refused_refresh")
			b.r.relogins.Add(1)
			err = b.doLogin()
		}
		b.mu.Lock()
		b.flight = nil
		b.mu.Unlock()
		f.err = err
		close(f.done)
		return err
	}
}

// relogin logs in once for every caller that saw the login generation seen
func (b *browser) relogin(seen int64) error {
	for {
		b.mu.Lock()
		if b.gen != seen {
			b.mu.Unlock()
			return nil
		}
		if f := b.flight; f != nil {
			b.mu.Unlock()
			<-f.done
			if f.err != nil {
				return f.err
			}
			continue
		}
		f := &flight{done: make(chan struct{})}
		b.flight = f
		b.mu.Unlock()
		b.r.relogins.Add(1)
		err := b.doLogin()
		b.mu.Lock()
		b.flight = nil
		b.mu.Unlock()
		f.err = err
		close(f.done)
		return err
	}
}

// do sends one request with the browser's token; an expired access token is refreshed and the request repeated
// once, an ended session logs in again
func (b *browser) do(ctx context.Context, method, path, ctype string, body []byte, background, keep bool) (int, []byte, error) {
	r := b.r
	for attempt := 0; ; attempt++ {
		access, gen := b.creds()
		var rd io.Reader
		if body != nil {
			rd = bytes.NewReader(body)
		}
		req, _ := http.NewRequestWithContext(ctx, method, r.cfg.base+path, rd)
		req.Header.Set("Authorization", "Bearer "+access)
		req.Header.Set("Accept", "application/json")
		if ctype != "" {
			req.Header.Set("Content-Type", ctype)
		}
		if background {
			req.Header.Set("X-Activity", "background")
		}
		resp, err := b.client.Do(req)
		if err != nil {
			return 0, nil, err
		}
		if resp.StatusCode == http.StatusUnauthorized && attempt == 0 {
			data, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
			resp.Body.Close()
			switch problemCode(data) {
			case "invalid_token":
				r.m.inc("auth.request_invalid_token")
				if b.renew(access) == nil {
					continue
				}
			case "session_expired", "token_certificate_mismatch":
				r.m.inc("auth.request_session_expired")
				if b.relogin(gen) == nil {
					continue
				}
			}
			return resp.StatusCode, data, nil
		}
		var data []byte
		if keep || resp.StatusCode >= 400 {
			data, _ = io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		return resp.StatusCode, data, nil
	}
}

func (b *browser) startAndRun(ctx context.Context, wg *sync.WaitGroup) {
	r := b.r
	// The login happens when the browser opens its first tab, so the tokens of the browsers expire spread as the ramp
	if len(b.streams) > 0 && !sleepUntil(ctx, b.streams[0].startAt) {
		return
	}
	for attempt := 0; ; attempt++ {
		if err := b.doLogin(); err == nil {
			break
		} else {
			r.m.inc("auth.login_failures")
			r.errorsN.Add(1)
			r.logf("login", "%v", err)
		}
		wait := time.Duration(math.Min(30, math.Pow(2, float64(attempt)))*float64(time.Second)) + jitter(time.Second)
		if !sleepCtx(ctx, wait) {
			return
		}
	}
	for _, s := range b.streams {
		if !sleepUntil(ctx, s.startAt) {
			return
		}
		wg.Add(1)
		go func(s *stream) {
			defer wg.Done()
			s.run(ctx)
		}(s)
	}
}

// ---------------------------------------------------------------------------------------------------------------
// Stream: one tab's GET /events/stream, the web kit's behaviour
// ---------------------------------------------------------------------------------------------------------------

const seenRing = 128

// replayWindow: a domain event this soon after ready on a reconnect counts as replayed (lag_replay)
const replayWindow = 250 * time.Millisecond

type stream struct {
	b       *browser
	id      int
	startAt time.Time

	// owned by the stream's goroutine
	position string
	retry    time.Duration
	failures int
	connects int
	open     bool
	gen      int64
	seen     [seenRing]uint64
	seenPos  int
	readyAt  time.Time // ready of the current connection: what follows it at once on a reconnect is the replay

	openedOnce atomic.Bool
	resetAt    atomic.Int64 // unix nanos of the last reload or position reset: deliveries before it are excused
	abortedAt  atomic.Int64
}

type outKind int

const (
	outStop outKind = iota
	outClose
	outEnded
	outStatus
	outErr
	outOff
	outUnauthorized
)

type outcome struct {
	kind       outKind
	reason     string
	retryAfter time.Duration
	access     string
	gen        int64
}

type block struct {
	id       string
	hasID    bool
	data     []byte
	hasData  bool
	retry    int64
	hasRetry bool
}

func (k *block) reset() {
	k.id, k.hasID, k.data, k.hasData, k.hasRetry = "", false, k.data[:0], false, false
}

// field parses one line of a block (WHATWG server-sent events)
func (k *block) field(line []byte) {
	if line[0] == ':' {
		return
	}
	name, value := line, []byte(nil)
	if i := bytes.IndexByte(line, ':'); i >= 0 {
		name, value = line[:i], line[i+1:]
		if len(value) > 0 && value[0] == ' ' {
			value = value[1:]
		}
	}
	switch string(name) {
	case "data":
		if k.hasData {
			k.data = append(k.data, '\n')
		}
		k.data = append(k.data, value...)
		k.hasData = true
	case "id":
		if bytes.IndexByte(value, 0) < 0 {
			k.id, k.hasID = string(value), true
		}
	case "retry":
		if n, err := strconv.ParseInt(string(value), 10, 64); err == nil && n >= 0 {
			k.retry, k.hasRetry = n, true
		}
	}
}

type message struct {
	Type    string `json:"type"`
	ID      string `json:"id"`
	Refetch int64  `json:"refetch_within_ms"`
	Scope   *struct {
		Kind string `json:"kind"`
		ID   int64  `json:"id"`
	} `json:"scope"`
	Data struct {
		Reason        string `json:"reason"`
		MoveID        int64  `json:"move_id"`
		SubjectUserID int64  `json:"subject_user_id"`
		CertificateID int64  `json:"certificate_id"`
		RoleID        int64  `json:"role_id"`
		Dictionary    string `json:"dictionary"`
		AuditLogID    int64  `json:"audit_log_id"`
	} `json:"data"`
}

func (s *stream) run(ctx context.Context) {
	r := s.b.r
	var delay time.Duration
	for {
		if delay > 0 && !sleepCtx(ctx, delay) {
			return
		}
		if ctx.Err() != nil {
			return
		}
		out := s.connect(ctx)
		switch out.kind {
		case outStop:
			return
		case outOff: // 204: events are off on this server, stop until the page reloads
			r.m.inc("stream.events_off")
			return
		case outEnded: // the stream may end at any time: reconnect after the retry
			delay = s.retry
		case outClose:
			switch out.reason {
			case "token_expired":
				if err := s.b.renew(out.access); err != nil {
					r.logf("renew", "%s: %v", s.b.acct.name, err)
					delay = s.backoff(0)
				} else {
					delay = 0
				}
			case "session_ended":
				r.m.inc("stream.session_ended")
				if err := s.b.relogin(out.gen); err != nil {
					r.logf("relogin", "%s: %v", s.b.acct.name, err)
					delay = s.backoff(0)
				} else {
					delay = 0
				}
			default: // restart: the retry it carried
				delay = s.retry
			}
		case outUnauthorized: // never reconnect in a loop on a 401: refresh, else log in, then back off
			if err := s.b.renew(out.access); err != nil {
				r.logf("renew", "%s after 401: %v", s.b.acct.name, err)
			}
			delay = s.backoff(0)
		case outStatus:
			delay = s.backoff(out.retryAfter)
		case outErr:
			delay = s.backoff(0)
		}
	}
}

// backoff: a random time between retry and min(60 s, retry x 2^n), at least Retry-After (plus up to 20% jitter)
func (s *stream) backoff(atLeast time.Duration) time.Duration {
	s.failures++
	upper := s.retry << uint(min(s.failures, 10))
	if upper > 60*time.Second {
		upper = 60 * time.Second
	}
	if upper < s.retry {
		upper = s.retry
	}
	d := s.retry + time.Duration(mrand.Float64()*float64(upper-s.retry))
	if atLeast > 0 {
		if a := atLeast + time.Duration(mrand.Float64()*0.2*float64(atLeast)); a > d {
			d = a
		}
	}
	return d
}

func (s *stream) connect(ctx context.Context) outcome {
	b := s.b
	r := b.r
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	access, gen := b.creds()
	out := outcome{access: access, gen: gen}
	if gen != s.gen {
		// After a new login a tab starts without a position (API.md, "Two ids")
		if s.gen != 0 && s.position != "" {
			s.position = ""
			s.resetAt.Store(time.Now().UnixNano())
		}
		s.gen = gen
	}
	req, _ := http.NewRequestWithContext(cctx, http.MethodGet, r.cfg.base+pathStream, nil)
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Authorization", "Bearer "+access)
	req.Header.Set("Cache-Control", "no-store")
	if s.position != "" {
		req.Header.Set("Last-Event-ID", s.position)
	}
	var watchdogFired atomic.Bool
	watchdog := time.AfterFunc(r.cfg.connectTimeout, func() { watchdogFired.Store(true); cancel() })
	t0 := time.Now()
	resp, err := b.client.Do(req)
	watchdog.Stop()
	if err != nil {
		if ctx.Err() != nil {
			out.kind = outStop
			return out
		}
		class := "connect"
		if watchdogFired.Load() {
			class = "connect_watchdog"
		}
		r.streamError(class, err)
		out.kind = outErr
		return out
	}
	r.m.h("stream.connect").add(time.Since(t0))
	r.m.inc("stream_status." + strconv.Itoa(resp.StatusCode))
	r.m.inc("stream_protocol." + resp.Proto)
	if resp.StatusCode != http.StatusOK {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		resp.Body.Close()
		out.retryAfter = parseRetryAfter(resp.Header.Get("Retry-After"))
		switch resp.StatusCode {
		case http.StatusNoContent:
			out.kind = outOff
		case http.StatusUnauthorized:
			r.m.inc("stream_401." + problemCode(data))
			out.kind = outUnauthorized
		default:
			if code := problemCode(data); code != "" {
				r.m.inc("stream_refused." + code)
			}
			if resp.StatusCode >= 500 || resp.StatusCode == 429 {
				r.logf(fmt.Sprintf("stream status %d", resp.StatusCode), "%s retry-after=%s", b.acct.name, resp.Header.Get("Retry-After"))
			}
			if !r.inAbortWindow() && resp.StatusCode != 503 && resp.StatusCode != 429 {
				r.errorsN.Add(1)
			}
			out.kind = outStatus
		}
		return out
	}
	defer resp.Body.Close()
	s.failures = 0
	var silenceFired atomic.Bool
	silence := time.AfterFunc(r.cfg.silence, func() { silenceFired.Store(true); cancel() })
	defer silence.Stop()
	defer s.markClosed()
	br := bufio.NewReaderSize(resp.Body, 1024)
	var blk block
	var long []byte
	for {
		line, err := br.ReadSlice('\n')
		if err == bufio.ErrBufferFull {
			long = append(long, line...)
			continue
		}
		if err != nil {
			if ctx.Err() != nil {
				out.kind = outStop
				return out
			}
			if silenceFired.Load() {
				r.streamError("silence_watchdog", err)
				out.kind = outErr
				return out
			}
			if err == io.EOF {
				r.m.inc("stream.ended_without_close")
				out.kind = outEnded
				return out
			}
			r.streamError("read", err)
			out.kind = outErr
			return out
		}
		silence.Reset(r.cfg.silence)
		if long != nil {
			line = append(long, line...)
			long = nil
		}
		line = bytes.TrimRight(line, "\r\n")
		if len(line) == 0 {
			if reason := s.dispatch(&blk); reason != "" {
				out.kind, out.reason = outClose, reason
				return out
			}
			blk.reset()
			continue
		}
		blk.field(line)
	}
}

func (s *stream) markClosed() {
	if s.open {
		s.open = false
		s.b.r.open.Add(-1)
	}
}

func fnv64(text string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(text))
	return h.Sum64()
}

func (s *stream) seenBefore(id string) bool {
	h := fnv64(id)
	for _, v := range s.seen {
		if v == h {
			return true
		}
	}
	s.seen[s.seenPos] = h
	s.seenPos = (s.seenPos + 1) % seenRing
	return false
}

// dispatch handles one block; a close message returns its reason (the connection then ends)
func (s *stream) dispatch(k *block) string {
	r := s.b.r
	if k.hasID {
		s.position = k.id // the heartbeat's too
	}
	if k.hasRetry {
		s.retry = time.Duration(k.retry) * time.Millisecond
	}
	if !k.hasData || len(k.data) == 0 {
		return ""
	}
	var msg message
	if err := json.Unmarshal(k.data, &msg); err != nil {
		r.m.inc("stream_error.bad_json")
		r.errorsN.Add(1)
		return ""
	}
	now := time.Now()
	switch msg.Type {
	case "ready":
		r.m.inc("message.ready")
		s.onReady(now)
	case "heartbeat":
		r.m.inc("message.heartbeat")
	case "reload":
		r.m.inc("message.reload")
		s.resetAt.Store(now.UnixNano())
		r.schedule(s.b, rkMe, 0, msg.Refetch)
	case "close":
		reason := msg.Data.Reason
		if reason == "" {
			reason = "unknown"
		}
		r.m.inc("message.close_" + reason)
		return reason
	default:
		if s.seenBefore(msg.ID) {
			r.m.inc("event_duplicate." + msg.Type)
			return ""
		}
		label := msg.Type
		if msg.Type == "dictionary.changed" {
			label += ":" + msg.Data.Dictionary
		}
		r.m.inc("event." + label)
		r.domainEvents.Add(1)
		// The replay of a reconnect arrives right behind ready, in the prelude: its lag is the time the stream was away
		replay := s.connects > 1 && now.Sub(s.readyAt) < replayWindow
		r.matcher.receive(s, &msg, label, now, replay)
		if kind, id, ok := refetchFor(&msg, s.b.uid.Load()); ok {
			if r.cfg.refetchRatio >= 1 || mrand.Float64() < r.cfg.refetchRatio {
				r.schedule(s.b, kind, id, msg.Refetch)
			}
		}
	}
	return ""
}

func (s *stream) onReady(now time.Time) {
	r := s.b.r
	if !s.open {
		s.open = true
		r.open.Add(1)
	}
	if s.connects > 0 {
		r.m.inc("stream.reconnects")
		r.reconnects.Add(1)
	}
	s.connects++
	s.readyAt = now
	if !s.openedOnce.Load() {
		s.openedOnce.Store(true)
		if r.everOpen.Add(1) == int64(len(r.streams)) {
			r.allOpenAfter.Store(int64(now.Sub(r.start)))
		}
	}
	if a := s.abortedAt.Load(); a != 0 && s.abortedAt.CompareAndSwap(a, 0) {
		r.m.h("abort.recovery").add(time.Duration(now.UnixNano() - a))
		if r.abortPending.Add(-1) == 0 {
			r.abortAllBack.Store(now.UnixNano() - r.abortTime.Load())
		}
	}
}

func parseRetryAfter(v string) time.Duration {
	if v == "" {
		return 0
	}
	if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil && n > 0 {
		return time.Duration(n) * time.Second
	}
	if t, err := http.ParseTime(v); err == nil {
		if d := time.Until(t); d > 0 {
			return d
		}
	}
	return 0
}

// ---------------------------------------------------------------------------------------------------------------
// Refetch: what a page would read after an event, after a random wait below refetch_within_ms (X-Activity:
// background). Pending refetches sit in a timing wheel: 16 bytes each, no timer per refetch.
// ---------------------------------------------------------------------------------------------------------------

type rkind uint8

const (
	rkMe rkind = iota
	rkPosts
	rkUser
	rkUserCertificates
	rkUserSessions
	rkMySessions
	rkRole
	rkMove
	rkAudit
	rkTags
	rkCategories
	rkSignalTypes
	rkMediaFormats
	rkGuides
)

var rkNames = [...]string{"me", "posts", "user", "user_certificates", "user_sessions", "my_sessions", "role", "move",
	"audit_log", "tags", "categories", "signal_types", "media_formats", "guides"}

func (k rkind) path(id int64) string {
	switch k {
	case rkPosts:
		return pathPosts
	case rkUser:
		return fmt.Sprintf(pathUserByID, id)
	case rkUserCertificates:
		return fmt.Sprintf(pathUserCertificates, id)
	case rkUserSessions:
		return fmt.Sprintf(pathUserSessions, id)
	case rkMySessions:
		return pathMySessions
	case rkRole:
		return fmt.Sprintf(pathRoleByID, id)
	case rkMove:
		return fmt.Sprintf(pathMoveByID, id)
	case rkAudit:
		return fmt.Sprintf(pathAuditByID, id)
	case rkTags:
		return pathTagsList
	case rkCategories:
		return pathCategoriesList
	case rkSignalTypes:
		return pathSignalTypesList
	case rkMediaFormats:
		return pathMediaFormatsList
	case rkGuides:
		return pathGuides
	}
	return pathMe
}

// refetchFor: the GET a page showing what the event names would make (API.md, "The administration")
func refetchFor(msg *message, self int64) (rkind, int64, bool) {
	d := msg.Data
	switch msg.Type {
	case "scope.changed":
		return rkPosts, 0, true
	case "move.changed":
		return rkMove, d.MoveID, true
	case "user.changed":
		if d.SubjectUserID == self {
			return rkMe, 0, true // your own rights may have changed
		}
		return rkUser, d.SubjectUserID, true
	case "certificate.changed":
		return rkUserCertificates, d.SubjectUserID, true
	case "session.changed":
		if d.SubjectUserID == self {
			return rkMySessions, 0, true
		}
		return rkUserSessions, d.SubjectUserID, true
	case "role.changed":
		return rkRole, d.RoleID, true
	case "security.certificate_mismatch":
		return rkAudit, d.AuditLogID, true
	case "dictionary.changed":
		switch d.Dictionary {
		case "tags":
			return rkTags, 0, true
		case "categories":
			return rkCategories, 0, true
		case "signal_types":
			return rkSignalTypes, 0, true
		case "media_formats":
			return rkMediaFormats, 0, true
		case "guides":
			return rkGuides, 0, true
		}
	}
	return 0, 0, false
}

type job struct {
	b    int32
	kind rkind
	id   int64
}

type wheel struct {
	mu    sync.Mutex
	slots [][]job
	tick  time.Duration
	pos   int
}

func newWheel(span, tick time.Duration) *wheel {
	return &wheel{slots: make([][]job, int(span/tick)+2), tick: tick}
}

func (w *wheel) after(d time.Duration, j job) {
	steps := int(d / w.tick)
	if steps > len(w.slots)-2 {
		steps = len(w.slots) - 2
	}
	w.mu.Lock()
	i := (w.pos + 1 + steps) % len(w.slots)
	w.slots[i] = append(w.slots[i], j)
	w.mu.Unlock()
}

func (w *wheel) run(ctx context.Context, fire func(job)) {
	t := time.NewTicker(w.tick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		w.mu.Lock()
		w.pos = (w.pos + 1) % len(w.slots)
		batch := w.slots[w.pos]
		w.slots[w.pos] = nil
		w.mu.Unlock()
		for _, j := range batch {
			fire(j)
		}
	}
}

func (r *runner) schedule(b *browser, kind rkind, id int64, within int64) {
	if within < 0 {
		within = 0
	}
	r.m.inc("refetch_scheduled." + rkNames[kind])
	r.refetchPending.Add(1)
	r.wheel.after(time.Duration(mrand.Float64()*float64(within))*time.Millisecond, job{b: int32(b.idx), kind: kind, id: id})
}

func (r *runner) fireRefetch(ctx context.Context, j job) {
	r.refetchPending.Add(-1)
	if ctx.Err() != nil {
		return
	}
	if r.refetchInflight.Load() >= int64(r.cfg.refetchMaxInflight) {
		r.m.inc("refetch_dropped." + rkNames[j.kind])
		return
	}
	b := r.browsers[j.b]
	r.refetchInflight.Add(1)
	go func() {
		defer r.refetchInflight.Add(-1)
		rctx, cancel := context.WithTimeout(ctx, r.cfg.requestTimeout)
		defer cancel()
		t0 := time.Now()
		status, _, err := b.do(rctx, http.MethodGet, j.kind.path(j.id), "", nil, true, false)
		name := rkNames[j.kind]
		r.m.h("refetch." + name).add(time.Since(t0))
		r.refetchDone.Add(1)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if r.inAbortWindow() {
				r.m.inc("refetch_status_abort." + name)
				return
			}
			r.m.inc("refetch_status." + name + "_error")
			r.errorsN.Add(1)
			r.logf("refetch error", "%s: %v", name, err)
			return
		}
		r.m.inc(fmt.Sprintf("refetch_status.%s_%d", name, status))
		if status >= 500 {
			r.errorsN.Add(1)
		}
	}()
}

// ---------------------------------------------------------------------------------------------------------------
// Matcher: binds each event to the emitter write that caused it and checks the deliveries that can be computed
// ---------------------------------------------------------------------------------------------------------------

type mwrite struct {
	kind     string
	key      string
	start    time.Time
	done     atomic.Int64 // unix nanos of the response
	bound    atomic.Bool
	eventID  string
	failed   bool
	seq      uint64        // order of the writes of one key
	seen     chan struct{} // closed when the event is first received anywhere
	expected []uint64      // streams that must receive it (nil: not computable)
	got      []uint64
	late     []uint64 // set at the check: streams that had not received it within the window
}

// conflated: the server keeps only the latest queued event of one user or dictionary (API.md, "The
// administration"): a stream that is behind may get a later write's event instead of this one's
func conflated(key string) bool {
	return strings.HasPrefix(key, "dict:") || strings.HasPrefix(key, "user:")
}

type missSample struct {
	Kind     string  `json:"kind"`
	Key      string  `json:"key"`
	Stream   int     `json:"stream"`
	Account  string  `json:"account"`
	WriteAtS float64 `json:"write_at_s"`
}

type matcher struct {
	r       *runner
	mu      sync.Mutex
	queue   map[string][]*mwrite // per resource, the writes whose event has not come yet, oldest first
	byEvent map[string]*mwrite   // event identity -> write
	foreign map[string]int64     // event identities no write of ours explains
	live    []*mwrite
	retired []*mwrite
	seqs    map[string]uint64           // writes begun per key
	cover   map[string]map[int32]uint64 // conflated keys: per stream, the latest write whose event it received

	checked, noEvent, failed, unchecked   int64
	expectedN, delivered, missed, excused int64
	conflatedN, lateN                     int64
	samples                               []missSample
}

func newMatcher(r *runner) *matcher {
	return &matcher{r: r, queue: map[string][]*mwrite{}, byEvent: map[string]*mwrite{}, foreign: map[string]int64{},
		seqs: map[string]uint64{}, cover: map[string]map[int32]uint64{}}
}

// eventKey: the resource a write of the emitter names, as the event shows it
func eventKey(msg *message) string {
	switch msg.Type {
	case "scope.changed":
		if msg.Scope != nil && msg.Scope.Kind == "post" {
			return "post:" + strconv.FormatInt(msg.Scope.ID, 10)
		}
	case "dictionary.changed":
		return "dict:" + msg.Data.Dictionary
	case "user.changed":
		return "user:" + strconv.FormatInt(msg.Data.SubjectUserID, 10)
	}
	return ""
}

func (m *matcher) begin(kind, key string, expected []uint64) *mwrite {
	w := &mwrite{kind: kind, key: key, start: time.Now(), expected: expected, seen: make(chan struct{})}
	if expected != nil {
		w.got = make([]uint64, len(expected))
	}
	m.mu.Lock()
	m.seqs[key]++
	w.seq = m.seqs[key]
	m.queue[key] = append(m.queue[key], w)
	m.live = append(m.live, w)
	m.mu.Unlock()
	return w
}

func (m *matcher) removeQueued(w *mwrite) {
	q := m.queue[w.key]
	for i, x := range q {
		if x == w {
			m.queue[w.key] = append(q[:i:i], q[i+1:]...)
			return
		}
	}
}

func (m *matcher) finish(w *mwrite, done time.Time, ok bool) {
	w.done.Store(done.UnixNano())
	if !ok {
		m.mu.Lock()
		w.failed = true
		if !w.bound.Load() {
			m.removeQueued(w)
		}
		m.mu.Unlock()
	}
}

func (m *matcher) receive(s *stream, msg *message, label string, now time.Time, replay bool) {
	key := eventKey(msg)
	if key == "" {
		return
	}
	m.mu.Lock()
	w := m.byEvent[msg.ID]
	if w == nil {
		if _, isForeign := m.foreign[msg.ID]; !isForeign {
			if q := m.queue[key]; len(q) > 0 && !q[0].start.After(now) {
				w = q[0]
				m.queue[key] = q[1:]
				w.eventID = msg.ID
				w.bound.Store(true)
				close(w.seen)
				m.byEvent[msg.ID] = w
			} else {
				m.foreign[msg.ID] = now.UnixNano()
			}
		}
	}
	if w != nil && w.late != nil && s.id/64 < len(w.late) {
		bit := uint64(1) << (uint(s.id) % 64)
		if w.late[s.id/64]&bit != 0 {
			w.late[s.id/64] &^= bit
			m.lateN++
		}
	}
	if w != nil && conflated(key) {
		c := m.cover[key]
		if c == nil {
			c = map[int32]uint64{}
			m.cover[key] = c
		}
		if w.seq > c[int32(s.id)] {
			c[int32(s.id)] = w.seq
		}
	}
	m.mu.Unlock()
	if w == nil {
		m.r.m.inc("event_unmatched." + label)
		return
	}
	if w.got != nil && s.id/64 < len(w.got) {
		atomic.OrUint64(&w.got[s.id/64], 1<<(uint(s.id)%64))
	}
	d := w.done.Load()
	lag := time.Duration(0)
	if d == 0 || now.UnixNano() < d {
		m.r.m.inc("event_before_response." + label)
	} else {
		lag = time.Duration(now.UnixNano() - d)
	}
	if replay {
		m.r.m.h("lag_replay." + label).add(lag)
		m.r.m.h("lag_replay.all").add(lag)
		return
	}
	m.r.m.h("lag." + label).add(lag)
	m.r.m.h("lag.all").add(lag)
}

// finalize checks every write whose delivery window has passed (all of them with force)
func (m *matcher) finalize(now time.Time, force bool) {
	within := int64(m.r.cfg.deliverWithin)
	var due []*mwrite
	m.mu.Lock()
	keep := m.live[:0]
	for _, w := range m.live {
		d := w.done.Load()
		switch {
		case d != 0 && now.UnixNano()-d >= within:
			due = append(due, w)
		case force:
			m.unchecked++
			if !w.bound.Load() {
				m.removeQueued(w)
			}
		default:
			keep = append(keep, w)
		}
	}
	for i := len(keep); i < len(m.live); i++ {
		m.live[i] = nil
	}
	m.live = keep
	for _, w := range due {
		if !w.bound.Load() {
			m.removeQueued(w) // never came: it must not explain a later event
		}
		m.retired = append(m.retired, w)
	}
	// forget identities long after their window: a very late copy then counts as unmatched
	oldest := now.UnixNano() - 3*within
	keepRetired := m.retired[:0]
	for _, w := range m.retired {
		if w.done.Load() < oldest {
			if w.eventID != "" {
				delete(m.byEvent, w.eventID)
			}
			continue
		}
		keepRetired = append(keepRetired, w)
	}
	m.retired = keepRetired
	for id, t := range m.foreign {
		if t < oldest {
			delete(m.foreign, id)
		}
	}
	for _, w := range due {
		if w.failed {
			m.failed++
			continue
		}
		m.checked++
		if !w.bound.Load() {
			m.noEvent++
		}
		if w.expected == nil {
			continue
		}
		for i, word := range w.expected {
			got := atomic.LoadUint64(&w.got[i])
			m.expectedN += int64(bits.OnesCount64(word))
			m.delivered += int64(bits.OnesCount64(word & got))
			miss := word &^ got
			for miss != 0 {
				bit := bits.TrailingZeros64(miss)
				miss &= miss - 1
				s := m.r.streams[i*64+bit]
				if s.resetAt.Load() > w.start.UnixNano() {
					m.excused++
					continue
				}
				if conflated(w.key) && m.cover[w.key][int32(s.id)] > w.seq {
					m.conflatedN++ // a later write's event replaced it in the stream's queue: delivered as designed
					continue
				}
				m.missed++
				if w.late == nil {
					w.late = make([]uint64, len(w.expected))
				}
				w.late[i] |= 1 << uint(bit)
				if len(m.samples) < 50 {
					m.samples = append(m.samples, missSample{Kind: w.kind, Key: w.key, Stream: s.id, Account: s.b.acct.name,
						WriteAtS: round1(w.start.Sub(m.r.start).Seconds())})
				}
			}
		}
	}
	m.mu.Unlock()
}

// ---------------------------------------------------------------------------------------------------------------
// Emitter: writes through the API with an administrator account, at a set rate, one write in flight per resource
// ---------------------------------------------------------------------------------------------------------------

type fixturePost struct {
	id       int64
	archived bool
	busy     atomic.Bool
}

type emitter struct {
	r         *runner
	adm       *browser
	stamp     string
	mix       *weighted
	posts     []*fixturePost
	tagID     int64
	guideID   int64
	tagBusy   atomic.Bool
	guideBusy atomic.Bool
	n         atomic.Int64
}

func (e *emitter) jsonCall(ctx context.Context, method, path string, body any, want ...int) ([]byte, error) {
	data, _ := json.Marshal(body)
	status, resp, err := e.adm.do(ctx, method, path, "application/json", data, false, true)
	if err != nil {
		return nil, err
	}
	for _, w := range want {
		if status == w {
			return resp, nil
		}
	}
	return nil, fmt.Errorf("%s %s: %d %s", method, path, status, truncate(string(resp), 300))
}

func idOf(data []byte) int64 {
	var v struct {
		ID int64 `json:"id"`
	}
	_ = json.Unmarshal(data, &v)
	return v.ID
}

func (e *emitter) setup(ctx context.Context) error {
	if err := e.adm.doLogin(); err != nil {
		return fmt.Errorf("emitter login: %w", err)
	}
	if e.mix.weight("post") > 0 {
		for i := 0; i < e.r.cfg.emitPosts; i++ {
			data, err := e.jsonCall(ctx, http.MethodPost, pathPosts, map[string]any{
				"name": fmt.Sprintf("Load %s %d", e.stamp, i), "code": fmt.Sprintf("LD%s-%d", e.stamp, i),
			}, 200, 201)
			if err != nil {
				return err
			}
			e.posts = append(e.posts, &fixturePost{id: idOf(data)})
		}
	}
	if e.mix.weight("tag") > 0 {
		data, err := e.jsonCall(ctx, http.MethodPost, pathTagCreate, map[string]any{"name": "load-" + e.stamp}, 200, 201)
		if err != nil {
			return err
		}
		e.tagID = idOf(data)
	}
	if e.mix.weight("guide") > 0 {
		data, err := e.jsonCall(ctx, http.MethodPost, pathGuides, map[string]any{"title": "Load test " + e.stamp}, 200, 201)
		if err != nil {
			return err
		}
		e.guideID = idOf(data)
	}
	log.Printf("emitter: %d posts, tag %d, guide %d (stamp %s)", len(e.posts), e.tagID, e.guideID, e.stamp)
	return nil
}

func (e *emitter) cleanup(ctx context.Context) {
	for _, p := range e.posts {
		if !p.archived {
			if _, err := e.jsonCall(ctx, http.MethodPost, fmt.Sprintf(pathPostArchive, p.id), nil, 200); err != nil {
				log.Printf("cleanup: %v", err)
			}
		}
	}
	if e.tagID != 0 {
		if _, err := e.jsonCall(ctx, http.MethodPost, fmt.Sprintf(pathTagArchive, e.tagID), nil, 200); err != nil {
			log.Printf("cleanup: %v", err)
		}
	}
	if e.guideID != 0 {
		if _, err := e.jsonCall(ctx, http.MethodPost, fmt.Sprintf(pathGuideArchive, e.guideID), nil, 200); err != nil {
			log.Printf("cleanup: %v", err)
		}
	}
}

// expectedAll: every stream that has connected once (it has a position: live or by replay it must get the event)
func (r *runner) expectedAll() []uint64 {
	set := make([]uint64, (len(r.streams)+63)/64)
	for _, s := range r.streams {
		if s.openedOnce.Load() {
			set[s.id/64] |= 1 << (uint(s.id) % 64)
		}
	}
	return set
}

func (r *runner) expectedOf(b *browser) []uint64 {
	set := make([]uint64, (len(r.streams)+63)/64)
	for _, s := range b.streams {
		if s.openedOnce.Load() {
			set[s.id/64] |= 1 << (uint(s.id) % 64)
		}
	}
	return set
}

func (e *emitter) tick(ctx context.Context) {
	kind := e.mix.pick()
	switch kind {
	case "post":
		var p *fixturePost
		for _, i := range mrand.Perm(len(e.posts)) {
			if e.posts[i].busy.CompareAndSwap(false, true) {
				p = e.posts[i]
				break
			}
		}
		if p == nil {
			e.r.m.inc("emit_skipped.post_busy")
			return
		}
		go func() {
			defer p.busy.Store(false)
			path := fmt.Sprintf(pathPostArchive, p.id)
			if p.archived {
				path = fmt.Sprintf(pathPostRestore, p.id)
			}
			if e.write(ctx, "post", fmt.Sprintf("post:%d", p.id), e.expected("post", nil), http.MethodPost, path, "", nil) != nil {
				p.archived = !p.archived
			}
		}()
	case "tag":
		if !e.tagBusy.CompareAndSwap(false, true) {
			e.r.m.inc("emit_skipped.tag_busy")
			return
		}
		go func() {
			defer e.tagBusy.Store(false)
			body, _ := json.Marshal(map[string]string{"name": fmt.Sprintf("load-%s-%d", e.stamp, e.n.Add(1))})
			e.gate(ctx, e.write(ctx, "tag", "dict:tags", e.expected("tag", nil), http.MethodPut, fmt.Sprintf(pathTagByID, e.tagID), "application/json", body))
		}()
	case "guide":
		if !e.guideBusy.CompareAndSwap(false, true) {
			e.r.m.inc("emit_skipped.guide_busy")
			return
		}
		go func() {
			defer e.guideBusy.Store(false)
			body, _ := json.Marshal(map[string]string{"title": fmt.Sprintf("Load test %s %d", e.stamp, e.n.Add(1))})
			e.gate(ctx, e.write(ctx, "guide", "dict:guides", e.expected("guide", nil), http.MethodPatch, fmt.Sprintf(pathGuideByID, e.guideID), "application/json", body))
		}()
	case "user":
		var b *browser
		for try := 0; try < 10 && len(e.r.browsers) > 0; try++ {
			c := e.r.browsers[mrand.IntN(len(e.r.browsers))]
			if c.uid.Load() > 0 && c.busy.CompareAndSwap(false, true) {
				b = c
				break
			}
		}
		if b == nil {
			e.r.m.inc("emit_skipped.user_busy")
			return
		}
		go func() {
			defer b.busy.Store(false)
			uid := b.uid.Load()
			form := url.Values{"username_alias": {fmt.Sprintf("Load user %s %d", b.acct.name, e.n.Add(1))}}
			e.gate(ctx, e.write(ctx, "user", fmt.Sprintf("user:%d", uid), e.expected("user", b), http.MethodPatch,
				fmt.Sprintf(pathUserByID, uid), "application/x-www-form-urlencoded", []byte(form.Encode())))
		}()
	}
}

func (e *emitter) expected(kind string, b *browser) []uint64 {
	if !e.r.expect[kind] {
		return nil
	}
	if kind == "user" {
		return e.r.expectedOf(b) // user.changed reaches the user themself
	}
	return e.r.expectedAll()
}

// gate holds a conflated key until the write's event has reached some stream (or the delivery window passed): with
// one write of a key waiting at a time, an event binds to its own write even when a slow stream's queue replaced
// earlier events of the key by later ones
func (e *emitter) gate(ctx context.Context, w *mwrite) {
	if w == nil {
		return
	}
	t := time.NewTimer(e.r.cfg.deliverWithin)
	defer t.Stop()
	select {
	case <-w.seen:
	case <-ctx.Done():
	case <-t.C:
		m := e.r.matcher
		m.mu.Lock()
		if !w.bound.Load() {
			m.removeQueued(w)
		}
		m.mu.Unlock()
		e.r.m.inc("emit_gate_timeout." + w.kind)
	}
}

// write sends one write and returns it, nil when it failed
func (e *emitter) write(ctx context.Context, kind, key string, expected []uint64, method, path, ctype string, body []byte) *mwrite {
	r := e.r
	w := r.matcher.begin(kind, key, expected)
	t0 := time.Now()
	wctx, cancel := context.WithTimeout(ctx, r.cfg.requestTimeout)
	defer cancel()
	status, data, err := e.adm.do(wctx, method, path, ctype, body, false, false)
	done := time.Now()
	r.m.h("emit." + kind).add(done.Sub(t0))
	r.emitDone.Add(1)
	ok := err == nil && status >= 200 && status < 300
	r.matcher.finish(w, done, ok)
	if ok {
		r.m.inc("emit." + kind + "_ok")
		return w
	}
	if err != nil && ctx.Err() != nil {
		return nil
	}
	r.m.inc(fmt.Sprintf("emit.%s_failed_%d", kind, status))
	r.errorsN.Add(1)
	r.logf("emit "+kind, "%s %s: %d %v %s", method, path, status, err, truncate(string(data), 200))
	return nil
}

// ---------------------------------------------------------------------------------------------------------------
// Weighted choice ("a=3,b=1"; the weight is after the last '=')
// ---------------------------------------------------------------------------------------------------------------

type weighted struct {
	items []string
	cum   []float64
}

func parseMix(s string) (*weighted, error) {
	w := &weighted{}
	total := 0.0
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		i := strings.LastIndexByte(part, '=')
		if i < 0 {
			return nil, fmt.Errorf("mix item %q: want name=weight", part)
		}
		v, err := strconv.ParseFloat(part[i+1:], 64)
		if err != nil || v < 0 {
			return nil, fmt.Errorf("mix item %q: bad weight", part)
		}
		if v == 0 {
			continue
		}
		total += v
		w.items = append(w.items, part[:i])
		w.cum = append(w.cum, total)
	}
	if len(w.items) == 0 {
		return nil, fmt.Errorf("mix %q is empty", s)
	}
	return w, nil
}

func (w *weighted) pick() string {
	x := mrand.Float64() * w.cum[len(w.cum)-1]
	i := sort.SearchFloat64s(w.cum, x)
	if i >= len(w.items) {
		i = len(w.items) - 1
	}
	return w.items[i]
}

func (w *weighted) weight(name string) float64 {
	prev := 0.0
	for i, item := range w.items {
		if item == name {
			return w.cum[i] - prev
		}
		prev = w.cum[i]
	}
	return 0
}

// ---------------------------------------------------------------------------------------------------------------
// Open-loop schedules: requests start on time whatever the answers do
// ---------------------------------------------------------------------------------------------------------------

func openLoop(ctx context.Context, rate float64, from, until time.Time, fn func()) {
	if rate <= 0 || !until.After(from) {
		return
	}
	if !sleepUntil(ctx, from) {
		return
	}
	interval := time.Duration(float64(time.Second) / rate)
	next := from
	for {
		next = next.Add(interval)
		if next.After(until) || !sleepUntil(ctx, next) {
			return
		}
		fn()
	}
}

func (r *runner) restLoop(ctx context.Context) {
	mix, _ := parseMix(r.cfg.restMix)
	var rr atomic.Int64
	openLoop(ctx, r.cfg.restRate, r.start, r.end, func() {
		if r.restInflight.Load() >= int64(r.cfg.restMaxInflight) {
			r.m.inc("rest_status.dropped_max_inflight")
			return
		}
		var b *browser
		for try := 0; try < len(r.browsers); try++ {
			c := r.browsers[int(rr.Add(1))%len(r.browsers)]
			if c.uid.Load() > 0 {
				b = c
				break
			}
		}
		if b == nil {
			r.m.inc("rest_status.no_browser_logged_in")
			return
		}
		path := mix.pick()
		r.restInflight.Add(1)
		go func() {
			defer r.restInflight.Add(-1)
			rctx, cancel := context.WithTimeout(ctx, r.cfg.requestTimeout)
			defer cancel()
			t0 := time.Now()
			status, _, err := b.do(rctx, http.MethodGet, path, "", nil, false, false)
			d := time.Since(t0)
			r.restDone.Add(1)
			if err != nil {
				if ctx.Err() != nil {
					return
				}
				if r.inAbortWindow() {
					r.m.inc("rest_status.error_in_abort")
					return
				}
				r.m.inc("rest_status.error")
				r.errorsN.Add(1)
				r.logf("rest error", "%s: %v", path, err)
				return
			}
			r.m.h("rest.all").add(d)
			r.m.h("rest." + path).add(d)
			r.m.inc("rest_status." + strconv.Itoa(status))
			r.m.inc(fmt.Sprintf("rest_status_by_path.%s %d", path, status))
			if status >= 500 {
				r.errorsN.Add(1)
			}
		}()
	})
}

// ---------------------------------------------------------------------------------------------------------------
// Abort: drop every connection of every browser at once, as a network failure would
// ---------------------------------------------------------------------------------------------------------------

func (r *runner) abort() {
	now := time.Now().UnixNano()
	r.abortPending.Store(int64(len(r.streams)))
	for _, s := range r.streams {
		s.abortedAt.Store(now)
	}
	r.abortTime.Store(now)
	open := r.open.Load()
	n := 0
	for _, b := range r.browsers {
		n += b.tracker.dropAll()
	}
	r.abortDropped.Store(int64(n))
	log.Printf("ABORT at %s: dropped %d connections of %d browsers (%d streams were open)",
		time.Duration(now-r.start.UnixNano()).Round(time.Millisecond), n, len(r.browsers), open)
}

// ---------------------------------------------------------------------------------------------------------------
// Status line and summary
// ---------------------------------------------------------------------------------------------------------------

func (r *runner) statusLoop(ctx context.Context) {
	t := time.NewTicker(r.cfg.statusEvery)
	defer t.Stop()
	var lastEv, lastRefetch, lastRest, lastEmit int64
	last := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		now := time.Now()
		sec := now.Sub(last).Seconds()
		last = now
		ev, rf, rs, em := r.domainEvents.Load(), r.refetchDone.Load(), r.restDone.Load(), r.emitDone.Load()
		var ms runtime.MemStats
		runtime.ReadMemStats(&ms)
		if ms.HeapAlloc > r.maxHeap.Load() {
			r.maxHeap.Store(ms.HeapAlloc)
		}
		log.Printf("[%5.0fs] streams %d/%d open (ever %d) | events %.1f/s | refetch %.1f/s (pending %d) | rest %.1f/s p95 %.0fms | emit %.1f/s | lag p95 %.0fms | reconnects %d | relogins %d | errors %d | heap %d MB",
			now.Sub(r.start).Seconds(), r.open.Load(), len(r.streams), r.everOpen.Load(),
			float64(ev-lastEv)/sec, float64(rf-lastRefetch)/sec, r.refetchPending.Load(),
			float64(rs-lastRest)/sec, r.m.h("rest.all").quantile(0.95), float64(em-lastEmit)/sec,
			r.m.h("lag.all").quantile(0.95), r.reconnects.Load(), r.relogins.Load(), r.errorsN.Load(), ms.HeapAlloc>>20)
		lastEv, lastRefetch, lastRest, lastEmit = ev, rf, rs, em
	}
}

func (r *runner) summary(cfgView map[string]any) map[string]any {
	counters, hists := r.m.nested()
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	m := r.matcher
	m.mu.Lock()
	deliveries := map[string]any{
		"window_s":             r.cfg.deliverWithin.Seconds(),
		"expect":               r.cfg.expect,
		"writes_checked":       m.checked,
		"writes_failed":        m.failed,
		"writes_without_event": m.noEvent,
		"writes_unchecked":     m.unchecked,
		"expected":             m.expectedN,
		"delivered":            m.delivered,
		"missed":               m.missed,
		"excused_after_reset":  m.excused,
		"covered_by_later":     m.conflatedN,
		"missed_then_late":     m.lateN,
		"missed_samples":       m.samples,
	}
	m.mu.Unlock()
	allOpen := any(nil)
	if v := r.allOpenAfter.Load(); v > 0 {
		allOpen = round1(time.Duration(v).Seconds())
	}
	abort := map[string]any{"at_s": r.cfg.abortAt.Seconds()}
	if r.abortTime.Load() != 0 {
		abort["connections_dropped"] = r.abortDropped.Load()
		abort["streams_not_back"] = r.abortPending.Load()
		if v := r.abortAllBack.Load(); v > 0 {
			abort["all_back_after_s"] = round1(time.Duration(v).Seconds())
		} else {
			abort["all_back_after_s"] = nil
		}
	}
	return map[string]any{
		"config":     cfgView,
		"started_at": r.start.UTC().Format(time.RFC3339),
		"duration_s": round1(time.Since(r.start).Seconds()),
		"streams": map[string]any{
			"target":           len(r.streams),
			"ever_open":        r.everOpen.Load(),
			"open_at_end":      r.open.Load(),
			"all_open_after_s": allOpen,
			"reconnects":       r.reconnects.Load(),
		},
		"errors":     r.errorsN.Load(),
		"relogins":   r.relogins.Load(),
		"abort":      abort,
		"deliveries": deliveries,
		"counters":   counters,
		"latency":    hists,
		"generator": map[string]any{
			"heap_max_mb": r.maxHeap.Load() >> 20,
			"heap_end_mb": ms.HeapAlloc >> 20,
			"sys_mb":      ms.Sys >> 20,
			"goroutines":  runtime.NumGoroutine(),
			"go":          runtime.Version(),
			"cpus":        runtime.NumCPU(),
		},
	}
}

// ---------------------------------------------------------------------------------------------------------------
// run
// ---------------------------------------------------------------------------------------------------------------

func runMain(args []string) error {
	c := &config{}
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	commonFlags(fs, c)
	fs.StringVar(&c.accountsDir, "accounts", "accounts", "directory with one subdirectory per account (cert.pem, key.pem, optional login.json)")
	fs.BoolVar(&c.reuse, "reuse-accounts", false, "allow fewer accounts than browsers (accounts are cycled; nginx allows 32 streams per certificate)")
	fs.IntVar(&c.browsers, "browsers", 20, "simulated browsers (N): one account and one transport each")
	fs.IntVar(&c.streams, "streams", 3, "streams (tabs) per browser (K)")
	fs.DurationVar(&c.ramp, "ramp", 30*time.Second, "time over which the N x K streams open")
	fs.DurationVar(&c.hold, "hold", 2*time.Minute, "time to hold after the ramp")
	fs.Float64Var(&c.emitRate, "emit-rate", 2, "emitter writes per second (0: none)")
	fs.StringVar(&c.emitMix, "emit-mix", "post=4,tag=2,guide=1,user=2", "emitter mix: post (archive/restore of its own posts: scope.changed), tag (update: dictionary.changed tags), guide (update: dictionary.changed guides), user (alias of a load user: user.changed)")
	fs.IntVar(&c.emitPosts, "emit-posts", 10, "posts the emitter creates and toggles")
	fs.StringVar(&c.expect, "expect", "guide,user", "emitter kinds whose delivery is checked: guide (every stream), user (the user's own streams), tag (every stream holds tag.view), post (every stream sees the emitter's posts: users without a home post)")
	fs.DurationVar(&c.deliverWithin, "deliver-within", 30*time.Second, "an expected event not received within this is missed")
	fs.Float64Var(&c.restRate, "rest-rate", 10, "open-loop REST requests per second (0: none)")
	fs.StringVar(&c.restMix, "rest-mix", defaultRestMix, "REST mix: path=weight,...")
	fs.IntVar(&c.restMaxInflight, "rest-max-inflight", 2000, "REST requests in flight above which a scheduled one is dropped (counted)")
	fs.Float64Var(&c.refetchRatio, "refetch-ratio", 1, "fraction of domain events that cause a refetch (1: every one; a page refetches only what is on screen)")
	fs.IntVar(&c.refetchMaxInflight, "refetch-max-inflight", 5000, "refetches in flight above which one is dropped (counted)")
	fs.DurationVar(&c.abortAt, "abort-at", 0, "drop every connection at this time since the start (0: never)")
	fs.StringVar(&c.out, "out", "events_load_summary.json", "file for the final JSON summary")
	fs.DurationVar(&c.statusEvery, "status-every", 10*time.Second, "period of the status line")
	fs.DurationVar(&c.connectTimeout, "connect-timeout", 120*time.Second, "watchdog for the answer to a connect (nginx delays connects)")
	fs.DurationVar(&c.silence, "silence", 65*time.Second, "watchdog for a connected stream without a byte")
	fs.BoolVar(&c.cleanup, "cleanup", true, "archive the emitter's posts, tag and guide at the end")
	_ = fs.Parse(args)
	if err := c.finish(); err != nil {
		return err
	}
	mix, err := parseMix(c.emitMix)
	if err != nil {
		return err
	}
	for _, k := range mix.items {
		if k != "post" && k != "tag" && k != "guide" && k != "user" {
			return fmt.Errorf("-emit-mix: unknown kind %q", k)
		}
	}
	if _, err := parseMix(c.restMix); err != nil {
		return err
	}
	roots, err := loadRoots(c)
	if err != nil {
		return err
	}
	accounts, err := loadAccounts(c)
	if err != nil {
		return err
	}
	if len(accounts) == 0 {
		return fmt.Errorf("no accounts in %s", c.accountsDir)
	}
	if len(accounts) < c.browsers && !c.reuse {
		return fmt.Errorf("%d accounts for %d browsers (-reuse-accounts to cycle them)", len(accounts), c.browsers)
	}
	adminAcct, err := adminAccount(c)
	if err != nil {
		return err
	}

	r := &runner{cfg: c, roots: roots, logCounts: map[string]int{}, expect: map[string]bool{}}
	for _, k := range strings.Split(c.expect, ",") {
		if k = strings.TrimSpace(k); k != "" {
			r.expect[k] = true
		}
	}
	r.matcher = newMatcher(r)
	r.wheel = newWheel(65*time.Second, 50*time.Millisecond)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	if err := checkHealth(ctx, c, roots, adminAcct); err != nil {
		return err
	}
	var em *emitter
	if c.emitRate > 0 {
		em = &emitter{r: r, adm: newBrowser(r, -1, adminAcct), stamp: strconv.FormatInt(time.Now().Unix(), 36), mix: mix}
		if err := em.setup(ctx); err != nil {
			return err
		}
	}

	total := c.browsers * c.streams
	r.browsers = make([]*browser, c.browsers)
	r.streams = make([]*stream, 0, total)
	r.start = time.Now().Add(time.Second)
	r.end = r.start.Add(c.ramp + c.hold)
	for i := range r.browsers {
		b := newBrowser(r, i, accounts[i%len(accounts)])
		for j := 0; j < c.streams; j++ {
			g := len(r.streams)
			s := &stream{b: b, id: g, retry: 3 * time.Second,
				startAt: r.start.Add(time.Duration(float64(c.ramp) * float64(g) / float64(total)))}
			b.streams = append(b.streams, s)
			r.streams = append(r.streams, s)
		}
		r.browsers[i] = b
	}
	log.Printf("run: %d browsers x %d streams = %d, ramp %s, hold %s, emit %.1f/s (%s), rest %.1f/s, abort at %s, base %s (%s, login %s)",
		c.browsers, c.streams, total, c.ramp, c.hold, c.emitRate, c.emitMix, c.restRate, c.abortAt, c.base, c.mode, c.login)

	runCtx, cancelRun := context.WithCancel(ctx)
	var wg sync.WaitGroup
	var bg sync.WaitGroup
	bg.Add(3)
	go func() { defer bg.Done(); r.wheel.run(runCtx, func(j job) { r.fireRefetch(runCtx, j) }) }()
	go func() { defer bg.Done(); r.statusLoop(runCtx) }()
	go func() {
		defer bg.Done()
		t := time.NewTicker(time.Second)
		defer t.Stop()
		for {
			select {
			case <-runCtx.Done():
				return
			case now := <-t.C:
				r.matcher.finalize(now, false)
			}
		}
	}()
	for _, b := range r.browsers {
		go b.startAndRun(runCtx, &wg)
	}
	bg.Add(1)
	go func() { defer bg.Done(); r.restLoop(runCtx) }()
	if em != nil {
		emitFrom, emitUntil := r.start.Add(c.ramp), r.end.Add(-c.deliverWithin)
		if !emitUntil.After(emitFrom) {
			log.Printf("emitter: hold %s is shorter than -deliver-within %s: no writes", c.hold, c.deliverWithin)
		}
		bg.Add(1)
		go func() {
			defer bg.Done()
			openLoop(runCtx, c.emitRate, emitFrom, emitUntil, func() { em.tick(runCtx) })
		}()
	}
	if c.abortAt > 0 {
		go func() {
			if sleepUntil(runCtx, r.start.Add(c.abortAt)) {
				r.abort()
			}
		}()
	}

	select {
	case <-ctx.Done():
		log.Printf("interrupted")
	case <-time.After(time.Until(r.end)):
	}
	time.Sleep(2 * time.Second) // the last writes' answers and deliveries
	r.matcher.finalize(time.Now(), true)
	cancelRun()
	wg.Wait()
	bg.Wait()
	if em != nil && c.cleanup {
		cctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		em.cleanup(cctx)
		cancel()
	}

	cfgView := map[string]any{
		"base": c.base, "mode": c.mode, "login": c.login, "browsers": c.browsers, "streams_per_browser": c.streams,
		"accounts": len(accounts), "ramp_s": c.ramp.Seconds(), "hold_s": c.hold.Seconds(), "emit_rate": c.emitRate,
		"emit_mix": c.emitMix, "rest_rate": c.restRate, "rest_mix": c.restMix, "refetch_ratio": c.refetchRatio,
		"abort_at_s": c.abortAt.Seconds(), "expect": c.expect,
	}
	sum := r.summary(cfgView)
	data, _ := json.MarshalIndent(sum, "", "  ")
	if err := os.WriteFile(c.out, data, 0o644); err != nil {
		return err
	}
	printVerdict(sum)
	log.Printf("summary written to %s", c.out)
	return nil
}

func printVerdict(sum map[string]any) {
	st := sum["streams"].(map[string]any)
	d := sum["deliveries"].(map[string]any)
	log.Printf("streams: target %v, ever open %v, open at end %v, all open after %v s, reconnects %v",
		st["target"], st["ever_open"], st["open_at_end"], st["all_open_after_s"], st["reconnects"])
	log.Printf("deliveries: writes checked %v (without event %v, failed %v), expected %v, delivered %v, covered by a later event %v, missed %v (of which arrived later %v), excused %v",
		d["writes_checked"], d["writes_without_event"], d["writes_failed"], d["expected"], d["delivered"], d["covered_by_later"], d["missed"], d["missed_then_late"], d["excused_after_reset"])
	if lat, ok := sum["latency"].(map[string]map[string]any); ok {
		names := make([]string, 0)
		for name := range lat["lag"] {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			log.Printf("lag %-28s %v", name, lat["lag"][name])
		}
		if v, ok := lat["lag_replay"]["all"]; ok {
			log.Printf("lag of replayed events (after a reconnect) %v", v)
		}
		if v, ok := lat["rest"]["all"]; ok {
			log.Printf("rest all %v", v)
		}
	}
	log.Printf("abort: %v | errors %v | relogins %v", sum["abort"], sum["errors"], sum["relogins"])
}

func checkHealth(ctx context.Context, c *config, roots *x509.CertPool, a *account) error {
	tr, _ := newTransport(c, a, roots)
	client := &http.Client{Transport: tr, Timeout: 15 * time.Second}
	defer tr.CloseIdleConnections()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.base+pathHealth, nil)
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("health: %w", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var h struct {
		Status string         `json:"status"`
		Events map[string]any `json:"events"`
	}
	_ = json.Unmarshal(data, &h)
	log.Printf("health: %d %s over %s; events part: %v", resp.StatusCode, h.Status, resp.Proto, h.Events)
	return nil
}

// ---------------------------------------------------------------------------------------------------------------
// prepare: create load users through the API and write their account directories
// ---------------------------------------------------------------------------------------------------------------

func prepareMain(args []string) error {
	c := &config{}
	fs := flag.NewFlagSet("prepare", flag.ExitOnError)
	commonFlags(fs, c)
	out := fs.String("out", "accounts", "directory to write the accounts into (one subdirectory per user)")
	count := fs.Int("count", 20, "users to create")
	prefix := fs.String("prefix", "load", "username prefix (usernames: <prefix>_<stamp>_<n>)")
	userType := fs.String("user-type", "operator", "user type of the new users (GET /user-types); no home post: the base role sits at the root")
	postID := fs.Int64("post-id", 0, "home post of the new users (0: none)")
	_ = fs.Parse(args)
	if err := c.finish(); err != nil {
		return err
	}
	roots, err := loadRoots(c)
	if err != nil {
		return err
	}
	adminAcct, err := adminAccount(c)
	if err != nil {
		return err
	}
	r := &runner{cfg: c, roots: roots, logCounts: map[string]int{}}
	adm := newBrowser(r, -1, adminAcct)
	if err := adm.doLogin(); err != nil {
		return err
	}
	if err := os.MkdirAll(*out, 0o700); err != nil {
		return err
	}
	stamp := strconv.FormatInt(time.Now().Unix(), 36)
	ctx := context.Background()
	for i := 1; i <= *count; i++ {
		username := fmt.Sprintf("%s_%s_%03d", *prefix, stamp, i)
		dir := filepath.Join(*out, username)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return err
		}
		fields := map[string]string{"username": username, "username_alias": "Load user " + username, "user_type": *userType}
		if *postID > 0 {
			fields["post_id"] = strconv.FormatInt(*postID, 10)
		}
		var certPEM []byte
		if c.mode == "plain" {
			// A self-signed certificate: the development login matches its CN, serial and fingerprint
			var keyPEM []byte
			certPEM, keyPEM, err = selfSigned(username)
			if err != nil {
				return err
			}
			if err := os.WriteFile(filepath.Join(dir, "cert.pem"), certPEM, 0o600); err != nil {
				return err
			}
			if err := os.WriteFile(filepath.Join(dir, "key.pem"), keyPEM, 0o600); err != nil {
				return err
			}
		}
		body, ctype := multipartBody(fields, certPEM)
		status, data, err := adm.do(ctx, http.MethodPost, pathUsers, ctype, body, false, true)
		if err != nil || status != 200 {
			return fmt.Errorf("create %s: %d %v %s", username, status, err, truncate(string(data), 300))
		}
		uid := idOf(data)
		if c.mode == "mtls" {
			// The API generates the key; the answer carries the .p12 and its password (once: Cache-Control no-store)
			req, _ := json.Marshal(map[string]string{"label": username})
			status, data, err := adm.do(ctx, http.MethodPost, fmt.Sprintf(pathUserCertificates, uid), "application/json", req, false, true)
			if err != nil || (status != 200 && status != 201) {
				return fmt.Errorf("issue for %s: %d %v %s", username, status, err, truncate(string(data), 300))
			}
			var pkg struct {
				P12      string `json:"p12_base64"`
				Password string `json:"password"`
			}
			if err := json.Unmarshal(data, &pkg); err != nil {
				return err
			}
			p12, err := base64.StdEncoding.DecodeString(pkg.P12)
			if err != nil {
				return err
			}
			if err := os.WriteFile(filepath.Join(dir, "client.p12"), p12, 0o600); err != nil {
				return err
			}
			if err := os.WriteFile(filepath.Join(dir, "password.txt"), []byte(pkg.Password), 0o600); err != nil {
				return err
			}
		}
		log.Printf("created %s (user %d)", username, uid)
	}
	if c.mode == "mtls" {
		log.Printf("now turn every client.p12 into cert.pem + key.pem: sh p12_to_pem.sh %s", *out)
	}
	return nil
}

func multipartBody(fields map[string]string, certPEM []byte) ([]byte, string) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	keys := make([]string, 0, len(fields))
	for k := range fields {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		_ = w.WriteField(k, fields[k])
	}
	if certPEM != nil {
		part, _ := w.CreateFormFile("certificate", "cert.pem")
		_, _ = part.Write(certPEM)
	}
	_ = w.Close()
	return buf.Bytes(), w.FormDataContentType()
}

func selfSigned(cn string) ([]byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	if err != nil {
		return nil, nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial.Add(serial, big.NewInt(1)),
		Subject:      pkix.Name{CommonName: cn},
		NotBefore:    time.Now().Add(-5 * time.Minute),
		NotAfter:     time.Now().Add(2 * 365 * 24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, nil, err
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}), nil
}

// ---------------------------------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------------------------------

func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func sleepUntil(ctx context.Context, t time.Time) bool { return sleepCtx(ctx, time.Until(t)) }

func jitter(d time.Duration) time.Duration { return time.Duration(mrand.Float64() * float64(d)) }

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "..."
	}
	return s
}

func main() {
	log.SetFlags(log.Ltime | log.Lmicroseconds)
	args := os.Args[1:]
	cmd := "run"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd, args = args[0], args[1:]
	}
	var err error
	switch cmd {
	case "run":
		err = runMain(args)
	case "prepare":
		err = prepareMain(args)
	default:
		err = fmt.Errorf("unknown command %q (run, prepare)", cmd)
	}
	if err != nil {
		log.Fatalf("error: %v", err)
	}
}
