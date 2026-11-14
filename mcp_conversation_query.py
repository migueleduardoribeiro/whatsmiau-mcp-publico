"""Bounded global selection. Scanned candidates do not imply presentation or read."""
import hashlib
import time
from mcp_archive_models import canonical, CHAT
from mcp_archive_query import BudgetExceeded, bounded_query
from mcp_conversation_models import unknown, valid_ref
from mcp_store import StateError


def reference(record):
    return dict(instance=record['instance'], chat=record['chat'], id=record['message_id'],
                from_me=record['from_me'], participant=record.get('participant'),
                timestamp=record['timestamp'], revision=record['revision'])


class ConversationQuery:
    def __init__(self, archive, catalog, *, unread_fallback=False):
        self.archive, self.catalog, self.cursors, self.drive = archive, catalog, archive.cursors, archive.drive
        self.unread_fallback=unread_fallback

    def _resume(self, profile, cursor, binding, kind):
        try:
            state = self.cursors.peek_ticket(cursor, kind, profile)
            if state['binding'] != binding: raise StateError('binding')
            self.cursors.consume_ticket(cursor, kind, profile)
            return state
        except (StateError, KeyError): raise ValueError('invalid_cursor') from None

    def _pending(self, state):
        if len(canonical(state)) > 65536: raise ValueError('archive_query_too_broad')
        return self.cursors.ticket('conversation_recent', state, 900)

    def _validate(self, profile, instance, chats, limit):
        self.catalog.source.authorize(profile, instance)
        if type(limit) is not int or not 1 <= limit <= 100: raise ValueError('invalid_arguments')
        if chats is not None and (not isinstance(chats, list) or not 1 <= len(chats) <= 5
                or len(set(chats)) != len(chats)
                or any(not isinstance(jid, str) or not CHAT.fullmatch(jid) for jid in chats)):
            raise ValueError('invalid_arguments')

    @bounded_query
    def recent(self, profile: str, instance: str, *, chats=None, start: int, end: int, limit=50, cursor=None, _purpose='recent'):
        self._validate(profile, instance, chats, limit)
        if type(start) not in (int, float) or type(end) not in (int, float) or not 0 <= start < end <= 4133980800:
            raise ValueError('invalid_arguments')
        binding = dict(instance=instance, chats=sorted(chats) if chats else None, start=start, end=end, limit=limit,purpose=_purpose)
        self.archive._begin(); self.archive.local.soft_deadline = time.monotonic() + 30
        if cursor: state = self._resume(profile, cursor, binding, 'conversation_recent')
        else:
            identities={}
            for chat in chats or []:
                identity=self.catalog.identity(profile,instance,chat)
                identities[identity['chat']]=dict(chat=identity['chat'],aliases=identity['aliases'])
            state = dict(owner=profile, binding=binding, discovery=None, discovery_done=chats is not None,
                chats=list(identities.values()), identities=list(identities.values()), work=[], active=None,
                manifests={}, top=[], phase='scan', validate=[], scope_complete=chats is not None,
                coverage=dict(partial=True, known_gaps=[]),before=None,more_matches=False)
        try:
            while state['phase'] == 'scan':
                if state['active']:
                    active = state['active']
                    raw = self.archive._raw(active['id'])
                    signature = hashlib.sha256(raw).hexdigest()
                    if active['signature'] and signature != active['signature']: raise ValueError('archive_changed_retry')
                    active['signature'] = signature
                    import json
                    node = json.loads(raw)
                    if 'children' in node:
                        for identifier in node['children'].values():
                            state['work'].append(dict(id=identifier, chat=active['chat'],
                                canonical=active['canonical'], aliases=active['aliases'], offset=0, signature=None))
                        state['active'] = None
                        continue
                    records = list(node.get('records', {}).values())
                    while active['offset'] < len(records):
                        record = records[active['offset']]
                        if record.get('instance') != instance or record.get('chat') != active['chat']:
                            raise ValueError('archive_invalid_index')
                        if len(active['aliases']) > 1:
                            current = self.archive._current_aliases(instance, active['aliases'], record['message_id'])
                            if current and current['chat'] != record['chat']:
                                active['offset'] += 1; continue
                        timestamp = record.get('timestamp')
                        if record.get('kind') != 'delete' and type(timestamp) is int and start <= timestamp < end:
                            ref = reference(record); valid_ref(ref, instance)
                            position=(ref['timestamp'],active['canonical'],ref['id'])
                            if state['before'] is not None and position>=tuple(state['before']):
                                active['offset']+=1; continue
                            key = (active['canonical'], ref['id'])
                            state['top'] = [item for item in state['top'] if (item['canonical'], item['ref']['id']) != key]
                            state['top'].append(dict(canonical=active['canonical'], ref=ref))
                            state['top'].sort(key=lambda item: (item['ref']['timestamp'], item['canonical'], item['ref']['id']), reverse=True)
                            if len(state['top'])>limit: state['more_matches']=True
                            del state['top'][limit:]
                        active['offset'] += 1
                    state['active'] = None
                elif state['work']:
                    state['active'] = state['work'].pop()
                elif state['chats']:
                    row = state['chats'][0]
                    # Build work atomically: budget exhaustion must not drop a chat.
                    work, manifests = [], {}
                    for chat in row['aliases']:
                        _, manifest = self.archive._manifest(instance, chat)
                        manifests[chat] = hashlib.sha256(canonical(manifest)).hexdigest()
                        if chat in state['manifests'] and state['manifests'][chat]!=manifests[chat]:
                            raise ValueError('archive_changed_retry')
                        for identifier in self.archive._time_buckets(manifest, start, end).values():
                            work.append(dict(id=identifier, chat=chat, canonical=row['chat'],
                                             aliases=row['aliases'], offset=0, signature=None))
                        for gap in manifest.get('coverage', {}).get('known_gaps', []):
                            if gap not in state['coverage']['known_gaps']: state['coverage']['known_gaps'].append(gap)
                    state['manifests'].update(manifests); state['work'].extend(work); state['chats'].pop(0)
                elif not state['discovery_done']:
                    # Discovery shares the remaining per-call read budget.
                    page = self.catalog.list(profile, instance, limit=5, cursor=state['discovery'], shared_budget=True,include_state=False)
                    state['chats'] = [dict(chat=row['chat'], aliases=row['aliases']) for row in page['conversations']]
                    state['discovery'], state['discovery_done'] = page['cursor'], page['cursor'] is None
                    state['scope_complete'] = page['complete']
                    state['coverage']['known_gaps'].extend(page['coverage']['known_gaps'])
                else:
                    state['phase'], state['validate'] = 'validate', list(state['manifests'])
            while state['validate']:
                chat = state['validate'][0]
                # Drop per-request manifest memoization before final comparison.
                self.archive.local.manifests.pop((instance, chat), None)
                folder = self.archive.catalog.chat_folder(instance, chat)
                manifest_meta = self.archive.drive.lookup(folder, 'current-v1') if folder else None
                if manifest_meta: self.archive.local.reads.pop(manifest_meta['id'], None)
                _, current = self.archive._manifest(instance, chat)
                if hashlib.sha256(canonical(current)).hexdigest() != state['manifests'][chat]:
                    raise ValueError('archive_changed_retry')
                state['validate'].pop(0)
        except BudgetExceeded:
            return dict(refs=[], cursor=self._pending(state), selection_complete=False,
                scope_complete=state['scope_complete'], coverage=state['coverage'], read_revision=None,
                read_state=unknown(), reason='selection_pending')
        refs = [item['ref'] for item in state['top']]
        next_cursor=None
        if state['more_matches'] and state['top']:
            last=state['top'][-1]
            state.update(before=[last['ref']['timestamp'],last['canonical'],last['ref']['id']],more_matches=False,
                phase='scan',validate=[],top=[],work=[],active=None,discovery=None,discovery_done=chats is not None,
                chats=[dict(row) for row in state['identities']])
            next_cursor=self._pending(state)
        return dict(refs=refs, cursor=next_cursor, selection_complete=True, scope_complete=state['scope_complete'],
                    coverage=state['coverage'], read_revision=None, read_state=unknown(), reason='archive_coverage_partial')

    def unread(self, profile: str, instance: str, *, chats=None, limit=50, cursor=None):
        self._validate(profile, instance, chats, limit)
        if self.unread_fallback:
            end=int(time.time())
            if cursor:
                try: end=self.cursors.peek_ticket(cursor,'conversation_recent',profile)['binding']['end']
                except (StateError,KeyError): raise ValueError('invalid_cursor') from None
            selection=self.recent(profile,instance,chats=chats,start=1,end=end,limit=limit,cursor=cursor,_purpose='unread_available')
            selection.update(read_state=unknown('unread_state_unavailable'),reason='available_messages_unread_unconfirmed',
                fallback_mode='available_messages',warning='Não foi possível confirmar quais mensagens estão não lidas no WhatsApp. Abaixo estão apenas as mensagens disponíveis; elas podem já ter sido lidas.')
            return selection
        page = self.catalog.list(profile, instance, unread_only=True, limit=min(limit, 50), cursor=cursor)
        refs, missing = [], False
        for row in page['conversations']:
            if chats and not set(row['aliases']).intersection(chats): continue
            if row['unread_refs'] is None:
                missing = True; continue
            refs.extend(row['unread_refs'])
        # No default recent window and no invented last-N mapping from counts.
        refs.sort(key=lambda ref: (ref['timestamp'], ref['chat'], ref['id']), reverse=True)
        if len(refs) > limit: raise ValueError('archive_query_too_broad')
        return dict(refs=refs, cursor=page['cursor'], selection_complete=True,
                    scope_complete=page['complete'], coverage=dict(page['coverage'], unread_ids_missing=missing),
                    read_revision=page['revision'], read_state=unknown() if missing or page['unknown_conversations'] or not page['complete']
                    else dict(value='unread' if refs else 'read', source='whatsapp', revision=page['revision'],
                              observed_at=page['observed_at'], reason=None),
                    reason='unread_ids_unavailable' if missing else page['reason'])
