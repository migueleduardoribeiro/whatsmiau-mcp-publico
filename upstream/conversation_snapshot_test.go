package whatsmiau

import (
	"bytes"
	"context"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waServerSync"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"
	"time"
)

// Any unexpected method call panics through the embedded nil interface.
// This proves the primitive reads keys only and never touches live AppState.
type conversationFixtureKeys struct{ store.AppStateSyncKeyStore }

func (conversationFixtureKeys) GetAppStateSyncKey(context.Context, []byte) (*store.AppStateSyncKey, error) {
	return &store.AppStateSyncKey{Data: bytes.Repeat([]byte{7}, 32)}, nil
}

type conversationUntouchableStore struct{ store.AppStateStore }
type conversationFixtureLIDs struct {
	store.LIDStore
	lid types.JID
}

func (s conversationFixtureLIDs) GetLIDForPN(context.Context, types.JID) (types.JID, error) {
	return s.lid, nil
}

func TestConversationSnapshotAuthenticatesWithoutLiveWrites(t *testing.T) {
	ctx := context.Background()
	live := &conversationUntouchableStore{}
	device := &store.Device{AppStateKeys: conversationFixtureKeys{}, AppState: live, LIDs: conversationFixtureLIDs{lid: types.NewJID("resolvedfixture", types.HiddenUserServer)}}
	encoder := appstate.NewProcessor(&store.Device{AppStateKeys: conversationFixtureKeys{}, AppState: &conversationShadowState{macs: make(map[string][]byte)}}, waLog.Noop)
	info := appstate.BuildMarkChatAsRead(types.NewJID("resolvedfixture", types.HiddenUserServer), false, time.Unix(100, 0), nil)
	pnInfo := appstate.BuildMarkChatAsRead(types.NewJID("fixture", types.DefaultUserServer), true, time.Unix(200, 0), nil)
	info.Mutations = append(info.Mutations, pnInfo.Mutations...)
	encoded, err := encoder.EncodePatch(ctx, []byte{1}, appstate.HashState{}, info)
	if err != nil {
		t.Fatal(err)
	}
	var patch waServerSync.SyncdPatch
	if err = proto.Unmarshal(encoded, &patch); err != nil {
		t.Fatal(err)
	}
	snapshot := &waServerSync.SyncdSnapshot{Version: &waServerSync.SyncdVersion{Version: proto.Uint64(1)}, Mac: patch.SnapshotMAC, KeyID: patch.KeyID}
	for _, mutation := range patch.Mutations {
		snapshot.Records = append(snapshot.Records, mutation.Record)
	}
	calls := 0
	fetch := func(_ context.Context, name appstate.WAPatchName, version uint64, wantSnapshot bool) (*appstate.PatchList, error) {
		calls++
		if name != appstate.WAPatchRegularLow || version != 0 || !wantSnapshot {
			t.Fatal("must request fresh baseline")
		}
		return &appstate.PatchList{Name: name, Snapshot: snapshot}, nil
	}
	result, err := fetchConversationReadSnapshot(ctx, device, fetch, "fixture@s.whatsapp.net")
	if err != nil {
		t.Fatal(err)
	}
	if calls != 1 || !result.Complete || result.Version != 1 || len(result.Boundaries) != 2 || result.Boundaries[0].Read == nil || *result.Boundaries[0].Read || result.Boundaries[0].Range.GetLastMessageTimestamp() != 100 || result.Boundaries[1].Chat != "fixture@s.whatsapp.net" || !*result.Boundaries[1].Read || len(result.QueriedAliases) != 2 {
		t.Fatalf("unexpected result: %#v", result)
	}
	if result.RequestedChat != "fixture@s.whatsapp.net" || result.ResolvedChat == nil || *result.ResolvedChat != "resolvedfixture@lid" || result.AliasResolution != "pn_to_lid" || result.Boundaries[0].Chat != "resolvedfixture@lid" {
		t.Fatal("authoritative PN/LID resolution lost")
	}
	if device.AppState != live {
		t.Fatal("live Device AppState replaced")
	}
	snapshot.Mac[0] ^= 1
	if result, err = fetchConversationReadSnapshot(ctx, device, fetch, "fixture@s.whatsapp.net"); err == nil || result != nil {
		t.Fatal("tampered snapshot must fail closed")
	}
}

func TestConversationAliasBoundariesKeepIndependentLatestFacts(t *testing.T) {
	mutation := func(chat string, read bool, ts int64) appstate.Mutation {
		return appstate.Mutation{Index: []string{appstate.IndexMarkChatAsRead, chat}, Operation: waServerSync.SyncdMutation_SET, Action: &waSyncAction.SyncActionValue{Timestamp: proto.Int64(ts), MarkChatAsReadAction: &waSyncAction.MarkChatAsReadAction{Read: proto.Bool(read)}}}
	}
	out := conversationReadBoundaries(nil, []appstate.Mutation{mutation("pn", false, 1), mutation("lid", false, 2), mutation("pn", true, 3), mutation("other", true, 4)}, "pn", "lid")
	if len(out) != 2 || out[0].Chat != "lid" || *out[0].Read || *out[0].ActionTimestamp != 2 || out[1].Chat != "pn" || !*out[1].Read || *out[1].ActionTimestamp != 3 {
		t.Fatal("aliases must retain independent latest facts")
	}
	remove := mutation("pn", false, 0)
	remove.Operation = waServerSync.SyncdMutation_REMOVE
	out = conversationReadBoundaries(out, []appstate.Mutation{remove}, "pn", "lid")
	if len(out) != 1 || out[0].Chat != "lid" {
		t.Fatal("one alias REMOVE must not erase another alias")
	}
	exact := conversationReadBoundaries(nil, []appstate.Mutation{mutation("pn", true, 3), mutation("lid", false, 2)}, "lid")
	if len(exact) != 1 || exact[0].Chat != "lid" {
		t.Fatal("exact LID must not broaden scope")
	}
}

func TestConversationSnapshotMissingMappingIsExplicitUnknown(t *testing.T) {
	for _, lids := range []store.LIDStore{nil, conversationFixtureLIDs{}} {
		called := false
		fetch := func(context.Context, appstate.WAPatchName, uint64, bool) (*appstate.PatchList, error) {
			called = true
			return nil, nil
		}
		result, err := fetchConversationReadSnapshot(context.Background(), &store.Device{AppStateKeys: conversationFixtureKeys{}, LIDs: lids}, fetch, "fixture@s.whatsapp.net")
		if err != nil || called || result.Complete || result.ResolvedChat != nil || len(result.Boundaries) != 0 || !strings.HasPrefix(result.AliasResolution, "mapping_") {
			t.Fatal("missing PN/LID mapping must remain unknown before querying")
		}
	}
	resolved, status, err := resolveConversationSnapshotTarget(context.Background(), nil, "fixture@g.us")
	if err != nil || resolved == nil || *resolved != "fixture@g.us" || status != "exact" {
		t.Fatal("group JID must remain exact")
	}
}

func TestConversationSnapshotRejectsOversizeBeforeDecode(t *testing.T) {
	fetch := func(context.Context, appstate.WAPatchName, uint64, bool) (*appstate.PatchList, error) {
		return &appstate.PatchList{Name: appstate.WAPatchRegularLow, Snapshot: &waServerSync.SyncdSnapshot{Records: make([]*waServerSync.SyncdRecord, 25001)}}, nil
	}
	result, err := fetchConversationReadSnapshot(context.Background(), &store.Device{AppStateKeys: conversationFixtureKeys{}}, fetch, "fixture@lid")
	if result != nil || err == nil || !strings.Contains(err.Error(), "resource limit") {
		t.Fatal("oversized snapshot must fail closed before decoding")
	}
}

func TestConversationBoundaryPresenceAndLatest(t *testing.T) {
	mutation := func(chat string, read *bool) appstate.Mutation {
		return appstate.Mutation{Index: []string{appstate.IndexMarkChatAsRead, chat}, Operation: waServerSync.SyncdMutation_SET, Action: &waSyncAction.SyncActionValue{MarkChatAsReadAction: &waSyncAction.MarkChatAsReadAction{Read: read}}}
	}
	for _, read := range []*bool{nil, proto.Bool(false), proto.Bool(true)} {
		out := conversationReadBoundaries(nil, []appstate.Mutation{mutation("authorized", read), mutation("other", proto.Bool(true))}, "authorized")
		if len(out) != 1 || out[0].Read != read {
			t.Fatal("presence lost or unrelated chat exposed")
		}
	}
	latest := conversationReadBoundaries(nil, []appstate.Mutation{mutation("authorized", proto.Bool(false)), mutation("authorized", proto.Bool(true))}, "authorized")
	if len(latest) != 1 || !*latest[0].Read {
		t.Fatal("latest SET must supersede previous snapshot record")
	}
	removed := mutation("authorized", nil)
	removed.Operation = waServerSync.SyncdMutation_REMOVE
	if len(conversationReadBoundaries(latest, []appstate.Mutation{removed}, "authorized")) != 0 {
		t.Fatal("REMOVE must invalidate boundary")
	}
}

func TestConversationShadowDoesNotAliasMACBuffers(t *testing.T) {
	ctx := context.Background()
	shadow := &conversationShadowState{macs: make(map[string][]byte)}
	input := []byte{2}
	_ = shadow.PutAppStateMutationMACs(ctx, "regular_low", 1, []store.AppStateMutationMAC{{IndexMAC: []byte{1}, ValueMAC: input}})
	input[0] = 9
	value, _ := shadow.GetAppStateMutationMAC(ctx, "regular_low", []byte{1})
	if value[0] != 2 {
		t.Fatal("input alias")
	}
	value[0] = 9
	value, _ = shadow.GetAppStateMutationMAC(ctx, "regular_low", []byte{1})
	if value[0] != 2 {
		t.Fatal("output alias")
	}
}

func TestConversationSnapshotRequiresKeysAndSnapshot(t *testing.T) {
	called := false
	fetch := func(context.Context, appstate.WAPatchName, uint64, bool) (*appstate.PatchList, error) {
		called = true
		return nil, nil
	}
	if _, err := fetchConversationReadSnapshot(context.Background(), nil, fetch, "authorized"); err == nil || called {
		t.Fatal("must reject absent device before networking")
	}
}
