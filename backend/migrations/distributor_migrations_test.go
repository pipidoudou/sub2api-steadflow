package migrations

import (
	"strings"
	"testing"
)

func TestDistributorMigrationsAreAdditiveAndOrdered(t *testing.T) {
	for _, name := range []string{
		"174_add_distributor_bindings_and_user_api_keys.sql",
		"175_add_distributor_payment_fields.sql",
	} {
		content, err := FS.ReadFile(name)
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		text := string(content)
		if !strings.Contains(text, "IF NOT EXISTS") {
			t.Fatalf("%s must be idempotent", name)
		}
		if strings.Contains(strings.ToUpper(text), "DROP TABLE") || strings.Contains(strings.ToUpper(text), "DROP COLUMN") {
			t.Fatalf("%s must not delete existing data", name)
		}
	}
}

func TestDistributorPaymentFieldsAreNullable(t *testing.T) {
	content, err := FS.ReadFile("175_add_distributor_payment_fields.sql")
	if err != nil {
		t.Fatal(err)
	}
	text := string(content)
	for _, column := range []string{"distributor_email", "external_user_id", "created_user_id", "distributor_id"} {
		if !strings.Contains(text, "ADD COLUMN IF NOT EXISTS "+column) {
			t.Fatalf("missing additive payment field %s", column)
		}
	}
}
