//go:build embed

package web

import (
	"bytes"
	"encoding/json"
	"encoding/xml"
	"io/fs"
	"net/http"
	"net/url"
	"path"
	"slices"
	"strings"

	"github.com/gin-gonic/gin"
)

func resolveSiteOrigin(req *http.Request, settings any) string {
	scheme := strings.TrimSpace(req.Header.Get("X-Forwarded-Proto"))
	if scheme == "" {
		if req.TLS != nil {
			scheme = "https"
		} else {
			scheme = "http"
		}
	}

	host := strings.TrimSpace(req.Header.Get("X-Forwarded-Host"))
	if host == "" {
		host = req.Host
	}
	host = strings.TrimSpace(host)
	if host == "" {
		if rawBaseURL := strings.TrimSpace(readStringSetting(settings, "api_base_url")); rawBaseURL != "" {
			if parsed, err := url.Parse(rawBaseURL); err == nil && parsed.Scheme != "" && parsed.Host != "" {
				return parsed.Scheme + "://" + parsed.Host
			}
		}
		return ""
	}
	return scheme + "://" + host
}

type renderedSEO struct {
	Title       string
	Description string
	Robots      string
	Canonical   string
}

func resolveRenderedSEO(path string, settings any, origin string) renderedSEO {
	siteName := strings.TrimSpace(readStringSetting(settings, "site_name"))
	if siteName == "" {
		siteName = "Sub2API"
	}
	siteSubtitle := strings.TrimSpace(readStringSetting(settings, "site_subtitle"))
	if siteSubtitle == "" {
		siteSubtitle = "Unified AI gateway for subscription quota distribution."
	}

	seo := renderedSEO{
		Title:       siteName + " - AI API Gateway",
		Description: siteSubtitle,
		Robots:      "index,follow",
		Canonical:   joinCanonical(origin, normalizeSEOPath(path)),
	}

	switch {
	case path == "/" || path == "/home":
		seo.Title = "AI API Gateway Platform - " + siteName
		seo.Description = buildHomeDescription(siteName, siteSubtitle)
		seo.Canonical = joinCanonical(origin, "/home")
	case path == "/thesis":
		seo.Title = "Thesis - " + siteName
		seo.Description = "AI thesis writing guide covering topic discovery, literature review, triage, and proposal drafting."
	case strings.HasPrefix(path, "/legal/"):
		seo.Title = resolveLegalDocumentTitle(settings, path, siteName)
		seo.Description = "Read the latest legal document and policy details."
	case path == "/login":
		seo.Title = "Login - " + siteName
		seo.Description = "Log in to your " + siteName + " workspace."
		seo.Robots = "noindex,follow"
	case path == "/register":
		seo.Title = "Register - " + siteName
		seo.Description = "Create a " + siteName + " account."
		seo.Robots = "noindex,follow"
	case path == "/forgot-password":
		seo.Title = "Forgot Password - " + siteName
		seo.Robots = "noindex,follow"
	case path == "/reset-password":
		seo.Title = "Reset Password - " + siteName
		seo.Robots = "noindex,follow"
	case path == "/key-usage":
		seo.Title = "Key Usage - " + siteName
		seo.Robots = "noindex,follow"
	case strings.HasPrefix(path, "/auth/"), strings.HasPrefix(path, "/admin"), strings.HasPrefix(path, "/dashboard"), strings.HasPrefix(path, "/usage"), strings.HasPrefix(path, "/keys"):
		seo.Robots = "noindex,nofollow"
	default:
		if path != "/home" && path != "/thesis" && !strings.HasPrefix(path, "/help") {
			seo.Robots = "noindex,nofollow"
		}
	}

	return seo
}

func joinCanonical(origin, path string) string {
	trimmedOrigin := strings.TrimRight(strings.TrimSpace(origin), "/")
	if trimmedOrigin == "" {
		return path
	}
	return trimmedOrigin + path
}

func normalizeSEOPath(path string) string {
	trimmed := strings.TrimSpace(path)
	if trimmed == "" || trimmed == "/" {
		return "/home"
	}
	if strings.HasPrefix(trimmed, "/help") {
		return trimmed
	}
	return trimmed
}

func resolveLegalDocumentTitle(settings any, path, siteName string) string {
	documentID := strings.TrimPrefix(path, "/legal/")
	documentID = strings.TrimSpace(documentID)
	if documentID == "" {
		return "Legal Document - " + siteName
	}
	typed := normalizeSettingsMap(settings)
	if rawDocuments, ok := typed["login_agreement_documents"].([]map[string]any); ok {
		for _, document := range rawDocuments {
			id, _ := document["id"].(string)
			title, _ := document["title"].(string)
			if strings.TrimSpace(id) == documentID && strings.TrimSpace(title) != "" {
				return strings.TrimSpace(title) + " - " + siteName
			}
		}
	}
	if rawAny, ok := typed["login_agreement_documents"].([]any); ok {
		for _, item := range rawAny {
			document, ok := item.(map[string]any)
			if !ok {
				continue
			}
			id, _ := document["id"].(string)
			title, _ := document["title"].(string)
			if strings.TrimSpace(id) == documentID && strings.TrimSpace(title) != "" {
				return strings.TrimSpace(title) + " - " + siteName
			}
		}
	}
	return "Legal Document - " + siteName
}

func injectInitialSEO(html []byte, seo renderedSEO) []byte {
	headClose := []byte("</head>")
	injection := []byte(
		`<meta name="description" content="` + escapeHTMLAttribute(seo.Description) + `" />` +
			`<meta name="robots" content="` + escapeHTMLAttribute(seo.Robots) + `" />` +
			`<meta property="og:type" content="website" />` +
			`<meta property="og:title" content="` + escapeHTMLAttribute(seo.Title) + `" />` +
			`<meta property="og:description" content="` + escapeHTMLAttribute(seo.Description) + `" />` +
			`<meta property="og:url" content="` + escapeHTMLAttribute(seo.Canonical) + `" />` +
			`<meta name="twitter:card" content="summary_large_image" />` +
			`<meta name="twitter:title" content="` + escapeHTMLAttribute(seo.Title) + `" />` +
			`<meta name="twitter:description" content="` + escapeHTMLAttribute(seo.Description) + `" />` +
			`<link rel="canonical" href="` + escapeHTMLAttribute(seo.Canonical) + `" />`)

	html = stripManagedSEO(html)
	html = bytes.Replace(html, headClose, append(injection, headClose...), 1)
	titleStart := bytes.Index(html, []byte("<title>"))
	titleEnd := bytes.Index(html, []byte("</title>"))
	if titleStart != -1 && titleEnd != -1 && titleEnd > titleStart {
		var buf bytes.Buffer
		buf.Write(html[:titleStart])
		buf.WriteString("<title>")
		buf.WriteString(escapeHTMLText(seo.Title))
		buf.WriteString("</title>")
		buf.Write(html[titleEnd+len("</title>"):])
		html = buf.Bytes()
	}
	return html
}

func stripManagedSEO(html []byte) []byte {
	markers := []string{
		`<meta name="description"`,
		`<meta name="robots"`,
		`<meta property="og:type"`,
		`<meta property="og:title"`,
		`<meta property="og:description"`,
		`<meta property="og:url"`,
		`<meta name="twitter:card"`,
		`<meta name="twitter:title"`,
		`<meta name="twitter:description"`,
		`<link rel="canonical"`,
	}
	output := string(html)
	for _, marker := range markers {
		for {
			start := strings.Index(output, marker)
			if start == -1 {
				break
			}
			end := strings.Index(output[start:], ">")
			if end == -1 {
				break
			}
			output = output[:start] + output[start+end+1:]
		}
	}
	return []byte(output)
}

func escapeHTMLAttribute(value string) string {
	replacer := strings.NewReplacer("&", "&amp;", `"`, "&quot;", "<", "&lt;", ">", "&gt;")
	return replacer.Replace(value)
}

func escapeHTMLText(value string) string {
	replacer := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;")
	return replacer.Replace(value)
}

func buildHomeDescription(siteName, siteSubtitle string) string {
	trimmedSubtitle := strings.TrimSpace(siteSubtitle)
	if trimmedSubtitle == "" {
		return siteName + " helps you access AI workflows, subscriptions, and operational guidance in one place."
	}
	if strings.Contains(strings.ToLower(trimmedSubtitle), strings.ToLower(siteName)) {
		return trimmedSubtitle
	}
	return siteName + " - " + trimmedSubtitle
}

func buildPublicSiteURLs(origin string, settings any, assetFS fs.FS) []string {
	base := strings.TrimRight(strings.TrimSpace(origin), "/")
	if base == "" {
		return nil
	}

	urls := []string{base + "/home", base + "/thesis"}
	urls = append(urls, buildMachineReadableURLs(base, assetFS)...)

	for _, documentID := range readDocumentIDs(settings) {
		urls = append(urls, base+"/legal/"+documentID)
	}

	slices.Sort(urls)
	return slices.Compact(urls)
}

func buildMachineReadableURLs(base string, assetFS fs.FS) []string {
	if assetFS == nil {
		return nil
	}

	candidates := []string{
		"llms.txt",
		"pricing.md",
	}
	urls := make([]string, 0, len(candidates))
	for _, candidate := range candidates {
		if _, err := fs.Stat(assetFS, candidate); err == nil {
			urls = append(urls, base+"/"+path.Clean(candidate))
		}
	}
	return urls
}

func buildRobotsTXT(origin string) string {
	base := strings.TrimRight(strings.TrimSpace(origin), "/")
	lines := []string{
		"User-agent: *",
		"Allow: /",
		"Disallow: /admin",
		"Disallow: /dashboard",
		"Disallow: /usage",
		"Disallow: /keys",
		"Disallow: /login",
		"Disallow: /register",
		"Disallow: /auth/",
	}
	lines = append(lines, buildAIBotRobotsBlock("GPTBot")...)
	lines = append(lines, buildAIBotRobotsBlock("ChatGPT-User")...)
	lines = append(lines, buildAIBotRobotsBlock("PerplexityBot")...)
	lines = append(lines, buildAIBotRobotsBlock("ClaudeBot")...)
	lines = append(lines, buildAIBotRobotsBlock("anthropic-ai")...)
	lines = append(lines, buildAIBotRobotsBlock("Google-Extended")...)
	lines = append(lines, buildAIBotRobotsBlock("Bingbot")...)
	if base != "" {
		lines = append(lines, "Sitemap: "+base+"/sitemap.xml")
	}
	return strings.Join(lines, "\n") + "\n"
}

func buildAIBotRobotsBlock(agent string) []string {
	return []string{
		"",
		"User-agent: " + agent,
		"Allow: /",
		"Disallow: /admin",
		"Disallow: /dashboard",
		"Disallow: /usage",
		"Disallow: /keys",
		"Disallow: /login",
		"Disallow: /register",
		"Disallow: /auth/",
	}
}

func buildSitemapXML(urls []string) string {
	type sitemapURL struct {
		Loc string `xml:"loc"`
	}
	type sitemap struct {
		XMLName xml.Name     `xml:"urlset"`
		Xmlns   string       `xml:"xmlns,attr"`
		URLs    []sitemapURL `xml:"url"`
	}

	items := make([]sitemapURL, 0, len(urls))
	for _, item := range urls {
		trimmed := strings.TrimSpace(item)
		if trimmed == "" {
			continue
		}
		items = append(items, sitemapURL{Loc: trimmed})
	}
	body, err := xml.MarshalIndent(sitemap{
		Xmlns: "http://www.sitemaps.org/schemas/sitemap/0.9",
		URLs:  items,
	}, "", "  ")
	if err != nil {
		return `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>`
	}
	return xml.Header + string(body)
}

func readStringSetting(settings any, key string) string {
	typed := normalizeSettingsMap(settings)
	if value, ok := typed[key].(string); ok {
		return value
	}
	return ""
}

func readBoolSetting(settings any, key string) bool {
	typed := normalizeSettingsMap(settings)
	if value, ok := typed[key].(bool); ok {
		return value
	}
	return false
}

func readDocumentIDs(settings any) []string {
	typed := normalizeSettingsMap(settings)
	rawDocuments, ok := typed["login_agreement_documents"].([]map[string]any)
	if ok {
		ids := make([]string, 0, len(rawDocuments))
		for _, item := range rawDocuments {
			if id, ok := item["id"].(string); ok && strings.TrimSpace(id) != "" {
				ids = append(ids, strings.TrimSpace(id))
			}
		}
		return ids
	}

	rawAny, ok := typed["login_agreement_documents"].([]any)
	if !ok {
		return nil
	}
	ids := make([]string, 0, len(rawAny))
	for _, item := range rawAny {
		document, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if id, ok := document["id"].(string); ok && strings.TrimSpace(id) != "" {
			ids = append(ids, strings.TrimSpace(id))
		}
	}
	return ids
}

func normalizeSettingsMap(settings any) map[string]any {
	if settings == nil {
		return map[string]any{}
	}
	if typed, ok := settings.(map[string]any); ok {
		return typed
	}
	payload, err := json.Marshal(settings)
	if err != nil {
		return map[string]any{}
	}
	var normalized map[string]any
	if err := json.Unmarshal(payload, &normalized); err != nil {
		return map[string]any{}
	}
	return normalized
}

func serveSEOAsset(c *gin.Context, settings any, assetFS fs.FS) bool {
	origin := resolveSiteOrigin(c.Request, settings)
	switch c.Request.URL.Path {
	case "/robots.txt":
		c.Data(http.StatusOK, "text/plain; charset=utf-8", []byte(buildRobotsTXT(origin)))
		c.Abort()
		return true
	case "/sitemap.xml":
		c.Data(http.StatusOK, "application/xml; charset=utf-8", []byte(buildSitemapXML(buildPublicSiteURLs(origin, settings, assetFS))))
		c.Abort()
		return true
	default:
		return false
	}
}

func serveSEOAssetFallback(c *gin.Context, assetFS fs.FS) bool {
	switch c.Request.URL.Path {
	case "/robots.txt":
		c.Data(http.StatusOK, "text/plain; charset=utf-8", []byte(buildRobotsTXT(resolveSiteOrigin(c.Request, nil))))
		c.Abort()
		return true
	case "/sitemap.xml":
		origin := resolveSiteOrigin(c.Request, nil)
		c.Data(http.StatusOK, "application/xml; charset=utf-8", []byte(buildSitemapXML(buildPublicSiteURLs(origin, nil, assetFS))))
		c.Abort()
		return true
	default:
		return false
	}
}
