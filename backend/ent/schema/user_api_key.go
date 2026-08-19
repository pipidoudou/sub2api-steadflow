// Package schema - user_api_key.go
//
// 依据: 架构方案 v2.2 §4.3.3 + §4.3.4
// 对应 SQL: console/backend/migrations/20260708_distributor_and_apikeys.sql 第 3 节
package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// UserAPIKey 持有用户级 API key（console 全新概念）。
type UserAPIKey struct {
	ent.Schema
}

func (UserAPIKey) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "user_api_keys"},
	}
}

func (UserAPIKey) Fields() []ent.Field {
	return []ent.Field{
		field.Int64("user_id").
			Comment("关联 users.id"),
		field.Int64("group_id").
			Comment("关联 account_groups.id（订阅 group）"),
		field.String("key_hash").
			MaxLen(128).
			Comment("bcrypt(plaintext_api_key)，慢哈希用于查重"),
		field.String("key_encrypted").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.Postgres: "text"}).
			Comment("AES-256-GCM(plaintext)，可解密用于审计/迁移"),
		field.String("status").
			MaxLen(20).
			Default("active").
			Comment("active / revoked / expired"),
		field.String("issued_by").
			MaxLen(64).
			Optional().
			Nillable().
			Comment("distributor / console / redeem"),
		field.String("issued_by_id").
			MaxLen(128).
			Optional().
			Nillable().
			Comment("distributor_id 或 admin_id"),
		field.Time("expires_at").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.Postgres: "timestamptz"}),
		field.Time("last_used_at").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.Postgres: "timestamptz"}),
		field.String("notes").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.Postgres: "text"}),
		field.Time("created_at").
			Immutable().
			Default(time.Now).
			SchemaType(map[string]string{dialect.Postgres: "timestamptz"}),
		field.Time("updated_at").
			Default(time.Now).
			UpdateDefault(time.Now).
			SchemaType(map[string]string{dialect.Postgres: "timestamptz"}),
	}
}

func (UserAPIKey) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("user_id"),
		index.Fields("status"),
		index.Fields("expires_at"),
		// 同一 user + group 下只能有一个 active key
		index.Fields("user_id", "group_id").
			Unique().
			Annotations(entsql.IndexWhere("status = 'active'")),
	}
}