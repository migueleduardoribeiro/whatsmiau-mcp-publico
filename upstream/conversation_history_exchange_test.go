package whatsmiau

import (
	"context"
	"errors"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
	"testing"
)

func TestConversationDiagnosticWaiterRegisteredBeforeSend(t *testing.T) {
	registered, cleaned := false, false
	waiter := &pendingSyncWaiter{chat: types.NewJID("fixture", types.HiddenUserServer), sendID: "request", ch: make(chan *events.HistorySync, 1)}
	expected := &events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String("fixture@lid")}}}}
	result, err := conversationDiagnosticExchange(context.Background(), waiter, func() { registered = true }, func() { cleaned = true }, func() error {
		if !registered {
			t.Fatal("race: send started before waiter registration")
		}
		waiter.ch <- expected
		return nil
	})
	if err != nil || result != expected || !cleaned {
		t.Fatal("fast response lost or waiter leaked")
	}
}

func TestConversationDiagnosticCategoriesAndCleanup(t *testing.T) {
	for _, fixture := range []struct {
		ctx      context.Context
		send     error
		expected string
	}{{context.Background(), errors.New("SECRET token"), "send"}, {func() context.Context { ctx, cancel := context.WithCancel(context.Background()); cancel(); return ctx }(), nil, "timeout"}} {
		cleaned := false
		_, err := conversationDiagnosticExchange(fixture.ctx, &pendingSyncWaiter{ch: make(chan *events.HistorySync, 1)}, func() {}, func() { cleaned = true }, func() error { return fixture.send })
		if ConversationDiagnosticCategory(err) != fixture.expected || !cleaned {
			t.Fatal("safe error category/cleanup lost")
		}
	}
}

func TestConversationDiagnosticMatchesBothVerifiedAliases(t *testing.T) {
	waiter := &pendingSyncWaiter{chat: types.NewJID("mapped", types.HiddenUserServer), aliases: []types.JID{types.NewJID("pn", types.DefaultUserServer)}}
	for _, target := range []string{"mapped@lid", "pn@s.whatsapp.net"} {
		event := &events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String(target)}}}}
		if !conversationHistorySyncMatchesWaiter(event, waiter) || conversationHistoryMetadata(event.Data.Conversations, waiter.chat.String(), "pn@s.whatsapp.net") == nil {
			t.Fatal("verified alias was dropped")
		}
	}
	other := &events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String("other@lid")}}}}
	if conversationHistorySyncMatchesWaiter(other, waiter) {
		t.Fatal("scope broadened to unrelated chat")
	}
}
