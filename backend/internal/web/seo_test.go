//go:build embed

package web

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"testing/fstest"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestResolveSiteOrigin(t *testing.T) {
	req := httptest.NewRequest("GET", "https://fallback.example.com/home", nil)
	req.Host = "app.example.com"
	req.Header.Set("X-Forwarded-Proto", "https")
	req.Header.Set("X-Forwarded-Host", "app.example.com")

	origin := resolveSiteOrigin(req, map[string]any{
		"thesis_vertical_brand_domain": "paper.example.com",
		"api_base_url":                 "https://api.example.com/v1",
	})

	assert.Equal(t, "https://app.example.com", origin)
}

func TestBuildPublicSiteURLs(t *testing.T) {
	urls := buildPublicSiteURLs("https://sub2api.example.com", map[string]any{
		"login_agreement_documents": []map[string]any{
			{"id": "privacy-policy"},
			{"id": "terms-of-service"},
		},
	}, fstest.MapFS{
		"pricing.md": &fstest.MapFile{Data: []byte("pricing")},
		"llms.txt":   &fstest.MapFile{Data: []byte("llms")},
	})

	assert.Contains(t, urls, "https://sub2api.example.com/home")
	assert.Contains(t, urls, "https://sub2api.example.com/thesis")
	assert.Contains(t, urls, "https://sub2api.example.com/legal/privacy-policy")
	assert.Contains(t, urls, "https://sub2api.example.com/pricing.md")
	assert.Contains(t, urls, "https://sub2api.example.com/llms.txt")
}

func TestBuildRobotsTXT(t *testing.T) {
	robots := buildRobotsTXT("https://sub2api.example.com")

	assert.Contains(t, robots, "User-agent: *")
	assert.Contains(t, robots, "User-agent: GPTBot")
	assert.Contains(t, robots, "User-agent: ClaudeBot")
	assert.Contains(t, robots, "Allow: /")
	assert.Contains(t, robots, "Disallow: /dashboard")
	assert.Contains(t, robots, "Sitemap: https://sub2api.example.com/sitemap.xml")
}

func TestBuildSitemapXML(t *testing.T) {
	xml := buildSitemapXML([]string{
		"https://sub2api.example.com/home",
		"https://sub2api.example.com/thesis",
	})

	assert.Contains(t, xml, `<loc>https://sub2api.example.com/home</loc>`)
	assert.Contains(t, xml, `<loc>https://sub2api.example.com/thesis</loc>`)
	assert.Contains(t, xml, `<urlset`)
}

func TestResolveRenderedSEO(t *testing.T) {
	settings := map[string]any{
		"site_name":     "Sub2API",
		"site_subtitle": "Gateway platform",
		"login_agreement_documents": []map[string]any{
			{"id": "privacy-policy", "title": "Privacy Policy"},
		},
	}

	homeSEO := resolveRenderedSEO("/home", settings, "https://app.example.com")
	assert.Equal(t, "AI API Gateway Platform - Sub2API", homeSEO.Title)
	assert.Equal(t, "index,follow", homeSEO.Robots)
	assert.Equal(t, "https://app.example.com/home", homeSEO.Canonical)
	assert.Equal(t, "Sub2API - Gateway platform", homeSEO.Description)

	legalSEO := resolveRenderedSEO("/legal/privacy-policy", settings, "https://app.example.com")
	assert.Equal(t, "Privacy Policy - Sub2API", legalSEO.Title)

	loginSEO := resolveRenderedSEO("/login", settings, "https://app.example.com")
	assert.Equal(t, "noindex,follow", loginSEO.Robots)
}

func TestServeSEOAsset(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.GET("/robots.txt", func(c *gin.Context) {
		ok := serveSEOAsset(c, map[string]any{
			"thesis_vertical_brand_domain": "sub2api.example.com",
		}, fstest.MapFS{
			"pricing.md": &fstest.MapFile{Data: []byte("pricing")},
			"llms.txt":   &fstest.MapFile{Data: []byte("llms")},
		})
		require.True(t, ok)
	})
	router.GET("/sitemap.xml", func(c *gin.Context) {
		ok := serveSEOAsset(c, map[string]any{
			"thesis_vertical_brand_domain": "sub2api.example.com",
		}, fstest.MapFS{
			"pricing.md": &fstest.MapFile{Data: []byte("pricing")},
			"llms.txt":   &fstest.MapFile{Data: []byte("llms")},
		})
		require.True(t, ok)
	})

	robotsRecorder := httptest.NewRecorder()
	robotsRequest := httptest.NewRequest(http.MethodGet, "/robots.txt", nil)
	robotsRequest.Host = "sub2api.example.com"
	robotsRequest.Header.Set("X-Forwarded-Proto", "https")
	robotsRequest.Header.Set("X-Forwarded-Host", "sub2api.example.com")
	router.ServeHTTP(robotsRecorder, robotsRequest)
	assert.Equal(t, http.StatusOK, robotsRecorder.Code)
	assert.Contains(t, robotsRecorder.Body.String(), "Sitemap: https://sub2api.example.com/sitemap.xml")

	sitemapRecorder := httptest.NewRecorder()
	sitemapRequest := httptest.NewRequest(http.MethodGet, "/sitemap.xml", nil)
	sitemapRequest.Host = "sub2api.example.com"
	sitemapRequest.Header.Set("X-Forwarded-Proto", "https")
	sitemapRequest.Header.Set("X-Forwarded-Host", "sub2api.example.com")
	router.ServeHTTP(sitemapRecorder, sitemapRequest)
	assert.Equal(t, http.StatusOK, sitemapRecorder.Code)
	assert.Contains(t, sitemapRecorder.Body.String(), "<loc>https://sub2api.example.com/home</loc>")
	assert.Contains(t, sitemapRecorder.Body.String(), "<loc>https://sub2api.example.com/thesis</loc>")
	assert.Contains(t, sitemapRecorder.Body.String(), "<loc>https://sub2api.example.com/pricing.md</loc>")
}
