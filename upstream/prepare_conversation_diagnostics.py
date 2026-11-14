"""Install metadata-only read-state diagnostic on pinned WhatsMiau source.

Run after prepare_archive_metadata.py. No session files are inspected. The
endpoint is disabled unless WHATSMIAU_CONVERSATION_DIAGNOSTICS=1 at runtime.
"""
from pathlib import Path
import argparse


def apply(source):
    source = Path(source).resolve()
    assets = Path(__file__).resolve().parent
    route = source / 'server/routes/chat.go'
    text = route.read_text()
    anchor = '\tgroup.POST("/syncMessages", controller.SyncChatMessages)'
    addition = '\n\tgroup.GET("/conversation-read-snapshot", controller.ConversationReadSnapshot)'
    if text.count(anchor) != 1:
        raise ValueError('Pinned chat routes do not match; no files changed')
    if addition not in text:
        text = text.replace(anchor, anchor + addition, 1)
    sync_path = source / 'lib/whatsmiau/sync.go'
    sync_text = sync_path.read_text()
    waiter_anchor = '\tch     chan *events.HistorySync\n'
    if 'aliases []types.JID' not in sync_text and 'aliases ' not in sync_text:
        if sync_text.count(waiter_anchor) != 1:
            raise ValueError('Pinned waiter does not match; no files changed')
        sync_text = sync_text.replace(waiter_anchor, waiter_anchor + '\taliases []types.JID\n', 1)
    emitter_path = source / 'lib/whatsmiau/event_emitter.go'
    emitter_text = emitter_path.read_text()
    old_match = 'historySyncMatchesChat(hs, w.chat)'
    new_match = 'conversationHistorySyncMatchesWaiter(hs, w)'
    if new_match not in emitter_text:
        if emitter_text.count(old_match) != 1:
            raise ValueError('Pinned event matcher does not match; no files changed')
        emitter_text = emitter_text.replace(old_match, new_match, 1)
    field_anchor = '\tFromMe     *bool\n'
    request_fields = '\tmetadataOnly bool\n\tmetadataResult **ConversationHistoryMetadata\n\tanchorTimestamp time.Time\n'
    metadata_anchor = '\t\tvar sinceTS int64\n'
    metadata_intercept = '''\t\tif req.metadataOnly {
            if req.metadataResult != nil { *req.metadataResult = conversationHistoryMetadata(evt.Data.GetConversations(), chatJID.String()) }
            return nil, nil
        }
'''
    if 'metadataOnly' not in sync_text:
        if sync_text.count(field_anchor) != 1 or sync_text.count(metadata_anchor) != 1 or sync_text.count('anchorTS := time.Now().Unix()') != 1:
            raise ValueError('Pinned sync source does not match; no files changed')
        sync_text = sync_text.replace(field_anchor, field_anchor + request_fields, 1)
        sync_text = sync_text.replace('anchorTS := time.Now().Unix()', 'anchorTS := time.Now().Unix()\n\tif !req.anchorTimestamp.IsZero() { anchorTS = req.anchorTimestamp.Unix() }', 1)
        sync_text = sync_text.replace(metadata_anchor, metadata_intercept + metadata_anchor, 1)
    files = {
        'conversation_snapshot.go': 'lib/whatsmiau/conversation_snapshot.go',
        'conversation_service.go': 'lib/whatsmiau/conversation_service.go',
        'conversation_history.go': 'lib/whatsmiau/conversation_history.go',
        'conversation_exchange.go': 'lib/whatsmiau/conversation_exchange.go',
        'conversation_history_test.go': 'lib/whatsmiau/conversation_history_test.go',
        'conversation_history_exchange_test.go': 'lib/whatsmiau/conversation_history_exchange_test.go',
        'conversation_identity.go': 'lib/whatsmiau/conversation_identity.go',
        'conversation_identity_test.go': 'lib/whatsmiau/conversation_identity_test.go',
        'conversation_snapshot_test.go': 'lib/whatsmiau/conversation_snapshot_test.go',
        'conversation_controller.go': 'server/controllers/conversation_snapshot.go',
        'conversation_controller_test.go': 'server/controllers/conversation_snapshot_test.go',
    }
    contents = [(source / dest, (assets / name).read_text()) for name, dest in files.items()]
    route.write_text(text)
    sync_path.write_text(sync_text)
    emitter_path.write_text(emitter_text)
    for dest, body in contents:
        dest.write_text(body)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    apply(parser.parse_args().source)
