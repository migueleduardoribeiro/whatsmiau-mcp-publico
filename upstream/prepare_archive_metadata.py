"""Add explicit privacy metadata to the pinned WhatsMiau v1.5.0 source only.

The upstream serializer unwraps view-once messages but drops the wrapper flag.
This patch adds JSON metadata; it does not change pairing or message delivery.
"""
from pathlib import Path
import argparse

HELPER = '''package whatsmiau

import "go.mau.fi/whatsmeow/types/events"

func archiveFlags(event *events.Message) (bool, bool) {
    return event.IsViewOnce, event.IsEdit
}
'''

TEST = '''package whatsmiau

import (
    "encoding/json"
    "testing"
    "go.mau.fi/whatsmeow/types/events"
    "go.mau.fi/whatsmeow/proto/waE2E"
)

func TestArchiveMetadata(t *testing.T) {
    inner := &waE2E.Message{ImageMessage: &waE2E.ImageMessage{}}
    cases := []struct { name string; raw *waE2E.Message; once bool; edit bool }{
        {"ordinary", inner, false, false},
        {"once-v1", &waE2E.Message{ViewOnceMessage: &waE2E.FutureProofMessage{Message: inner}}, true, false},
        {"once-v2", &waE2E.Message{ViewOnceMessageV2: &waE2E.FutureProofMessage{Message: inner}}, true, false},
        {"once-extension", &waE2E.Message{ViewOnceMessageV2Extension: &waE2E.FutureProofMessage{Message: inner}}, true, false},
        {"edited", &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: inner}}, false, true},
    }
    for _, fixture := range cases { t.Run(fixture.name, func(t *testing.T) {
        event := (&events.Message{RawMessage: fixture.raw}).UnwrapRaw()
        once, edit := archiveFlags(event)
        if once != fixture.once || edit != fixture.edit { t.Fatal("wrapper metadata lost") }
        payload, err := json.Marshal(&WookMessageData{IsViewOnce: once, IsEdit: edit})
        if err != nil { t.Fatal(err) }
        var body map[string]any
        if err = json.Unmarshal(payload, &body); err != nil { t.Fatal(err) }
        value, ok := body["isViewOnce"].(bool)
        if !ok || value != fixture.once { t.Fatal("explicit view-once boolean required, including false") }
    }) }
}
'''


def apply(source, *, tests_only=False):
    source = Path(source).resolve()
    directory = source / 'lib/whatsmiau'
    models = directory / 'models.go'
    emitter = directory / 'event_emitter.go'
    model_text, event_text = models.read_text(), emitter.read_text()
    if tests_only:
        (directory / 'archive_metadata_test.go').write_text(TEST)
        return
    field = '\tMessageTimestamp int                     `json:"messageTimestamp,omitempty"`'
    assignment = '\t\tMessageTimestamp: int(ts.Unix()),'
    returning = '\treturn &WookMessageData{\n\t\tKey:              key,'
    additions = ('\n\tIsViewOnce       bool                    `json:"isViewOnce"`'
                 '\n\tIsEdit           bool                    `json:"isEdit,omitempty"`')
    if 'IsViewOnce       bool' not in model_text:
        if model_text.count(field) != 1 or event_text.count(returning) != 1 or event_text.count(assignment) != 1:
            raise ValueError('Pinned upstream source does not match; no files changed')
        model_text = model_text.replace(field, field + additions, 1)
        event_text = event_text.replace(returning, '\tisViewOnce, isEdit := archiveFlags(e)\n' + returning, 1)
        event_text = event_text.replace(assignment, assignment + '\n\t\tIsViewOnce:       isViewOnce,\n\t\tIsEdit:           isEdit,', 1)
        models.write_text(model_text)
        emitter.write_text(event_text)
    elif 'archiveFlags(e)' not in event_text:
        raise ValueError('Unexpected partial upstream patch')
    (directory / 'archive_metadata.go').write_text(HELPER)
    (directory / 'archive_metadata_test.go').write_text(TEST)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('--tests-only', action='store_true')
    options = parser.parse_args()
    apply(options.source, tests_only=options.tests_only)
