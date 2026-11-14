package whatsmiau

import (
	"context"
	"errors"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"sync"
	"time"
)

var conversationDiagnosticTimeout = errors.New("diagnostic timeout")
var conversationDiagnosticSend = errors.New("diagnostic send failed")
var conversationDiagnosticNotConnected = errors.New("diagnostic client not connected")
var conversationDiagnosticMissing = errors.New("diagnostic instance or conversation missing")

func ConversationDiagnosticCategory(err error) string {
	switch {
	case errors.Is(err, conversationDiagnosticTimeout), errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
		return "timeout"
	case errors.Is(err, conversationDiagnosticSend):
		return "send"
	case errors.Is(err, conversationDiagnosticNotConnected):
		return "notconnected"
	default:
		return "missing"
	}
}

func conversationHistorySyncMatchesWaiter(event *events.HistorySync, waiter *pendingSyncWaiter) bool {
	if historySyncMatchesChat(event, waiter.chat) {
		return true
	}
	for _, alias := range waiter.aliases {
		if historySyncMatchesChat(event, alias) {
			return true
		}
	}
	return false
}

func conversationDiagnosticExchange(ctx context.Context, waiter *pendingSyncWaiter, register, cleanup func(), send func() error) (*events.HistorySync, error) {
	defer cleanup()
	register()
	if err := send(); err != nil {
		if ctx.Err() != nil || errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
			return nil, conversationDiagnosticTimeout
		}
		return nil, conversationDiagnosticSend
	}
	select {
	case event := <-waiter.ch:
		return event, nil
	case <-ctx.Done():
		return nil, conversationDiagnosticTimeout
	}
}

// A separate diagnostic path leaves legacy SyncChatMessages timing unchanged.
// Explicit request ID permits installing the waiter before protocol submission.
func (s *Whatsmiau) ConversationHistoryMetadata(ctx context.Context, instance string, chat types.JID, anchorID string, anchorTimestamp int64, fromMe bool) (*ConversationHistoryMetadata, error) {
	if s == nil || s.clients == nil || s.syncLocks == nil || s.pendingSyncs == nil || anchorID == "" || anchorTimestamp <= 0 {
		return nil, conversationDiagnosticMissing
	}
	client, ok := s.clients.Load(instance)
	if !ok || client == nil || client.Store == nil || client.Store.GetJID().IsEmpty() {
		return nil, conversationDiagnosticMissing
	}
	if !client.IsConnected() {
		return nil, conversationDiagnosticNotConnected
	}
	ctx, cancel := context.WithTimeout(ctx, 18*time.Second)
	defer cancel()
	mutex, _ := s.syncLocks.LoadOrStore(instance, &sync.Mutex{})
	// Bound lock contention too; ordinary Mutex.Lock would outlive the deadline.
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for !mutex.TryLock() {
		select {
		case <-ctx.Done():
			return nil, conversationDiagnosticTimeout
		case <-ticker.C:
		}
	}
	defer mutex.Unlock()
	identity, err := resolveConversationIdentity(ctx, client.Store.LIDs, chat.String())
	if err != nil {
		return nil, conversationDiagnosticMissing
	}
	address := chat
	aliases := make([]types.JID, 0, len(identity.Aliases))
	for _, alias := range identity.Aliases {
		jid, err := types.ParseJID(alias)
		if err != nil {
			return nil, conversationDiagnosticMissing
		}
		aliases = append(aliases, jid)
		if jid.Server == types.HiddenUserServer {
			address = jid
		}
	}
	requestID := client.GenerateMessageID()
	waiter := &pendingSyncWaiter{chat: address, aliases: aliases, sendID: requestID, ch: make(chan *events.HistorySync, 1)}
	message := client.BuildHistorySyncRequest(&types.MessageInfo{MessageSource: types.MessageSource{Chat: address, IsFromMe: fromMe}, ID: anchorID, Timestamp: time.Unix(anchorTimestamp, 0)}, 1)
	event, err := conversationDiagnosticExchange(ctx, waiter, func() { s.pendingSyncs.Store(instance, waiter) }, func() { s.pendingSyncs.Delete(instance) }, func() error {
		// Same own-device Peer=true operation as SendPeerMessage, with explicit ID.
		_, err := client.SendMessage(ctx, client.Store.GetJID().ToNonAD(), message, whatsmeow.SendRequestExtra{Peer: true, ID: requestID, Timeout: 18 * time.Second})
		return err
	})
	if err != nil {
		return nil, err
	}
	if event == nil || event.Data == nil {
		return nil, conversationDiagnosticMissing
	}
	result := conversationHistoryMetadata(event.Data.GetConversations(), identity.Aliases...)
	if result == nil {
		return nil, conversationDiagnosticMissing
	}
	return result, nil
}
