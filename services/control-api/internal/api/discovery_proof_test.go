package api

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
)

func proofServer(t *testing.T) *Server {
	t.Helper()
	node, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "node"), true)
	if err != nil {
		t.Fatal(err)
	}
	return &Server{cfg: &config.Config{PublicBaseURL: "https://configured.example/center"}, nodeIdentity: node, nodeRevision: 1}
}
func TestNodeProofBindsNonceAndConfiguredEndpointOnly(t *testing.T) {
	s := proofServer(t)
	nonce := base64.RawURLEncoding.EncodeToString([]byte(strings.Repeat("n", 24)))
	r := httptest.NewRequest("GET", "https://evil.example/api/v1/federation/node?challenge="+nonce, nil)
	r.Host = "evil.example"
	r.Header.Set("Forwarded", "host=evil.example;proto=http")
	r.Header.Set("X-Forwarded-Host", "evil.example")
	w := httptest.NewRecorder()
	httpx.Wrap(s.handleFederationNode).ServeHTTP(w, r)
	body := decodeDiscovery[struct {
		PublicKey string              `json:"public_key"`
		Proof     discovery.NodeProof `json:"proof"`
	}](t, w, 200)
	p := body.Proof
	if p.Endpoint != "https://configured.example/center" || p.Nonce != nonce || p.NodeID != s.nodeIdentity.NodeID() {
		t.Fatalf("caller controlled signed authority: %+v", p)
	}
	issued, err := time.Parse(time.RFC3339, p.IssuedAt)
	if err != nil {
		t.Fatal(err)
	}
	expires, err := time.Parse(time.RFC3339, p.ExpiresAt)
	if err != nil || expires.Sub(issued) != time.Minute {
		t.Fatal("invalid proof lifetime")
	}
	payload, err := json.Marshal([]string{p.Schema, p.Nonce, p.NodeID, p.Endpoint, p.IssuedAt, p.ExpiresAt})
	if err != nil {
		t.Fatal(err)
	}
	pub, err := base64.StdEncoding.DecodeString(body.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	signature, err := base64.RawURLEncoding.DecodeString(p.Signature)
	if err != nil {
		t.Fatal(err)
	}
	if !ed25519.Verify(pub, payload, signature) {
		t.Fatal("invalid Ed25519 proof")
	}
	for _, bad := range []string{"", "short", strings.Repeat("A", 65), nonce + "=", nonce + "&challenge=" + nonce} {
		r := httptest.NewRequest("GET", "/api/v1/federation/node?challenge="+bad, nil)
		w := httptest.NewRecorder()
		httpx.Wrap(s.handleFederationNode).ServeHTTP(w, r)
		if w.Code != 400 {
			t.Fatalf("invalid challenge accepted: %q => %d", bad, w.Code)
		}
	}
}
func TestGoNodeProofVerifiedByRealTypeScriptProvider(t *testing.T) {
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("Node runtime missing; Go-to-TypeScript proof integration requires Node >=22")
	}
	version, err := exec.Command(node, "-p", "Number(process.versions.node.split('.')[0])>=22").Output()
	if err != nil || strings.TrimSpace(string(version)) != "true" {
		t.Skip("Node >=22 required for direct TypeScript protocol integration")
	}
	s := proofServer(t)
	var requested atomic.Bool
	target := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requested.Store(true)
		httpx.Wrap(s.handleFederationNode).ServeHTTP(w, r)
	}))
	s.cfg.PublicBaseURL = "http://" + target.Listener.Addr().String() + "/center&docs"
	target.Start()
	defer target.Close()
	module, err := filepath.Abs("../../../../packages/client-runtime/src/http-provider.ts")
	if err != nil {
		t.Fatal(err)
	}
	script := `import {pathToFileURL} from 'node:url';
 const {HttpProvider}=await import(pathToFileURL(process.argv[1]).href);
 let credentials=0;
 const provider=new HttpProvider({ownedLoopback:true,credential:async()=>{credentials++;throw Error('proof must not read credentials')}});
 const environment={environmentId:process.argv[3],authorityNodeId:process.argv[3],workspaceId:'private-workspace',endpoint:process.argv[2]};
 const value=await provider.inspect(environment,new AbortController().signal);
 if(value.authorityNodeId!==environment.authorityNodeId||credentials!==0)throw Error('identity protocol mismatch');
 process.stdout.write('verified');`
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, node, "--experimental-strip-types", "--input-type=module", "-e", script, module, s.cfg.PublicBaseURL, s.nodeIdentity.NodeID()).CombinedOutput()
	if err != nil {
		t.Fatalf("Go↔TS node proof failed: %v %s", err, output)
	}
	if !requested.Load() || !strings.Contains(string(output), "verified") {
		t.Fatal("provider did not verify real Go response")
	}
}
