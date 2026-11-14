"""Bounded Drive-only current-message index; one writer, immutable event logs."""
import hashlib
import io
import json
from datetime import datetime, timezone
from mcp_archive_models import canonical
from mcp_drive_transport import DriveError

MAXIMUM = 1048576
WINDOW_SECONDS = 900


def hash_id(value): return hashlib.sha256(value.encode()).hexdigest()


def index_current(manifest):
    index = manifest.get('time_index')
    coverage = manifest.get('coverage', {})
    return (isinstance(index, dict) and index.get('version') == 2 and isinstance(index.get('slots'), dict)
            and index.get('dirty', False) is False
            and 'coverage_revision' in index and isinstance(coverage, dict)
            and index['coverage_revision'] == coverage.get('last_committed_at'))


def index_record(index, prefix, record):
    timestamp = record.get('timestamp')
    if type(timestamp) is not int: return True
    try: datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError): return False
    prefixes = index['slots'].setdefault(str(timestamp // WINDOW_SECONDS), [])
    if not isinstance(prefixes, list): return False
    if prefix not in prefixes:
        prefixes.append(prefix); prefixes.sort()
    return True


def bound_index(manifest):
    # An index is optional: losing the optimization must never block capture.
    if 'time_index' in manifest and len(canonical(manifest)) > 900000:
        manifest.pop('time_index', None)
        manifest['time_index_disabled'] = True


class ArchiveCatalog:
    def __init__(self, drive, reader=None):
        self.drive, self.reader = drive, reader

    def chat_folder(self, instance, chat, *, create=False):
        parent = self.drive.root_id
        for identity, name in [('instance:' + hash_id(instance), 'instance-' + hash_id(instance)[:16]),
                               ('chat:' + hash_id(chat), 'chat-' + hash_id(chat)[:24])]:
            found = self.drive.lookup(parent, identity)
            if found: parent = found['id']
            elif create: parent = self.drive.create_folder(name, parent, identity)
            else: return None
        return parent

    def read_node(self, identifier):
        if self.reader: return self.reader(identifier)
        try: node = json.loads(self.drive.read(identifier, MAXIMUM))
        except (ValueError, UnicodeError): raise DriveError('archive_invalid_index') from None
        if not isinstance(node, dict): raise DriveError('archive_invalid_index')
        return node

    def _write(self, parent, identity, node, claim):
        payload = canonical(node)
        if len(payload) > MAXIMUM: raise DriveError('archive_index_limit')
        digest = hashlib.sha256(payload).hexdigest()
        previous = self.drive.lookup(parent, identity)
        metadata = {'name': identity + '.json', 'mimeType': 'application/json',
                    'appProperties': {'archiveIdentity': identity, 'sha256': digest}}
        if previous:
            if previous.get('appProperties', {}).get('sha256') == digest: return previous['id']
            self.drive.replace(previous['id'], metadata, io.BytesIO(payload), len(payload))
            return previous['id']
        identifier = claim(identity)
        try: self.drive.put(identifier, parent, metadata, io.BytesIO(payload), len(payload))
        except DriveError as error:
            if error.status != 409: raise
            stored = self.drive.metadata(identifier)
            if stored.get('appProperties', {}).get('sha256') != digest: raise DriveError('archive_upload_conflict')
        return identifier

    def manifest(self, instance, chat):
        folder = self.chat_folder(instance, chat)
        if not folder: return None, {'buckets': {}}
        manifest = self.drive.lookup(folder, 'current-v1')
        return folder, self.read_node(manifest['id']) if manifest else {'buckets': {}}

    def current(self, instance, chat, message_id, *, manifest=None):
        if manifest is None: _, manifest = self.manifest(instance, chat)
        digest = hash_id(message_id)
        identifier = manifest['buckets'].get(digest[:2])
        for depth in range(2, 65):
            if not identifier: return None
            node = self.read_node(identifier)
            if 'records' in node: return node['records'].get(digest)
            identifier = node.get('children', {}).get(digest[:depth + 1])
        raise DriveError('archive_invalid_index')

    def update(self, instance, chat, records, claim, coverage):
        folder = self.chat_folder(instance, chat, create=True)
        existing = self.drive.lookup(folder, 'current-v1')
        manifest = self.read_node(existing['id']) if existing else {'buckets': {}}
        if not existing:
            manifest['time_index'] = {'version': 2, 'complete': True, 'slots': {}, 'coverage_revision': None}
        elif 'time_index' in manifest and not index_current(manifest):
            manifest.pop('time_index')
        elif 'time_index' in manifest:
            # Publish invalidation before changing mutable nodes. If a write is
            # interrupted, readers fall back to a full scan until a rebuild.
            manifest['time_index']['dirty'] = True
            self._write(folder, 'current-v1', manifest, claim)
        for record in records:
            digest = hash_id(record['message_id'])
            prefix = digest[:2]
            manifest['buckets'][prefix] = self._update_node(folder, prefix, manifest['buckets'].get(prefix),
                                                           digest, record, claim)
            index = manifest.get('time_index')
            if index is not None and not index_record(index, prefix, record):
                manifest.pop('time_index', None); manifest['time_index_disabled'] = True
        manifest['coverage'] = coverage
        # Self-describing metadata lets discovery read legacy/current folders
        # without relying on contact lists or copying message bodies elsewhere.
        previous = manifest.get('conversation', {}).get('last_message_at') or 0
        latest = max((r.get('timestamp') or 0 for r in records), default=0)
        manifest['conversation'] = {'chat': chat, 'last_message_at': max(previous, latest) or None}
        if 'time_index' in manifest:
            manifest['time_index']['coverage_revision'] = coverage.get('last_committed_at')
            manifest['time_index'].pop('dirty', None)
        bound_index(manifest)
        self._write(folder, 'current-v1', manifest, claim)

    def _update_node(self, folder, prefix, identifier, digest, record, claim):
        node = self.read_node(identifier) if identifier else {'records': {}}
        if 'children' in node:
            child = digest[:len(prefix) + 1]
            node['children'][child] = self._update_node(folder, child, node['children'].get(child), digest, record, claim)
        else:
            previous = node['records'].get(digest)
            if previous:
                # A tombstone dominates stale history/upsert copies. Upstream does
                # not expose a reliable independent edit revision number.
                if previous['kind'] == 'delete' and record['kind'] != 'delete': return identifier
                if previous.get('is_edit') and not record.get('is_edit') and record['kind'] != 'delete': return identifier
                if record['kind'] != 'delete' and previous.get('order', 0) > record.get('order', 0): return identifier
                if record['source'] == 'history_event' and previous['source'] != 'history_event': return identifier
                record = dict(record, timestamp=previous.get('timestamp') or record.get('timestamp'))
                if not record.get('media') and previous.get('media') and record['kind'] != 'delete':
                    record['media'] = previous['media']
                if (record['kind'] != 'delete' and (previous.get('media') or {}).get('state') == 'archived'
                        and (record.get('media') or {}).get('state') != 'archived'):
                    record['media'] = previous['media']
            node['records'][digest] = record
            if len(canonical(node)) > MAXIMUM:
                if len(prefix) >= 64: raise DriveError('archive_index_limit')
                groups = {}
                for key, value in node['records'].items(): groups.setdefault(key[:len(prefix) + 1], {})[key] = value
                children = {key: self._write(folder, 'idx-' + key, {'records': value}, claim)
                            for key, value in groups.items()}
                node = {'children': children}
        return self._write(folder, 'idx-' + prefix, node, claim)
