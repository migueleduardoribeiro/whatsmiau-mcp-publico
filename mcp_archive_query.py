"""Bounded private Drive queries. Empty results never prove absent WhatsApp history."""
import hashlib
import hmac
import json
import threading
import time
import re
from collections import OrderedDict
from datetime import datetime, timezone
from mcp_archive_catalog import ArchiveCatalog, index_current, WINDOW_SECONDS
from mcp_archive_models import canonical, PARTICIPANT
from mcp_drive_transport import DriveError
from mcp_store import StateError
from contextlib import nullcontext
from functools import wraps


class BudgetExceeded(Exception): pass


def bounded_query(function):
    @wraps(function)
    def execute(self, *args, **kwargs):
        transport = getattr(self.drive, 'transport', None)
        with transport.scope(45) if transport is not None else nullcontext():
            return function(self, *args, **kwargs)
    return execute


class ArchiveQuery:
    def __init__(self, drive, *, cursor_store, clock=time.time, health=None, schedule_index=None):
        self.drive, self.cursors, self.clock, self.health = drive, cursor_store, clock, health
        self.cache, self.cache_size, self.cache_lock = OrderedDict(), 0, threading.RLock()
        self.local, self.last_scan = threading.local(), {}
        self.catalog = ArchiveCatalog(drive, reader=self._node)
        self.schedule_index = schedule_index

    @staticmethod
    def _time_buckets(manifest, start, end):
        buckets = manifest.get('buckets', {})
        index = manifest.get('time_index')
        if not index_current(manifest) or index.get('complete') is not True:
            return buckets
        slots = index.get('slots')
        if not isinstance(slots, dict): return buckets
        chosen = set()
        try:
            for slot, prefixes in slots.items():
                if (not isinstance(slot, str) or not re.fullmatch(r'-?\d{1,15}', slot)
                        or not isinstance(prefixes, list) or any(not isinstance(p, str) or not re.fullmatch(r'[a-f0-9]{2}', p) for p in prefixes)):
                    return buckets
                at = int(slot) * WINDOW_SECONDS
                if at < end and at + WINDOW_SECONDS > start: chosen.update(prefixes)
        except (ValueError, OverflowError, OSError): return buckets
        return {prefix: identifier for prefix, identifier in buckets.items() if prefix in chosen}

    def _begin(self):
        self.local.scan = {'bytes': 0, 'batches': 0}
        self.local.reads, self.local.manifests = {}, {}
        self.local.soft_deadline = None

    def _manifest(self, instance, chat):
        key = (instance,chat)
        if key not in self.local.manifests:
            self.local.manifests[key] = self.catalog.manifest(instance,chat)
        return self.local.manifests[key]

    def _raw(self, identifier, maximum=1048576):
        if self.local.soft_deadline is not None and time.monotonic() >= self.local.soft_deadline:
            raise BudgetExceeded()
        if identifier in self.local.reads: return self.local.reads[identifier]
        metadata = self.drive.metadata(identifier)
        size = int(metadata.get('size', -1))
        budget = self.local.scan
        if size < 0 or size > maximum: raise DriveError('archive_invalid_index')
        if budget['batches'] >= 100 or budget['bytes'] + size > 8388608: raise BudgetExceeded()
        budget['batches'] += 1; budget['bytes'] += size
        signature = metadata.get('sha256Checksum') or metadata.get('appProperties', {}).get('sha256')
        cache_key = (identifier, signature)
        with self.cache_lock:
            for key in list(self.cache):
                if self.cache[key][0] <= self.clock(): self.cache_size -= len(self.cache.pop(key)[1])
            cached = self.cache.get(cache_key) if signature else None
            if cached:
                self.cache.move_to_end(cache_key)
                self.local.reads[identifier] = cached[1]
                return cached[1]
        raw = self.drive.read(identifier, maximum)
        if len(raw) != size: raise DriveError('archive_invalid_index')
        if signature and not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), signature):
            raise DriveError('archive_integrity_failed')
        with self.cache_lock:
            if signature:
                previous = self.cache.pop(cache_key, None)
                if previous: self.cache_size -= len(previous[1])
                self.cache[cache_key] = (self.clock() + 300, raw); self.cache_size += len(raw)
                while self.cache_size > 8388608: self.cache_size -= len(self.cache.popitem(last=False)[1][1])
        self.local.reads[identifier] = raw
        return raw

    def _node(self, identifier):
        try:
            node = json.loads(self._raw(identifier))
            if not isinstance(node, dict): raise ValueError()
            return node
        except (ValueError, UnicodeError): raise DriveError('archive_invalid_index') from None

    def _current_aliases(self, instance, chats, message_id):
        records = [self.catalog.current(instance, chat, message_id, manifest=self._manifest(instance,chat)[1])
                   for chat in sorted(chats)]
        records = [record for record in records if record]
        return max(records, key=lambda record: (record['kind'] == 'delete', bool(record.get('is_edit')),
                   record.get('order', 0), record['chat']), default=None)

    @bounded_query
    def messages(self, profile, instance, chats, start, end, limit, cursor=None, received_only=False):
        if not 1 <= len(chats) <= 2 or type(limit) is not int or not 1 <= limit <= 100 or start >= end or type(received_only) is not bool:
            raise ValueError('invalid_arguments')
        self._begin()
        started = time.monotonic()
        binding = dict(owner=profile, instance=instance, chats=sorted(chats), start=start, end=end, limit=limit)
        if received_only: binding['received_only'] = True
        if cursor:
            try: state = self.cursors.consume_ticket(cursor, 'archive_cursor', profile)
            except StateError: raise ValueError('invalid_cursor') from None
            if state.get('binding') != binding: raise ValueError('invalid_cursor')
        else:
            state = dict(binding=binding, todo=[], active=None, coverage={})
            # A batch prepared by an older writer may have changed a node
            # before its manifest was committed. Replay it before trusting an index.
            safe_index = self.health is None or self.health().get('prepared_batches', 0) == 0
            for chat in sorted(chats):
                _, manifest = self._manifest(instance, chat)
                index = manifest.get('time_index')
                if self.schedule_index and not manifest.get('time_index_disabled') and not (index_current(manifest) and index.get('complete') is True):
                    self.schedule_index(instance, chat)
                buckets = self._time_buckets(manifest, start, end) if safe_index else manifest.get('buckets', {})
                for _, identifier in sorted(buckets.items(), reverse=True):
                    state['todo'].append({'id': identifier, 'chat': chat})
                state['coverage'].update(manifest.get('coverage', {}))
        self.local.soft_deadline = started + 30
        messages, output_size, exhausted = [], 0, False
        while (state['todo'] or state['active']) and len(messages) < limit:
            active = state['active'] or dict(state['todo'].pop(), offset=0, signature=None)
            state['active'] = active
            try: raw = self._raw(active['id'])
            except BudgetExceeded: break
            signature = hashlib.sha256(raw).hexdigest()
            if active['signature'] and active['signature'] != signature: raise ValueError('archive_changed_retry')
            active['signature'] = signature
            try: node = json.loads(raw)
            except (ValueError, UnicodeError): raise DriveError('archive_invalid_index') from None
            if 'children' in node:
                state['todo'].extend({'id': identifier, 'chat': active['chat']}
                                     for _, identifier in sorted(node['children'].items(), reverse=True))
                state['active'] = None
                continue
            records = sorted(node.get('records', {}).values(), key=lambda item: (item.get('timestamp') or 0, item['message_id']), reverse=True)
            position = active['offset']
            while position < len(records) and len(messages) < limit:
                record = records[position]
                if len(chats) > 1:
                    try: current = self._current_aliases(instance, chats, record['message_id'])
                    except BudgetExceeded:
                        exhausted = True; break
                    if current and current['chat'] != active['chat']:
                        position += 1; continue
                timestamp = record.get('timestamp')
                if record['kind'] == 'delete' or type(timestamp) is not int or not start <= timestamp < end:
                    position += 1; continue
                if received_only and record.get('from_me'):
                    position += 1; continue
                if record['chat'] != active['chat'] or record['instance'] != instance: raise DriveError('archive_invalid_index')
                media = record.get('media') or {}
                attachment = {key: media[key] for key in ('state', 'kind', 'mime_type', 'filename', 'size') if key in media}
                message = {'id': record['message_id'], 'timestamp': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                    'from_me': bool(record['from_me']), 'text': str(record['text'])[:16384],
                    'has_image': media.get('kind') == 'image' and media.get('state') == 'archived',
                    'attachments': [attachment] if media else []}
                participant = record.get('participant')
                if isinstance(participant, str) and PARTICIPANT.fullmatch(participant):
                    message['participant'] = participant
                participant_name = record.get('participant_name')
                if isinstance(participant_name, str) and participant_name.strip():
                    message['participant_name'] = participant_name.strip()[:256]
                if record.get('location'):
                    message['location'] = dict(record['location'], received_at=datetime.fromtimestamp(
                        record['observed_at'], timezone.utc).isoformat())
                size = 3 * len(json.dumps(message, ensure_ascii=True).encode()) + 256
                if output_size + size > 2097152:
                    exhausted = True; break
                messages.append(message); output_size += size; position += 1
            active['offset'] = position
            if position == len(records): state['active'] = None
            if exhausted: break
        cursor_out = None
        if state['todo'] or state['active']:
            if len(canonical(state)) > 65536: raise ValueError('archive_query_too_broad')
            cursor_out = self.cursors.ticket('archive_cursor', dict(state, owner=profile), 900)
        coverage = {'capture_started_at': None, 'last_committed_at': None, 'known_gaps': []}
        coverage.update(state['coverage'])
        if self.health:
            coverage.update({key: value for key, value in self.health().items() if key in coverage})
        self.last_scan = dict(self.local.scan)
        messages.sort(key=lambda message: (message['timestamp'], message['id']), reverse=True)
        return {'mensagens': messages, 'partial': True, 'cursor': cursor_out, 'source': 'drive_archive', 'coverage': coverage,
                'reason': 'Somente mensagens capturadas/importadas. Para encontrar as últimas, consulte todas as páginas. Para saber se existe uma resposta recebida, uma ocorrência basta; se não houver ocorrência e existir cursor, continue. Resultado vazio não comprova ausência no WhatsApp.'}

    @bounded_query
    def refs_for_messages(self, profile, instance, chats, messages):
        from mcp_conversation_query import reference
        self._begin()
        if len(messages)>100 or not 1<=len(chats)<=2: raise ValueError('invalid_arguments')
        refs=[]
        for message in messages:
            record=self._current_aliases(instance,chats,message['id'])
            if not record or record['kind']=='delete': raise ValueError('archive_changed_retry')
            if message.get('from_me') is not None and record['from_me']!=message['from_me']:
                raise ValueError('archive_changed_retry')
            refs.append(reference(record))
        return refs

    @bounded_query
    def resolve_refs(self, profile, instance, refs):
        from mcp_conversation_models import valid_ref
        self._begin()
        if not isinstance(refs, list) or len(refs) > 100: raise ValueError('invalid_arguments')
        for ref in refs: valid_ref(ref, instance)
        output = []
        for ref in refs:
            record = self._current_aliases(instance, {ref['chat']}, ref['id'])
            if (not record or record['kind'] == 'delete' or record['revision'] != ref['revision']
                    or record.get('timestamp') != ref['timestamp'] or record['from_me'] != ref['from_me']
                    or record.get('participant') != ref['participant']):
                raise ValueError('archive_changed_retry')
            media = record.get('media') or {}
            message = dict(id=ref['id'], destinatario=ref['chat'],
                tipo_conversa='group' if ref['chat'].endswith('@g.us') else 'individual',
                timestamp=datetime.fromtimestamp(ref['timestamp'], timezone.utc).isoformat(),
                from_me=ref['from_me'], text=str(record['text'])[:16384],
                has_image=media.get('kind') == 'image' and media.get('state') == 'archived',
                attachments=[{k: media[k] for k in ('state','kind','mime_type','filename','size') if k in media}] if media else [])
            for key in ('participant', 'participant_name', 'location'):
                if record.get(key): message[key] = record[key]
            output.append(message)
        return output

    @bounded_query
    def audio_reference(self, profile, instance, chats, message_id):
        from mcp_transcription_store import TranscriptionError
        self._begin()
        if not 1 <= len(chats) <= 2: raise ValueError('invalid_arguments')
        record = self._current_aliases(instance, chats, message_id)
        media = (record or {}).get('media') or {}
        if (not record or record['kind'] == 'delete' or record['instance'] != instance or record['chat'] not in chats
                or media.get('kind') != 'audio' or media.get('state') != 'archived'):
            raise TranscriptionError('audio_reference_unavailable')
        if type(media.get('size')) is not int or not 0 < media['size'] <= 10485760:
            raise TranscriptionError('audio_limit_exceeded')
        return dict(chat=record['chat'], message_id=message_id, **{k: media[k] for k in ('drive_id','sha256','size','mime_type')})

    @bounded_query
    def image(self, profile, instance, chat, message_id, chats=None):
        self._begin()
        aliases = set(chats or {chat})
        if chat not in aliases or not 1 <= len(aliases) <= 2: raise ValueError('invalid_arguments')
        record = self._current_aliases(instance, aliases, message_id)
        media = (record or {}).get('media') or {}
        if not record or record['kind'] == 'delete' or media.get('kind') != 'image' or media.get('state') != 'archived':
            raise ValueError('image_reference_unavailable')
        if record['chat'] not in aliases or record['instance'] != instance: raise DriveError('archive_invalid_index')
        if not 0 < media.get('size', 0) <= 5242880: raise ValueError('invalid_file_size')
        data = self.drive.read(media['drive_id'], 5242880)
        if len(data) != media['size'] or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), media['sha256']):
            raise ValueError('media_integrity_failed')
        return {'data': data, 'mime_type': media['mime_type'], 'caption': record['text'],
                'timestamp': datetime.fromtimestamp(record['timestamp'], timezone.utc).isoformat()}
