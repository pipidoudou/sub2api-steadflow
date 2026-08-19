package admin

import (
	"encoding/json"
	"fmt"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

var thesisBrandDomainLabelPattern = regexp.MustCompile(`^[a-zA-Z0-9-]+$`)
var skillPackVersionPattern = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

type codexClientSkillCatalogItem struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

type codexClientSkillPackCatalog struct {
	ID            string                        `json:"id"`
	Name          string                        `json:"name"`
	Description   string                        `json:"description"`
	Tags          []string                      `json:"tags"`
	InstallPackID string                        `json:"install_pack_id"`
	Version       string                        `json:"version,omitempty"`
	ManifestURL   string                        `json:"manifest_url,omitempty"`
	Skills        []codexClientSkillCatalogItem `json:"skills"`
}

func validateThesisBrandDomain(raw string) error {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	if strings.Contains(raw, "://") || strings.ContainsAny(raw, "/?#") {
		return fmt.Errorf("must be host[:port] only")
	}
	u, err := url.Parse("https://" + raw)
	if err != nil || u.Host == "" || u.Host != raw {
		return fmt.Errorf("invalid host")
	}
	host := u.Hostname()
	if host == "" || strings.Contains(host, "_") {
		return fmt.Errorf("invalid hostname")
	}
	if ip := net.ParseIP(host); ip == nil {
		for _, label := range strings.Split(host, ".") {
			if label == "" || len(label) > 63 || !thesisBrandDomainLabelPattern.MatchString(label) || strings.HasPrefix(label, "-") || strings.HasSuffix(label, "-") {
				return fmt.Errorf("invalid hostname label")
			}
		}
	}
	if port := u.Port(); port != "" {
		portNum, err := strconv.Atoi(port)
		if err != nil || portNum < 1 || portNum > 65535 {
			return fmt.Errorf("invalid port")
		}
	}
	return nil
}

func normalizeCodexClientSkillsCatalogJSON(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "[]", nil
	}
	var packs []codexClientSkillPackCatalog
	if err := json.Unmarshal([]byte(raw), &packs); err != nil {
		return "", fmt.Errorf("must be a valid JSON array")
	}
	if len(packs) > 50 {
		return "", fmt.Errorf("too many skill packs (max 50)")
	}
	seenPackIDs := make(map[string]struct{}, len(packs))
	seenInstallPackIDs := make(map[string]struct{}, len(packs))
	for i := range packs {
		pack := &packs[i]
		pack.ID = strings.TrimSpace(pack.ID)
		pack.Name = strings.TrimSpace(pack.Name)
		pack.Description = strings.TrimSpace(pack.Description)
		pack.InstallPackID = strings.TrimSpace(pack.InstallPackID)
		pack.Version = strings.TrimSpace(pack.Version)
		pack.ManifestURL = strings.TrimSpace(pack.ManifestURL)
		if pack.ID == "" || pack.Name == "" || pack.InstallPackID == "" {
			return "", fmt.Errorf("each skill pack requires non-empty id, name and install_pack_id")
		}
		if pack.Version != "" && !skillPackVersionPattern.MatchString(pack.Version) {
			return "", fmt.Errorf("skill pack %s has invalid version", pack.ID)
		}
		if pack.ManifestURL != "" {
			if _, err := url.ParseRequestURI(pack.ManifestURL); err != nil {
				return "", fmt.Errorf("skill pack %s has invalid manifest_url", pack.ID)
			}
		}
		if _, exists := seenPackIDs[pack.ID]; exists {
			return "", fmt.Errorf("duplicate skill pack id: %s", pack.ID)
		}
		seenPackIDs[pack.ID] = struct{}{}
		if _, exists := seenInstallPackIDs[pack.InstallPackID]; exists {
			return "", fmt.Errorf("duplicate install_pack_id: %s", pack.InstallPackID)
		}
		seenInstallPackIDs[pack.InstallPackID] = struct{}{}
		pack.Tags = normalizeSteadflowStringSlice(pack.Tags)
		if len(pack.Tags) == 0 || len(pack.Skills) == 0 {
			return "", fmt.Errorf("skill pack %s must include tags and skills", pack.ID)
		}
		if len(pack.Skills) > 100 {
			return "", fmt.Errorf("skill pack %s has too many skills (max 100)", pack.ID)
		}
		seenSkillIDs := make(map[string]struct{}, len(pack.Skills))
		for j := range pack.Skills {
			skill := &pack.Skills[j]
			skill.ID = strings.TrimSpace(skill.ID)
			skill.Name = strings.TrimSpace(skill.Name)
			skill.Description = strings.TrimSpace(skill.Description)
			if skill.ID == "" || skill.Name == "" {
				return "", fmt.Errorf("skill pack %s contains a skill with empty id or name", pack.ID)
			}
			if _, exists := seenSkillIDs[skill.ID]; exists {
				return "", fmt.Errorf("skill pack %s contains duplicate skill id: %s", pack.ID, skill.ID)
			}
			seenSkillIDs[skill.ID] = struct{}{}
		}
	}
	normalized, err := json.Marshal(packs)
	if err != nil {
		return "", fmt.Errorf("failed to normalize JSON")
	}
	return string(normalized), nil
}

func normalizeSteadflowStringSlice(values []string) []string {
	result := make([]string, 0, len(values))
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed == "" {
			continue
		}
		if _, exists := seen[trimmed]; exists {
			continue
		}
		seen[trimmed] = struct{}{}
		result = append(result, trimmed)
	}
	return result
}
