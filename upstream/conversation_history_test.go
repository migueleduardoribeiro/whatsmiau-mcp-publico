package whatsmiau

import (
	"encoding/json"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"
)

func TestConversationHistoryMessageFactsPreserveStatusPresenceAndHideBodies(t *testing.T) {
	for _, status := range []*waWeb.WebMessageInfo_Status{nil, waWeb.WebMessageInfo_ERROR.Enum(), waWeb.WebMessageInfo_READ.Enum()} {
		conv := &waHistorySync.Conversation{ID: proto.String("fixture@lid"), Messages: []*waHistorySync.HistorySyncMsg{{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("fixture-id"), FromMe: proto.Bool(false)}, MessageTimestamp: proto.Uint64(123), Status: status, Message: &waE2E.Message{Conversation: proto.String("SECRET_BODY")}}}}}
		result := conversationHistoryMetadata([]*waHistorySync.Conversation{conv}, "fixture@lid")
		if len(result.Messages) != 1 || result.Messages[0].Status != status || result.Messages[0].FromMe == nil || *result.Messages[0].FromMe || *result.Messages[0].Timestamp != 123 || *result.Messages[0].ID != "fixture-id" {
			t.Fatal("raw message metadata/presence lost")
		}
		if (result.Messages[0].StatusName == nil) != (status == nil) {
			t.Fatal("status name must not invent absent enum")
		}
		raw, _ := json.Marshal(result)
		if strings.Contains(string(raw), "SECRET_BODY") {
			t.Fatal("message body leaked")
		}
		conv.Messages = append(conv.Messages, conv.Messages[0])
		result = conversationHistoryMetadata([]*waHistorySync.Conversation{conv}, "fixture@lid")
		if len(result.Messages) != 1 || !result.MessagesTruncated || result.ReturnedMessageCount != 2 {
			t.Fatal("metadata response must remain bounded to requested Count1")
		}
	}
}

func TestConversationHistoryMetadataPreservesOptionalPresence(t *testing.T) {
	for _, fixture := range []struct {
		count  *uint32
		marked *bool
	}{{nil, nil}, {proto.Uint32(0), proto.Bool(false)}, {proto.Uint32(3), proto.Bool(true)}} {
		conv := &waHistorySync.Conversation{ID: proto.String("fixture@g.us"), Name: proto.String("SECRET_NAME"), UnreadCount: fixture.count, MarkedAsUnread: fixture.marked}
		result := conversationHistoryMetadata([]*waHistorySync.Conversation{{ID: proto.String("other@g.us"), UnreadCount: proto.Uint32(99)}, conv}, "fixture@g.us")
		if result == nil || result.UnreadCount != fixture.count || result.MarkedAsUnread != fixture.marked {
			t.Fatal("optional presence lost or another chat returned")
		}
		raw, err := json.Marshal(result)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(raw), "SECRET") || strings.Contains(string(raw), "99") {
			t.Fatal("unrelated metadata leaked")
		}
	}
	if conversationHistoryMetadata(nil, "fixture@g.us") != nil {
		t.Fatal("missing chat must remain unknown")
	}
}
