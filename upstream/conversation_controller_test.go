package controllers

import (
	"github.com/labstack/echo/v4"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestConversationDiagnosticOptInAndExactTarget(t *testing.T) {
	cases := []struct {
		flag, url string
		status    int
	}{
		{"", "/?chat=123@s.whatsapp.net", http.StatusNotFound},
		{"1", "/", http.StatusBadRequest},
		{"1", "/?chat=123", http.StatusBadRequest},
		{"1", "/?chat=123@broadcast", http.StatusBadRequest},
		{"1", "/?chat=123@g.us&source=phone", http.StatusBadRequest},
		{"1", "/?chat=123@g.us&source=unknown", http.StatusBadRequest},
	}
	for _, fixture := range cases {
		t.Setenv("WHATSMIAU_CONVERSATION_DIAGNOSTICS", fixture.flag)
		e := echo.New()
		c := e.NewContext(httptest.NewRequest(http.MethodGet, fixture.url, nil), httptest.NewRecorder())
		c.SetParamNames("instance")
		c.SetParamValues("fixture")
		// nil Whatsmiau proves rejections occur before any client/network lookup.
		err := (&Chat{}).ConversationReadSnapshot(c)
		httpErr, ok := err.(*echo.HTTPError)
		if !ok || httpErr.Code != fixture.status {
			t.Fatalf("expected %d, got %v", fixture.status, err)
		}
	}
}
