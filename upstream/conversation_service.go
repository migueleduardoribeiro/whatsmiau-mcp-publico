package whatsmiau

import (
	"context"
	"errors"
)

func (s *Whatsmiau) ConversationReadSnapshot(ctx context.Context, instance, target string) (*ConversationSnapshot, error) {
	client, ok := s.clients.Load(instance)
	if !ok {
		return nil, errors.New("client unavailable")
	}
	return FetchConversationReadSnapshot(ctx, client, target)
}
