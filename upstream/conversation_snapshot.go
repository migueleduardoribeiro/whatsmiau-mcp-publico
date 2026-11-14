package whatsmiau

// Diagnostic primitive only: no HTTP route is registered by this file.
// Never dispatch these mutations: doing so would write to the real stores.
import (
	"context"
	"errors"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waServerSync"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type conversationShadowState struct {
	version uint64
	hash    [128]byte
	macs    map[string][]byte
}

var _ store.AppStateStore = (*conversationShadowState)(nil)

func (s *conversationShadowState) PutAppStateVersion(_ context.Context, _ string, v uint64, h [128]byte) error {
	s.version = v
	s.hash = h
	return nil
}
func (s *conversationShadowState) GetAppStateVersion(context.Context, string) (uint64, [128]byte, error) {
	return s.version, s.hash, nil
}
func (s *conversationShadowState) DeleteAppStateVersion(context.Context, string) error {
	s.version = 0
	s.hash = [128]byte{}
	s.macs = make(map[string][]byte)
	return nil
}
func (s *conversationShadowState) PutAppStateMutationMACs(_ context.Context, _ string, _ uint64, entries []store.AppStateMutationMAC) error {
	for _, e := range entries {
		s.macs[string(e.IndexMAC)] = append([]byte(nil), e.ValueMAC...)
	}
	return nil
}
func (s *conversationShadowState) DeleteAppStateMutationMACs(_ context.Context, _ string, keys [][]byte) error {
	for _, k := range keys {
		delete(s.macs, string(k))
	}
	return nil
}
func (s *conversationShadowState) GetAppStateMutationMAC(_ context.Context, _ string, k []byte) ([]byte, error) {
	return append([]byte(nil), s.macs[string(k)]...), nil
}

type conversationPatchFetch func(context.Context, appstate.WAPatchName, uint64, bool) (*appstate.PatchList, error)

// ConversationReadBoundary is a server app-state fact, not an unread count.
// Read=nil means absent/unknown; a false value must remain distinguishable.
type ConversationReadBoundary struct {
	ActionTimestamp *int64                               `json:"action_timestamp"`
	Chat            string                               `json:"chat"`
	Read            *bool                                `json:"read"`
	Range           *waSyncAction.SyncActionMessageRange `json:"messageRange,omitempty"`
}
type ConversationSnapshot struct {
	RequestedChat   string                     `json:"requested_chat"`
	ResolvedChat    *string                    `json:"resolved_chat"`
	AliasResolution string                     `json:"alias_resolution"`
	QueriedAliases  []string                   `json:"queried_aliases"`
	Version         uint64                     `json:"version"`
	Boundaries      []ConversationReadBoundary `json:"boundaries"`
	Complete        bool                       `json:"collectionComplete"`
}

// FetchConversationReadSnapshot requests server state without applying it to
// the client. DecodePatches writes exclusively to a fresh RAM store. It does
// not mark messages, send receipts, request missing keys or reset checkpoints.
// target must be a previously authorized exact PN/LID chat identifier.
func FetchConversationReadSnapshot(ctx context.Context, client *whatsmeow.Client, target string) (*ConversationSnapshot, error) {
	if client == nil || client.Store == nil || !client.IsConnected() {
		return nil, errors.New("client unavailable")
	}
	return fetchConversationReadSnapshot(ctx, client.Store, client.DangerousInternals().FetchAppStatePatches, target)
}

func fetchConversationReadSnapshot(ctx context.Context, device *store.Device, fetch conversationPatchFetch, target string) (*ConversationSnapshot, error) {
	if device == nil || device.AppStateKeys == nil || target == "" {
		return nil, errors.New("device keys and exact chat required")
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	resolved, status, err := resolveConversationSnapshotTarget(ctx, device.LIDs, target)
	if err != nil {
		return nil, err
	}
	result := &ConversationSnapshot{RequestedChat: target, ResolvedChat: resolved, AliasResolution: status, Boundaries: []ConversationReadBoundary{}}
	// Missing PN mapping is unknown coverage, never an empty authoritative read
	// baseline. Do not silently fall back to PN when the server may index by LID.
	if resolved == nil {
		return result, nil
	}
	aliases := []string{*resolved}
	if status == "pn_to_lid" {
		aliases = append(aliases, target)
	}
	result.QueriedAliases = aliases
	// Processor only needs these two stores. Do not retain sessions, contacts,
	// Container or other writable stores on the shadow Device.
	shadow := store.Device{AppStateKeys: device.AppStateKeys, AppState: &conversationShadowState{macs: make(map[string][]byte)}}
	processor := appstate.NewProcessor(&shadow, waLog.Noop)
	state := appstate.HashState{}
	var totalRecords, totalBytes int
	// Limit pagination, fail closed rather than reporting a partial baseline.
	for page := 0; page < 32; page++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		patches, err := fetch(ctx, appstate.WAPatchRegularLow, state.Version, page == 0)
		if err != nil {
			return nil, err
		}
		if patches == nil || patches.Name != appstate.WAPatchRegularLow || (page == 0 && patches.Snapshot == nil) {
			return nil, errors.New("server snapshot missing or wrong collection")
		}
		if patches.Snapshot != nil {
			totalRecords += len(patches.Snapshot.GetRecords())
			totalBytes += proto.Size(patches.Snapshot)
		}
		for _, patch := range patches.Patches {
			totalRecords += len(patch.GetMutations())
			totalBytes += proto.Size(patch)
		}
		// Reject before decrypting/storing; never truncate a MAC-covered payload.
		if totalRecords > 25000 || totalBytes > 16*1024*1024 {
			return nil, errors.New("app-state diagnostic resource limit exceeded")
		}
		mutations, next, err := processor.DecodePatches(ctx, patches, state, true)
		if err != nil {
			return nil, err
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		result.Boundaries = conversationReadBoundaries(result.Boundaries, mutations, aliases...)
		result.Version = next.Version
		if !patches.HasMorePatches {
			result.Complete = true
			return result, nil
		}
		if next.Version <= state.Version {
			return nil, errors.New("app-state pagination did not advance")
		}
		state = next
	}
	return nil, errors.New("app-state pagination limit exceeded")
}

func resolveConversationSnapshotTarget(ctx context.Context, lids store.LIDStore, target string) (*string, string, error) {
	jid, err := types.ParseJID(target)
	if err != nil || jid.User == "" || jid.String() != target {
		return nil, "", errors.New("exact chat JID required")
	}
	if jid.Server == types.HiddenUserServer || jid.Server == types.GroupServer {
		return &target, "exact", nil
	}
	if jid.Server != types.DefaultUserServer {
		return nil, "", errors.New("unsupported chat JID")
	}
	if lids == nil {
		return nil, "mapping_unavailable", nil
	}
	lid, err := lids.GetLIDForPN(ctx, jid)
	if err != nil {
		return nil, "", errors.New("PN/LID mapping lookup failed")
	}
	if lid.IsEmpty() {
		return nil, "mapping_missing", nil
	}
	if lid.Server != types.HiddenUserServer || lid.User == "" || lid.Device != 0 {
		return nil, "", errors.New("invalid PN/LID mapping")
	}
	resolved := lid.String()
	return &resolved, "pn_to_lid", nil
}

func conversationReadBoundaries(out []ConversationReadBoundary, mutations []appstate.Mutation, targets ...string) []ConversationReadBoundary {
	for _, m := range mutations {
		if len(m.Index) != 2 || m.Index[0] != appstate.IndexMarkChatAsRead {
			continue
		}
		allowed := false
		for _, target := range targets {
			if m.Index[1] == target {
				allowed = true
				break
			}
		}
		if !allowed {
			continue
		}
		target := m.Index[1]
		// There is one latest record per index; REMOVE invalidates an earlier SET.
		for index := 0; index < len(out); index++ {
			if out[index].Chat == target {
				out = append(out[:index], out[index+1:]...)
				break
			}
		}
		if m.Operation == waServerSync.SyncdMutation_REMOVE {
			continue
		}
		action := m.Action.GetMarkChatAsReadAction()
		if action != nil {
			out = append(out, ConversationReadBoundary{Chat: target, Read: action.Read, Range: action.MessageRange, ActionTimestamp: m.Action.Timestamp})
		}
	}
	return out
}
