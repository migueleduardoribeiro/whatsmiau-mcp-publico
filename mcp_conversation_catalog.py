"""Discover the existing private archive, including legacy hash-named folders."""
import re
import time
from contextlib import nullcontext
from functools import wraps
from mcp_archive_catalog import hash_id
from mcp_archive_models import canonical, CHAT
from mcp_archive_query import BudgetExceeded
from mcp_conversation_models import unknown
from mcp_store import StateError
from mcp_drive_transport import DriveError


def bounded_catalog(function):
    @wraps(function)
    def execute(self,*args,**kwargs):
        transport=getattr(self.archive.drive,'transport',None)
        remaining=30
        if kwargs.get('shared_budget'):
            remaining=max(.01,self.archive.local.soft_deadline-time.monotonic())
        with transport.scope(remaining) if transport is not None else nullcontext():
            return function(self,*args,**kwargs)
    return execute


class ConversationCatalog:
    def __init__(self, archive, source):
        self.archive, self.source, self.cursors = archive, source, archive.cursors

    def identity(self, profile, instance, chat):
        remaining=getattr(self.archive.local,'soft_deadline',time.monotonic()+30)-time.monotonic()
        if remaining<=0: raise BudgetExceeded()
        return self.source.identity(profile,instance,chat,timeout=min(5,remaining))

    def _conversation(self, instance, folder, include_state=True):
        identity = folder.get('appProperties', {}).get('archiveIdentity', '')
        if not re.fullmatch(r'chat:[a-f0-9]{64}', identity): return None
        found = self.archive.drive.lookup(folder['id'], 'current-v1')
        if not found: return None
        manifest = self.archive._node(found['id'])
        meta = manifest.get('conversation')
        anchor=None
        if not isinstance(meta, dict) or (include_state and self.source.phone_state):
            # Legacy manifests have no plaintext chat metadata. Read one valid
            # current-index leaf; never historical batches or media payloads.
            pending = list(manifest.get('buckets', {}).values())
            visited = set()
            while pending and len(visited) < 65:
                identifier = pending.pop()
                if identifier in visited: raise ValueError('archive_invalid_index')
                visited.add(identifier)
                node = self.archive._node(identifier)
                if 'children' in node:
                    pending.extend(node['children'].values())
                    continue
                for record in node.get('records', {}).values():
                    if record.get('instance') != instance: raise ValueError('archive_invalid_index')
                    if not isinstance(meta,dict): meta = dict(chat=record.get('chat'), last_message_at=record.get('timestamp'))
                    if record.get('chat')!=meta.get('chat'): raise ValueError('archive_invalid_index')
                    if record.get('kind')!='delete' and record.get('timestamp'):
                        anchor=dict(id=record.get('message_id'),timestamp=record.get('timestamp'),from_me=record.get('from_me'))
                        break
                if anchor is not None or (isinstance(meta,dict) and not (include_state and self.source.phone_state)): break
        chat = (meta or {}).get('chat')
        if not isinstance(chat, str) or not CHAT.fullmatch(chat) or identity != 'chat:' + hash_id(chat):
            raise ValueError('archive_invalid_index')
        return dict(chat=chat, aliases=[chat], name=None,
            kind='group' if chat.endswith('@g.us') else 'individual',
            last_message_at=meta.get('last_message_at'), unread_count=None, marked_unread=None,
            read_state=unknown(), unread_refs=None, _anchor=anchor)

    @bounded_catalog
    def list(self, profile: str, instance: str, *, unread_only=False, cursor=None, limit=50, shared_budget=False, include_state=True):
        self.source.authorize(profile, instance)
        if type(limit) is not int or not 1 <= limit <= 50 or type(unread_only) is not bool:
            raise ValueError('invalid_arguments')
        if not shared_budget:
            self.archive._begin()
            self.archive.local.soft_deadline = time.monotonic() + 30
        binding = dict(instance=instance, limit=limit, unread_only=unread_only,include_state=include_state)
        if cursor:
            try:
                state = self.cursors.peek_ticket(cursor, 'conversation_catalog', profile)
                if state['binding'] != binding: raise StateError('binding')
                self.cursors.consume_ticket(cursor, 'conversation_catalog', profile)
            except (StateError, KeyError): raise ValueError('invalid_cursor') from None
        else:
            source = self.source.snapshot(profile, instance)
            folder = self.archive.drive.lookup(self.archive.drive.root_id, 'instance:' + hash_id(instance))
            state = dict(owner=profile, binding=binding, folder=folder['id'] if folder else None,
                         page=None, started=False, todo=[], pending=source['conversations'],
                         source_complete=source['complete'], revision=source['revision'],
                         observed_at=source['observed_at'], seen=[], gaps=[])
        output, unknowns = [], []
        while len(output) + len(unknowns) < limit:
            if time.monotonic()>=self.archive.local.soft_deadline: break
            row = None
            if state['pending']:
                row = state['pending'].pop(0)
            elif state['todo']:
                try: row = self._conversation(instance, state['todo'][0],include_state)
                except BudgetExceeded: break
                except (ValueError, DriveError):
                    state['gaps'].append('archive_conversation_unavailable')
                state['todo'].pop(0)
                if row is None: continue
            elif state['folder'] and (not state['started'] or state['page']):
                page = self.archive.drive.list(state['folder'], state['page'])
                state['started'] = True; state['page'] = page.get('nextPageToken')
                state['todo'] = [dict(id=item['id'], appProperties=item.get('appProperties', {}))
                                 for item in page.get('files', [])
                                 if item.get('mimeType') == 'application/vnd.google-apps.folder']
                if not state['todo'] and state['page']: continue
                if not state['todo']: break
                continue
            else: break
            if row is None: continue
            try: identity=self.identity(profile,instance,row['chat'])
            except BudgetExceeded:
                state['pending'].insert(0,row)
                break
            row=dict(row,chat=identity['chat'],aliases=identity['aliases'])
            if any(alias in state['seen'] for alias in row['aliases']): continue
            anchor=row.pop('_anchor',None)
            if include_state and self.source.phone_state:
                remaining=self.archive.local.soft_deadline-time.monotonic()
                if remaining<=0:
                    row['_anchor']=anchor
                    state['pending'].insert(0,row)
                    break
                row.update(self.source.state(profile,instance,row['chat'],anchor,timeout=min(20,remaining)))
            state['seen'].extend(row['aliases'])
            if unread_only and row['read_state']['value'] == 'unknown': unknowns.append(row)
            elif not unread_only or row['read_state']['value'] == 'unread': output.append(row)
        more = bool(state['pending'] or state['todo'] or state['page'] or (state['folder'] and not state['started']))
        next_cursor = None
        if more:
            if len(canonical(state)) > 65536: raise ValueError('archive_query_too_broad')
            next_cursor = self.cursors.ticket('conversation_catalog', state, 900)
        return dict(conversations=output, unknown_conversations=unknowns, cursor=next_cursor,
                    revision=state['revision'], observed_at=state['observed_at'],
                    complete=state['source_complete'] and not more and not state['gaps'],
                    archive_complete=not more and not state['gaps'],
                    reason=None if state['source_complete'] else ('whatsapp_conversation_inventory_partial'
                        if include_state and self.source.phone_state else 'whatsapp_read_state_unverified'),
                    coverage=dict(source='drive_archive_and_whatsapp_state', partial=True, known_gaps=state['gaps']))
