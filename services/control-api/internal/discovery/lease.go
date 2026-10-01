package discovery

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"time"
)

const MaxRenewedLeaseDuration = 10 * time.Minute

func asciiDescriptorStrings(d NodeDescriptor) bool {
	isASCII := func(value string) bool {
		for i := range len(value) {
			if value[i] > 127 {
				return false
			}
		}
		return true
	}
	if !isASCII(d.Schema) || !isASCII(d.NodeID) {
		return false
	}
	for _, value := range d.ProtocolVersions {
		if !isASCII(value) {
			return false
		}
	}
	for _, value := range d.AuthMethods {
		if !isASCII(value) {
			return false
		}
	}
	for _, endpoint := range d.ControlledEndpoints {
		if !isASCII(endpoint.Purpose) || !isASCII(endpoint.URL) {
			return false
		}
	}
	return true
}

func publisherSignatureBytes(value string) ([]byte, error) {
	if len(value) != base64.StdEncoding.EncodedLen(ed25519.SignatureSize) {
		return nil, errors.New("invalid descriptor signature")
	}
	signature, err := base64.StdEncoding.Strict().DecodeString(value)
	if err != nil || len(signature) != ed25519.SignatureSize ||
		base64.StdEncoding.EncodeToString(signature) != value {
		return nil, errors.New("invalid descriptor signature")
	}
	return signature, nil
}

// descriptorSigningBytes binds the lease and every configuration field. Arrays
// avoid object-key ordering differences across implementations.
func descriptorSigningBytes(d NodeDescriptor) ([]byte, error) {
	if d.ValidUntil.Nanosecond() != 0 || !asciiDescriptorStrings(d) {
		return nil, errors.New("signed descriptor requires whole-second time and ASCII strings")
	}
	endpoints := make([][2]string, len(d.ControlledEndpoints))
	for i, endpoint := range d.ControlledEndpoints {
		endpoints[i] = [2]string{endpoint.Purpose, endpoint.URL}
	}
	var body bytes.Buffer
	body.WriteString("ddp-node-descriptor/1\n")
	enc := json.NewEncoder(&body)
	enc.SetEscapeHTML(false)
	err := enc.Encode([]any{d.Schema, d.NodeID, d.ProtocolVersions, endpoints, d.AuthMethods, d.DiscoveryCapabilities.EnumerateMembers, d.DiscoveryCapabilities.CatalogEvents, d.Revision, d.ValidUntil.UTC().Format(time.RFC3339)})
	return bytes.TrimSuffix(body.Bytes(), []byte("\n")), err
}

func (i *Identity) SignDescriptor(d *NodeDescriptor) error {
	if d.NodeID != i.NodeID() {
		return errors.New("descriptor identity mismatch")
	}
	d.ValidUntil = d.ValidUntil.UTC().Truncate(time.Second)
	payload, err := descriptorSigningBytes(*d)
	if err != nil {
		return err
	}
	d.PublisherSignature = i.Sign(payload)
	return nil
}

func (d NodeDescriptor) VerifyPublisher(publicKey string) error {
	key, err := base64.StdEncoding.Strict().DecodeString(publicKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return errors.New("invalid descriptor public key")
	}
	sig, err := publisherSignatureBytes(d.PublisherSignature)
	if err != nil {
		return err
	}
	payload, err := descriptorSigningBytes(d)
	if err != nil {
		return err
	}
	if !ed25519.Verify(key, payload, sig) {
		return errors.New("descriptor signature mismatch")
	}
	return nil
}

// ValidateLease applies the bounded freshness contract at both fetch and commit.
func (d NodeDescriptor) ValidateLease(publicKey, localID string, now time.Time) error {
	if err := d.Validate(publicKey, localID, now); err != nil {
		return err
	}
	if d.ValidUntil.After(now.Add(MaxRenewedLeaseDuration)) {
		return errors.New("renewed descriptor expiry exceeds maximum lease duration")
	}
	return d.VerifyPublisher(publicKey)
}

// SameDescriptorConfiguration excludes only lease expiry and its signature.
func SameDescriptorConfiguration(a, b NodeDescriptor) bool {
	a.ValidUntil, b.ValidUntil = time.Time{}, time.Time{}
	a.PublisherSignature, b.PublisherSignature = "", ""
	return reflect.DeepEqual(a, b)
}

func NewLeaseClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &http.Client{Transport: transport, Timeout: 2 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
}

// FetchLease uses the approved endpoint, never any address in the response.
func FetchLease(ctx context.Context, client *http.Client, approved NodeDescriptor, publicKey, localID string) (NodeDescriptor, error) {
	endpoint := ""
	for _, e := range approved.ControlledEndpoints {
		if e.Purpose != "federation" {
			continue
		}
		if endpoint != "" || !ValidBaseURL(e.URL) {
			return NodeDescriptor{}, errors.New("ambiguous or invalid approved federation endpoint")
		}
		endpoint = strings.TrimRight(e.URL, "/") + "/node"
	}
	if endpoint == "" {
		return NodeDescriptor{}, errors.New("approved federation endpoint missing")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return NodeDescriptor{}, err
	}
	response, err := client.Do(req)
	if err != nil {
		return NodeDescriptor{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return NodeDescriptor{}, fmt.Errorf("descriptor HTTP status %d", response.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, 64*1024+1))
	if err != nil {
		return NodeDescriptor{}, err
	}
	if len(raw) > 64*1024 {
		return NodeDescriptor{}, errors.New("descriptor response too large")
	}
	var body struct {
		AuthorityNodeID string         `json:"authority_node_id"`
		PublicKey       string         `json:"public_key"`
		Descriptor      NodeDescriptor `json:"descriptor"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		return NodeDescriptor{}, err
	}
	if body.AuthorityNodeID != approved.NodeID || body.PublicKey != publicKey {
		return NodeDescriptor{}, errors.New("descriptor approved identity or key mismatch")
	}
	d := body.Descriptor
	if err := d.ValidateLease(publicKey, localID, time.Now()); err != nil {
		return NodeDescriptor{}, err
	}
	if !SameDescriptorConfiguration(approved, d) || !d.ValidUntil.After(approved.ValidUntil) {
		return NodeDescriptor{}, errors.New("descriptor configuration changed or lease did not increase")
	}
	return d, nil
}
