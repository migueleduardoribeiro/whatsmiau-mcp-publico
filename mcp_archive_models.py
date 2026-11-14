"""Explicit v1.5.0 webhook projection; messages are data, never instructions."""
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode

INSTANCE = re.compile(r'[A-Za-z0-9_-]{1,100}')
CHAT = re.compile(r'(?:[1-9][0-9]{4,29}@(s\.whatsapp\.net|lid)|[1-9][0-9]{4,29}(?:-[0-9]{1,20})?@g\.us)')
PARTICIPANT = re.compile(r'[1-9][0-9]{4,19}@(s\.whatsapp\.net|lid)')
MESSAGE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,256}')
MEDIA_FIELDS = ('url', 'mimetype', 'fileLength', 'fileName', 'caption', 'mediaKey',
                'fileSha256', 'fileEncSha256', 'directPath')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def event_identity(event):
    return hashlib.sha256(canonical([event['instance'], event['chat'], event['message_id'], event['revision']])).hexdigest()


def location_from_message(raw):
    """Project WhatsMiau v1.5 coordinates; never follow sender-supplied URLs."""
    for key, kind in (('locationMessage', 'static'), ('liveLocationMessage', 'live')):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, dict):
            return None
        # Go's omitempty removes numeric zero coordinates from the webhook.
        latitude, longitude = value.get('degreesLatitude', 0), value.get('degreesLongitude', 0)
        if any(type(v) not in (float, int) or abs(v) > bound or not math.isfinite(v)
               for v, bound in ((latitude, 90), (longitude, 180))):
            return None
        location = {'type': kind, 'latitude': latitude, 'longitude': longitude,
                    'map_url': 'https://www.google.com/maps/search/?' + urlencode(
                        {'api': 1, 'query': f'{latitude},{longitude}'})}
        for field in (('name', 'address') if kind == 'static' else ('caption',)):
            text = value.get(field)
            if isinstance(text, str) and len(text) <= 4096:
                location[field] = text
        if kind == 'live':
            location['position_status'] = 'last_received'
            accuracy = value.get('accuracyInMeters')
            if type(accuracy) is int and 0 <= accuracy <= 4294967295:
                location['accuracy_meters'] = accuracy
        return location
    return None


def normalize_event(body: dict, instance: str) -> list[dict]:
    if (not isinstance(instance, str) or not INSTANCE.fullmatch(instance)
            or not isinstance(body, dict) or body.get('instance') != instance):
        raise ValueError('archive_instance_mismatch')
    name = body.get('event')
    if name not in ('messages.upsert', 'messages.set', 'messages.delete'):
        # messages.update carries delivery/read receipts, not replacement text.
        return []
    data = body.get('data')
    items = data if name == 'messages.set' else [data]
    if not isinstance(items, list) or len(items) > 1000:
        raise ValueError('archive_invalid_event')
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get('instanceId', instance) != instance:
            raise ValueError('archive_invalid_event')
        if item.get('isViewOnce') is True:
            continue
        deleting = name == 'messages.delete'
        key = item if deleting else item.get('key')
        if not isinstance(key, dict):
            raise ValueError('archive_invalid_key')
        chat, identifier, direction = key.get('remoteJid'), key.get('id'), key.get('fromMe', False)
        if not isinstance(chat, str) or not CHAT.fullmatch(chat):
            continue
        if not isinstance(identifier, str) or not MESSAGE_ID.fullmatch(identifier) or type(direction) is not bool:
            raise ValueError('archive_invalid_key')
        raw = {} if deleting else item.get('message', {})
        if not isinstance(raw, dict):
            raise ValueError('archive_invalid_message')
        if any('viewonce' in key.lower() for key in raw) or raw.get('viewOnce') or item.get('viewOnce'):
            continue
        media = None
        excluded = False
        for kind in ('image', 'document', 'audio', 'video', 'sticker', 'ptv'):
            descriptor = raw.get(kind + 'Message')
            if descriptor is None:
                continue
            if not isinstance(descriptor, dict):
                raise ValueError('archive_invalid_media')
            if descriptor.get('viewOnce'):
                excluded = True
                break
            media = {'kind': kind}
            for field in MEDIA_FIELDS:
                value = descriptor.get(field)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 4096:
                        raise ValueError('archive_invalid_media')
                    media[field] = value
            break
        if excluded:
            continue
        author = {}
        if chat.endswith('@g.us'):
            participant = key.get('participant') or item.get('participant') or item.get('participantLid')
            if isinstance(participant, str) and PARTICIPANT.fullmatch(participant):
                author['participant'] = participant
            push_name = item.get('pushName')
            if isinstance(push_name, str):
                push_name = re.sub(r'[\x00-\x1f\x7f]', '', push_name).strip()[:256]
                if push_name:
                    author['participant_name'] = push_name
        extended = raw.get('extendedTextMessage', {})
        if not isinstance(extended, dict):
            raise ValueError('archive_invalid_message')
        text = raw.get('conversation') or extended.get('text') or (media or {}).get('caption') or ''
        if not isinstance(text, str) or len(text) > 16384:
            raise ValueError('archive_text_limit')
        timestamp = item.get('messageTimestamp') if not deleting else None
        if not deleting and (type(timestamp) is not int or not 0 < timestamp < 4133980800):
            raise ValueError('archive_invalid_timestamp')
        content = {'kind': 'delete' if deleting else 'upsert', 'text': text, 'media': media,
                   'is_edit': item.get('isEdit') is True, **author}
        location = location_from_message(raw)
        if location is not None:
            content['location'] = location
        result.append(dict(v=1, instance=instance, chat=chat, message_id=identifier,
                           timestamp=timestamp, observed_at=time.time(), from_me=direction,
                           source='history_event' if name == 'messages.set' else 'webhook',
                           revision=hashlib.sha256(canonical(content)).hexdigest(), **content))
    return result
