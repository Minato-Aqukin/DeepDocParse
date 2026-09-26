package store

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

func seedMembershipUser(t *testing.T, s *Store, org string, role rbac.Role) *User {
	t.Helper()
	u, err := s.CreateUser(context.Background(), org, "member-test-"+auth.NewID(), "", "unused-password-hash", role)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx := context.Background()
		if _, err := s.pool.Exec(ctx, "DELETE FROM control.memberships WHERE user_id=$1", u.ID); err != nil {
			t.Error(err)
		}
		if _, err := s.pool.Exec(ctx, "DELETE FROM control.users WHERE id=$1", u.ID); err != nil {
			t.Error(err)
		}
	})
	return u
}

func TestMembershipChangesPreserveLastAdministrator(t *testing.T) {
	s := &Store{pool: testPool(t)}
	org := seedOrg(t, s)
	admin := seedMembershipUser(t, s, org, rbac.Admin)
	member := seedMembershipUser(t, s, org, rbac.Contributor)
	ctx := context.Background()

	if err := s.SetMemberRole(ctx, org, member.ID, rbac.Viewer); err != nil {
		t.Fatal(err)
	}
	changed, err := s.UserByUsername(ctx, org, member.Username)
	if err != nil || changed.Role != rbac.Viewer {
		t.Fatalf("role change did not affect the usable identity: user=%+v err=%v", changed, err)
	}
	if err := s.RemoveMember(ctx, org, member.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := s.UserByUsername(ctx, org, member.Username); !errors.Is(err, ErrNotFound) {
		t.Fatalf("removed membership still resolves as an organization identity: %v", err)
	}

	if err := s.SetMemberRole(ctx, org, admin.ID, rbac.Viewer); !errors.Is(err, ErrLastAdmin) {
		t.Fatalf("last administrator demotion: %v", err)
	}
	if err := s.RemoveMember(ctx, org, admin.ID); !errors.Is(err, ErrLastAdmin) {
		t.Fatalf("last administrator removal: %v", err)
	}
	if _, err := s.AddMember(ctx, org, admin.Username, rbac.Viewer); !errors.Is(err, ErrLastAdmin) {
		t.Fatalf("existing-member upsert bypassed last-administrator protection: %v", err)
	}
	remaining, err := s.UserByUsername(ctx, org, admin.Username)
	if err != nil || remaining.Role != rbac.Admin {
		t.Fatalf("administrator identity was lost: user=%+v err=%v", remaining, err)
	}
}

func TestConcurrentMembershipChangesKeepAnAdministrator(t *testing.T) {
	s := &Store{pool: testPool(t)}
	org := seedOrg(t, s)
	first := seedMembershipUser(t, s, org, rbac.Admin)
	second := seedMembershipUser(t, s, org, rbac.Admin)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	start := make(chan struct{})
	results := make(chan error, 2)
	for _, change := range []func() error{
		func() error { return s.SetMemberRole(ctx, org, first.ID, rbac.Viewer) },
		func() error { return s.RemoveMember(ctx, org, second.ID) },
	} {
		go func() {
			<-start
			results <- change()
		}()
	}
	close(start)
	changed, protected := 0, 0
	for range 2 {
		err := <-results
		switch {
		case err == nil:
			changed++
		case errors.Is(err, ErrLastAdmin):
			protected++
		default:
			t.Fatalf("concurrent membership change failed: %v", err)
		}
	}
	if changed != 1 || protected != 1 {
		t.Fatalf("both administrator mutations must not commit: changed=%d protected=%d", changed, protected)
	}
	members, err := s.Members(ctx, org)
	if err != nil {
		t.Fatal(err)
	}
	admins := 0
	for _, member := range members {
		if member.Role == rbac.Admin {
			admins++
		}
	}
	if admins != 1 {
		t.Fatalf("organization must retain exactly one administrator after the race, got %d", admins)
	}
}
