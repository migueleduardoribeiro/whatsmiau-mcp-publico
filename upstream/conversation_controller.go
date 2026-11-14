package controllers

import (
	"context"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/labstack/echo/v4"
	"github.com/verbeux-ai/whatsmiau/lib/whatsmiau"
	"go.mau.fi/whatsmeow/types"
)

// ConversationReadSnapshot is a metadata-only diagnostic, deliberately opt-in.
// The route belongs to the existing authenticated instance chat group.
func (s *Chat) ConversationReadSnapshot(ctx echo.Context) error {
	if os.Getenv("WHATSMIAU_CONVERSATION_DIAGNOSTICS") != "1" {
		return echo.NewHTTPError(http.StatusNotFound)
	}
	target := ctx.QueryParam("chat")
	jid, err := types.ParseJID(target)
	if err != nil || jid.User == "" || jid.String() != target || (jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer && jid.Server != types.GroupServer) || ctx.Param("instance") == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "exact PN, LID or group chat and instance required")
	}
	requestCtx, cancel := context.WithTimeout(ctx.Request().Context(), 20*time.Second)
	defer cancel()
	if ctx.QueryParam("source") == "identity" {
		identity, err := s.whatsmiau.ConversationIdentity(requestCtx, ctx.Param("instance"), target)
		if err != nil {
			return echo.NewHTTPError(http.StatusBadGateway, "identity unavailable")
		}
		return ctx.JSON(http.StatusOK, identity)
	}
	if ctx.QueryParam("source") == "phone" {
		anchor := ctx.QueryParam("anchor_id")
		timestamp, tsErr := strconv.ParseInt(ctx.QueryParam("anchor_timestamp"), 10, 64)
		fromMe, fmErr := strconv.ParseBool(ctx.QueryParam("anchor_from_me"))
		if anchor == "" || tsErr != nil || timestamp <= 0 || fmErr != nil {
			return echo.NewHTTPError(http.StatusBadRequest, "known anchor ID, timestamp in seconds and from-me boolean required")
		}
		metadata, err := s.whatsmiau.ConversationHistoryMetadata(requestCtx, ctx.Param("instance"), jid, anchor, timestamp, fromMe)
		if err != nil {
			return ctx.JSON(http.StatusBadGateway, map[string]string{"message": "phone metadata unavailable; no baseline established", "error_category": whatsmiau.ConversationDiagnosticCategory(err)})
		}
		return ctx.JSON(http.StatusOK, metadata)
	}
	if ctx.QueryParam("source") != "" && ctx.QueryParam("source") != "snapshot" {
		return echo.NewHTTPError(http.StatusBadRequest, "unsupported metadata source")
	}
	result, err := s.whatsmiau.ConversationReadSnapshot(requestCtx, ctx.Param("instance"), target)
	// Decode errors may contain key IDs; never expose them through this endpoint.
	if err != nil {
		return echo.NewHTTPError(http.StatusBadGateway, "read snapshot unavailable; no baseline established")
	}
	return ctx.JSON(http.StatusOK, result)
}
