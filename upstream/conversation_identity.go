package whatsmiau

import (
	"context"
	"errors"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

type ConversationIdentity struct {
	RequestedChat      string   `json:"requested_chat"`
	Aliases            []string `json:"aliases"`
	CanonicalChat      *string  `json:"canonical_chat"`
	ResolutionComplete bool     `json:"resolution_complete"`
}

// Cache-only identity resolution. No network, contacts, sync or read action.
func (s *Whatsmiau) ConversationIdentity(ctx context.Context, instance, target string) (*ConversationIdentity, error) {
	if s == nil || s.clients == nil {
		return nil, errors.New("instance unavailable")
	}
	client, ok := s.clients.Load(instance)
	if !ok || client == nil || client.Store == nil {
		return nil, errors.New("instance unavailable")
	}
	return resolveConversationIdentity(ctx, client.Store.LIDs, target)
}

func resolveConversationIdentity(ctx context.Context, lids store.LIDStore, target string) (*ConversationIdentity, error) {
	jid, err := types.ParseJID(target)
	if err != nil || jid.User == "" || jid.Device != 0 || jid.String() != target {
		return nil, errors.New("exact chat JID required")
	}
	result := &ConversationIdentity{RequestedChat: target, Aliases: []string{target}}
	if jid.Server == types.GroupServer {
		result.CanonicalChat = &target
		result.ResolutionComplete = true
		return result, nil
	}
	if jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer {
		return nil, errors.New("unsupported chat JID")
	}
	if jid.Server == types.DefaultUserServer {
		result.CanonicalChat = &target
	}
	if lids == nil {
		return result, nil
	}
	var mapped types.JID
	if jid.Server == types.DefaultUserServer {
		mapped, err = lids.GetLIDForPN(ctx, jid)
	} else {
		mapped, err = lids.GetPNForLID(ctx, jid)
	}
	if err != nil {
		return nil, errors.New("identity mapping lookup failed")
	}
	if mapped.IsEmpty() {
		return result, nil
	}
	expected := types.DefaultUserServer
	if jid.Server == types.DefaultUserServer {
		expected = types.HiddenUserServer
	}
	if mapped.Server != expected || mapped.User == "" || mapped.Device != 0 {
		return nil, errors.New("invalid identity mapping")
	}
	alias := mapped.String()
	result.Aliases = append(result.Aliases, alias)
	result.ResolutionComplete = true
	if mapped.Server == types.DefaultUserServer {
		result.CanonicalChat = &alias
	}
	return result, nil
}
