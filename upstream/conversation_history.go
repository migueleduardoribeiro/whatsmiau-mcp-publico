package whatsmiau

import (
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
)

// These optional fields are facts from this ON_DEMAND response only. Presence
// does not establish that the phone populated them with its current UI count.
type ConversationHistoryMetadata struct {
	Chat                 string                               `json:"chat"`
	UnreadCount          *uint32                              `json:"unreadCount"`
	MarkedAsUnread       *bool                                `json:"markedAsUnread"`
	UnreadMentionCount   *uint32                              `json:"unreadMentionCount"`
	Messages             []ConversationHistoryMessageMetadata `json:"messages"`
	ReturnedMessageCount int                                  `json:"returned_message_count"`
	MessagesTruncated    bool                                 `json:"messages_truncated"`
}

// Raw protocol facts only; Status is not interpreted as this account's unread
// flag. In particular outgoing receipt status has a different meaning.
type ConversationHistoryMessageMetadata struct {
	ID         *string                      `json:"id"`
	Timestamp  *uint64                      `json:"timestamp"`
	FromMe     *bool                        `json:"from_me"`
	Status     *waWeb.WebMessageInfo_Status `json:"status"`
	StatusName *string                      `json:"status_name"`
}

func conversationHistoryMetadata(conversations []*waHistorySync.Conversation, targets ...string) *ConversationHistoryMetadata {
	for _, conversation := range conversations {
		for _, target := range targets {
			if conversation.GetID() == target {
				result := &ConversationHistoryMetadata{Chat: target, UnreadCount: conversation.UnreadCount, MarkedAsUnread: conversation.MarkedAsUnread, UnreadMentionCount: conversation.UnreadMentionCount, Messages: []ConversationHistoryMessageMetadata{}, ReturnedMessageCount: len(conversation.GetMessages())}
				for _, historyMessage := range conversation.GetMessages() {
					message := historyMessage.GetMessage()
					if message == nil {
						continue
					}
					if len(result.Messages) >= 1 {
						result.MessagesTruncated = true
						break
					}
					entry := ConversationHistoryMessageMetadata{Timestamp: message.MessageTimestamp, Status: message.Status}
					if message.Key != nil {
						entry.ID = message.Key.ID
						entry.FromMe = message.Key.FromMe
					}
					if message.Status != nil {
						name := message.Status.String()
						entry.StatusName = &name
					}
					result.Messages = append(result.Messages, entry)
				}
				return result
			}
		}
	}
	return nil
}
