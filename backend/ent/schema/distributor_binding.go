// Package schema - distributor_binding.go
//
// 依据: 架构方案 v2.2 §4.2
// 对应 SQL: console/backend/migrations/20260708_distributor_and_apikeys.sql 第 1 节
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

// DistributorBinding 持有分销商 token 绑定信息。
type DistributorBinding struct {
	ent.Schema
}

func (DistributorBinding) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "distributor_bindings"},
	}
}

func (DistributorBinding) Fields() []ent.Field {
	return []ent.Field{
		field.String("distributor_id").
			MaxLen(64).
			Unique().
			Comment("外部 distributor_id（站新侧标识，如 'site'）"),
		field.String("name").
			MaxLen(128).
			Comment("分销商名称（用于后台显示）"),
		field.String("token_hash").
			MaxLen(128).
			Unique().
			Comment("SHA-256(token) 用于查询；token 明文仅展示一次"),
		field.Bool("enabled").
			Default(true).
			Comment("false 时拒绝请求"),
		field.String("contact_email").
			MaxLen(255).
			Optional().
			Nillable(),
		field.String("notes").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.Postgres: "text"}),
		field.Time("expires_at").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.Postgres: "timestamptz"}),
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

func (DistributorBinding) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("enabled"),
		index.Fields("expires_at"),
	}
}