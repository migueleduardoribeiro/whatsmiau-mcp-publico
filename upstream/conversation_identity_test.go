package whatsmiau

import (
	"context"
	"github.com/puzpuzpuz/xsync/v4"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"testing"
)

type conversationIdentityFixture struct {
	store.LIDStore
	pn, lid types.JID
}

func (s conversationIdentityFixture) GetLIDForPN(context.Context, types.JID) (types.JID, error) {
	return s.lid, nil
}
func (s conversationIdentityFixture) GetPNForLID(context.Context, types.JID) (types.JID, error) {
	return s.pn, nil
}

func TestConversationIdentityResolvesBothDirectionsCacheOnly(t *testing.T) {
	fixture := conversationIdentityFixture{pn: types.NewJID("fixture", types.DefaultUserServer), lid: types.NewJID("mapped", types.HiddenUserServer)}
	for _, target := range []string{"fixture@s.whatsapp.net", "mapped@lid"} {
		result, err := resolveConversationIdentity(context.Background(), fixture, target)
		if err != nil || !result.ResolutionComplete || result.RequestedChat != target || len(result.Aliases) != 2 || result.Aliases[0] != target || result.CanonicalChat == nil || *result.CanonicalChat != "fixture@s.whatsapp.net" {
			t.Fatal("cache alias resolution lost")
		}
	}
	for _, cache := range []store.LIDStore{nil, conversationIdentityFixture{}} {
		for _, target := range []string{"fixture@s.whatsapp.net", "mapped@lid"} {
			result, err := resolveConversationIdentity(context.Background(), cache, target)
			if err != nil || result.ResolutionComplete || len(result.Aliases) != 1 || result.Aliases[0] != target || (result.CanonicalChat == nil) != (target == "mapped@lid") {
				t.Fatal("missing cache/mapping must be explicit incomplete singleton")
			}
		}
	}
	// Nil methods would panic if group mode touched cache at all.
	group, err := resolveConversationIdentity(context.Background(), conversationIdentityFixture{}, "fixture@g.us")
	if err != nil || !group.ResolutionComplete || len(group.Aliases) != 1 || *group.CanonicalChat != "fixture@g.us" {
		t.Fatal("group must be exact complete singleton")
	}
}

func TestConversationIdentityRequiresExistingInstance(t *testing.T) {
	service := &Whatsmiau{clients: xsync.NewMap[string, *whatsmeow.Client]()}
	if result, err := service.ConversationIdentity(context.Background(), "missing", "fixture@lid"); err == nil || result != nil {
		t.Fatal("missing instance must fail")
	}
	service.clients.Store("empty", &whatsmeow.Client{})
	if result, err := service.ConversationIdentity(context.Background(), "empty", "fixture@lid"); err == nil || result != nil {
		t.Fatal("missing device must fail")
	}
	service.clients.Store("cached", &whatsmeow.Client{Store: &store.Device{}})
	result, err := service.ConversationIdentity(context.Background(), "cached", "fixture@lid")
	if err != nil || result.ResolutionComplete || len(result.Aliases) != 1 {
		t.Fatal("existing offline device without cache must return incomplete singleton")
	}
}
