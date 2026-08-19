package middleware

import "testing"

func TestHashTokenUsesSHA256(t *testing.T) {
	token := "dist_abcdefghijklmnopqrstuvwxyz123456"
	got := hashToken(token)
	want := "0456d7705523b72125740253ce7b2707b825f87096216b9f486e82b7c754f202"
	if got != want {
		t.Fatalf("hashToken() = %q, want %q", got, want)
	}
	if got == token {
		t.Fatal("hashToken returned the plaintext token")
	}
	if len(got) != 64 {
		t.Fatalf("hash length = %d, want 64", len(got))
	}
}
