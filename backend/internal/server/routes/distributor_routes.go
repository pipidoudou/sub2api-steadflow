package routes

import (
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/handler"
	"github.com/gin-gonic/gin"
)

// RegisterDistributorRoutes 注册分销商端点（v2.2 §4.3.1）。
//
// distributorAuth 是中间件 DistributorAuth 返回的 func(http.Handler) http.Handler。
// 我们把它包装成 gin.HandlerFunc。
func RegisterDistributorRoutes(rg *gin.RouterGroup, h *handler.DistributorHandler, distributorAuth func(http.Handler) http.Handler) {
	grp := rg.Group("/distributor")
	grp.POST("/orders", gin.WrapH(distributorAuth(http.HandlerFunc(adaptToHTTP(h.HandleOrders, "POST")))))
	grp.GET("/orders/:id", gin.WrapH(distributorAuth(http.HandlerFunc(adaptToHTTP(h.HandleOrderByID, "GET")))))
	grp.GET("/apikeys/:shadowUserId", gin.WrapH(distributorAuth(http.HandlerFunc(adaptToHTTP(h.HandleAPIKeysByUser, "GET")))))
}

// adaptToHTTP 把 gin handler 适配为 http.HandlerFunc 形态。
// 这里简化：分销商 handler 已是 net/http 风格（http.Handler 签名），直接返回。
func adaptToHTTP(h http.HandlerFunc, _ string) http.HandlerFunc {
	return h
}
