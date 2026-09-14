package store

import (
	"context"
	"sync"
	"testing"
)

func TestStableFileGrantSeparatesSubjectsAndResources(t *testing.T) {
	s := &Store{pool: testPool(t)}
	org := seedOrg(t, s)
	ctx := context.Background()
	first, err := s.StableGrantFor(ctx, org, "doc", "alice", "resource-a", "object", "application/pdf", "a.pdf")
	if err != nil {
		t.Fatal(err)
	}
	second, err := s.StableGrantFor(ctx, org, "doc", "bob", "resource-b", "object", "application/pdf", "b.pdf")
	if err != nil {
		t.Fatal(err)
	}
	third, err := s.StableGrantFor(ctx, org, "doc", "alice", "resource-c", "object", "application/pdf", "c.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if first.Token == second.Token || first.Token == third.Token {
		t.Fatal("capability crossed ownership boundary")
	}
	restored, err := s.FileGrantByToken(ctx, first.Token)
	if err != nil || restored.SubjectID != "alice" || restored.ResourceID != "resource-a" || restored.Filename != "a.pdf" {
		t.Fatalf("lost grant binding: %+v %v", restored, err)
	}
	if _, err := s.StableGrantFor(ctx, org, "missing", "alice", "missing", "", "", ""); err != ErrNotFound {
		t.Fatalf("read created empty grant: %v", err)
	}
	if _, err := s.StableGrantFor(ctx, org, "doc", "alice", "resource-a", "another-object", "application/pdf", ""); err == nil {
		t.Fatal("mutated fixed input")
	}
}

func TestSimultaneousGrantRequestsReturnOneToken(t *testing.T) {
	s := &Store{pool: testPool(t)}
	org := seedOrg(t, s)
	var group sync.WaitGroup
	tokens := make(chan string, 12)
	errors := make(chan error, 12)
	for range 12 {
		group.Go(func() {
			grant, err := s.StableGrantFor(context.Background(), org, "doc", "alice", "resource", "object", "application/pdf", "a.pdf")
			if err != nil {
				errors <- err
				return
			}
			tokens <- grant.Token
		})
	}
	group.Wait()
	close(tokens)
	close(errors)
	for err := range errors {
		t.Error(err)
	}
	seen := map[string]bool{}
	for token := range tokens {
		seen[token] = true
	}
	if len(seen) != 1 {
		t.Fatalf("one logical grant generated %d tokens", len(seen))
	}
}
