package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// ---------------------------------------------------------------- 组织

type Organization struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	Slug        string    `json:"slug"`
	CreatedAt   time.Time `json:"created_at"`
	MemberCount int       `json:"member_count"`
}

// DefaultOrganization 取（或创建）本次部署的默认组织。
//
// **首发是单组织独占部署**：一次部署服务一个组织，组织内成员共享语料。
// 这个方法让"组织"这个概念在单组织模式下不给任何人添麻烦，
// 同时让所有查询从第一天起就带着 organization_id —— 将来上多组织 SaaS 时，
// 要补的是隔离与 RLS，而不是给几十张表加列。
func (s *Store) DefaultOrganization(ctx context.Context) (*Organization, error) {
	org := &Organization{}
	err := s.pool.QueryRow(ctx, `
		INSERT INTO control.organizations (id, name, slug)
		VALUES ($1, '默认组织', 'default')
		ON CONFLICT (slug) DO UPDATE SET name = control.organizations.name
		RETURNING id, name, slug, created_at`,
		auth.NewID(),
	).Scan(&org.ID, &org.Name, &org.Slug, &org.CreatedAt)
	if err != nil {
		return nil, err
	}
	if err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM control.memberships WHERE organization_id = $1`,
		org.ID).Scan(&org.MemberCount); err != nil {
		return nil, err
	}
	return org, nil
}

// ---------------------------------------------------------------- 用户

type User struct {
	ID             string     `json:"id"`
	Username       string     `json:"username"`
	Email          *string    `json:"email"`
	Role           rbac.Role  `json:"role"`
	OrganizationID string     `json:"organization_id"`
	CreatedAt      time.Time  `json:"created_at"`
	LastSeenAt     *time.Time `json:"last_seen_at,omitempty"`

	passwordHash string
	active       bool
}

var ErrUsernameTaken = errors.New("username taken")

// CreateUser 建账号并加入组织，同一个事务。
//
// **第一个用户自动成为 admin**：否则一个全新部署会没有任何人能管理它，
// 只能改库。这条规则必须与"最后一个 admin 不能被降级"配套
// （见 SetMemberRole），两者共同保证组织**永远至少有一个 admin**。
func (s *Store) CreateUser(ctx context.Context, orgID, username, email, passwordHash string,
	defaultRole rbac.Role) (*User, error) {

	var u *User
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		var members int
		if err := tx.QueryRow(ctx,
			`SELECT count(*) FROM control.memberships WHERE organization_id = $1`,
			orgID).Scan(&members); err != nil {
			return err
		}
		role := defaultRole
		if members == 0 {
			role = rbac.Admin
		}

		id := auth.NewID()
		var emailArg any
		if email != "" {
			emailArg = email
		}
		row := tx.QueryRow(ctx, `
			INSERT INTO control.users (id, username, email, password_hash)
			VALUES ($1, $2, $3, $4)
			RETURNING id, username, email, created_at`,
			id, username, emailArg, passwordHash)

		u = &User{OrganizationID: orgID, Role: role, active: true}
		if err := row.Scan(&u.ID, &u.Username, &u.Email, &u.CreatedAt); err != nil {
			if isUniqueViolation(err) {
				return ErrUsernameTaken
			}
			return err
		}
		_, err := tx.Exec(ctx, `
			INSERT INTO control.memberships (organization_id, user_id, role)
			VALUES ($1, $2, $3)`, orgID, u.ID, string(role))
		return err
	})
	if err != nil {
		return nil, err
	}
	return u, nil
}

// UserByUsername 连同它在给定组织里的角色一起取。
// 没有 membership 的用户**取不到** —— "账号存在但不属于本组织"与
// "账号不存在"在登录接口上必须是同一个结果。
func (s *Store) UserByUsername(ctx context.Context, orgID, username string) (*User, error) {
	u := &User{OrganizationID: orgID}
	var role string
	err := s.pool.QueryRow(ctx, `
		SELECT u.id, u.username, u.email, u.password_hash, u.is_active, u.created_at, m.role
		FROM control.users u
		JOIN control.memberships m ON m.user_id = u.id AND m.organization_id = $1
		WHERE u.username = $2`, orgID, username).
		Scan(&u.ID, &u.Username, &u.Email, &u.passwordHash, &u.active, &u.CreatedAt, &role)
	if err != nil {
		return nil, norows(err)
	}
	parsed, err := rbac.Parse(role)
	if err != nil {
		return nil, err
	}
	u.Role = parsed
	return u, nil
}

func (s *Store) UserByID(ctx context.Context, orgID, userID string) (*User, error) {
	u := &User{OrganizationID: orgID}
	var role string
	err := s.pool.QueryRow(ctx, `
		SELECT u.id, u.username, u.email, u.password_hash, u.is_active, u.created_at, m.role
		FROM control.users u
		JOIN control.memberships m ON m.user_id = u.id AND m.organization_id = $1
		WHERE u.id = $2`, orgID, userID).
		Scan(&u.ID, &u.Username, &u.Email, &u.passwordHash, &u.active, &u.CreatedAt, &role)
	if err != nil {
		return nil, norows(err)
	}
	parsed, err := rbac.Parse(role)
	if err != nil {
		return nil, err
	}
	u.Role = parsed
	return u, nil
}

// UpsertOIDCUser 按 (issuer, subject) 找人；没有就建一个并加入组织。
// **subject 才是稳定标识**，email 会变、username 会重名。
func (s *Store) UpsertOIDCUser(ctx context.Context, orgID, issuer, subject, username, email string,
	defaultRole rbac.Role) (*User, error) {

	var userID string
	err := s.pool.QueryRow(ctx, `
		SELECT id FROM control.users WHERE oidc_issuer = $1 AND oidc_subject = $2`,
		issuer, subject).Scan(&userID)
	if err == nil {
		return s.UserByID(ctx, orgID, userID)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}

	var u *User
	err = s.InTx(ctx, func(tx pgx.Tx) error {
		var members int
		if err := tx.QueryRow(ctx,
			`SELECT count(*) FROM control.memberships WHERE organization_id = $1`,
			orgID).Scan(&members); err != nil {
			return err
		}
		role := defaultRole
		if members == 0 {
			role = rbac.Admin
		}
		id := auth.NewID()
		var emailArg any
		if email != "" {
			emailArg = email
		}
		u = &User{OrganizationID: orgID, Role: role, active: true}
		if err := tx.QueryRow(ctx, `
			INSERT INTO control.users (id, username, email, oidc_issuer, oidc_subject)
			VALUES ($1, $2, $3, $4, $5)
			RETURNING id, username, email, created_at`,
			id, username, emailArg, issuer, subject).
			Scan(&u.ID, &u.Username, &u.Email, &u.CreatedAt); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `
			INSERT INTO control.memberships (organization_id, user_id, role)
			VALUES ($1, $2, $3) ON CONFLICT DO NOTHING`, orgID, u.ID, string(role))
		return err
	})
	if err != nil {
		return nil, err
	}
	return u, nil
}

func (u *User) PasswordHash() string { return u.passwordHash }
func (u *User) Active() bool         { return u.active }

func (s *Store) TouchLastSeen(ctx context.Context, userID string) {
	// 尽力而为：这一条失败不该让请求失败
	_, _ = s.pool.Exec(ctx,
		`UPDATE control.users SET last_seen_at = now() WHERE id = $1`, userID)
}

// ---------------------------------------------------------------- 成员

type Member struct {
	UserID     string     `json:"user_id"`
	Username   string     `json:"username"`
	Email      *string    `json:"email"`
	Role       rbac.Role  `json:"role"`
	JoinedAt   time.Time  `json:"joined_at"`
	LastSeenAt *time.Time `json:"last_seen_at"`
}

func (s *Store) Members(ctx context.Context, orgID string) ([]Member, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT u.id, u.username, u.email, m.role, m.joined_at, u.last_seen_at
		FROM control.memberships m
		JOIN control.users u ON u.id = m.user_id
		WHERE m.organization_id = $1
		ORDER BY m.joined_at`, orgID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []Member{}
	for rows.Next() {
		var m Member
		var role string
		if err := rows.Scan(&m.UserID, &m.Username, &m.Email, &role, &m.JoinedAt, &m.LastSeenAt); err != nil {
			return nil, err
		}
		parsed, err := rbac.Parse(role)
		if err != nil {
			return nil, err
		}
		m.Role = parsed
		out = append(out, m)
	}
	return out, rows.Err()
}

// ErrLastAdmin 是"不能把最后一个 admin 降级或移除"。
//
// 没有这条的话，一次误操作就能让组织**永久失去管理能力**，
// 而恢复要直接改库。这是那种"看起来多余、真出事时救命"的约束。
var ErrLastAdmin = errors.New("organization must keep at least one admin")

func (s *Store) SetMemberRole(ctx context.Context, orgID, userID string, role rbac.Role) error {
	return s.InTx(ctx, func(tx pgx.Tx) error {
		// FOR UPDATE：两个管理员同时把对方降级时，没有锁的话两条都会成功
		var admins int
		if err := tx.QueryRow(ctx, `
			SELECT count(*) FROM control.memberships
			WHERE organization_id = $1 AND role = 'admin' FOR UPDATE`, orgID).Scan(&admins); err != nil {
			return err
		}
		var current string
		if err := tx.QueryRow(ctx, `
			SELECT role FROM control.memberships
			WHERE organization_id = $1 AND user_id = $2`, orgID, userID).Scan(&current); err != nil {
			return norows(err)
		}
		if current == string(rbac.Admin) && role != rbac.Admin && admins <= 1 {
			return ErrLastAdmin
		}
		_, err := tx.Exec(ctx, `
			UPDATE control.memberships SET role = $3
			WHERE organization_id = $1 AND user_id = $2`, orgID, userID, string(role))
		return err
	})
}

func (s *Store) RemoveMember(ctx context.Context, orgID, userID string) error {
	return s.InTx(ctx, func(tx pgx.Tx) error {
		var admins int
		if err := tx.QueryRow(ctx, `
			SELECT count(*) FROM control.memberships
			WHERE organization_id = $1 AND role = 'admin' FOR UPDATE`, orgID).Scan(&admins); err != nil {
			return err
		}
		var current string
		if err := tx.QueryRow(ctx, `
			SELECT role FROM control.memberships
			WHERE organization_id = $1 AND user_id = $2`, orgID, userID).Scan(&current); err != nil {
			return norows(err)
		}
		if current == string(rbac.Admin) && admins <= 1 {
			return ErrLastAdmin
		}
		_, err := tx.Exec(ctx, `
			DELETE FROM control.memberships WHERE organization_id = $1 AND user_id = $2`,
			orgID, userID)
		return err
	})
}

// AddMember 把一个已有账号加进组织。
func (s *Store) AddMember(ctx context.Context, orgID, username string, role rbac.Role) (*Member, error) {
	var m Member
	m.Role = role
	err := s.pool.QueryRow(ctx, `
		WITH target AS (SELECT id, username, email FROM control.users WHERE username = $2),
		     ins AS (
		       INSERT INTO control.memberships (organization_id, user_id, role)
		       SELECT $1, id, $3 FROM target
		       ON CONFLICT (organization_id, user_id) DO UPDATE SET role = EXCLUDED.role
		       RETURNING user_id, joined_at
		     )
		SELECT t.id, t.username, t.email, i.joined_at
		FROM ins i JOIN target t ON t.id = i.user_id`,
		orgID, username, string(role)).
		Scan(&m.UserID, &m.Username, &m.Email, &m.JoinedAt)
	if err != nil {
		return nil, norows(err)
	}
	return &m, nil
}
