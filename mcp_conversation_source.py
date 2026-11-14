"""Only installed, inspected operations. No invented unread endpoint fallback."""
import time
import hashlib
from urllib.parse import urlencode
from mcp_archive_models import CHAT, MESSAGE_ID, canonical
from mcp_conversation_models import unknown, valid_ref, operation
from mcp_contracts import instance_path


class ConversationSource:
    def __init__(self, bridge, profile, instance, *, phone_state=False,receipts_enabled=False):
        self.bridge, self.profile, self.instance = bridge, profile, instance
        self.identities = {}
        self.phone_state=phone_state
        self.receipts_enabled=receipts_enabled is True

    def authorize(self, profile, instance):
        if profile != self.profile or instance != self.instance:
            raise ValueError('access_denied')

    def snapshot(self, profile: str, instance: str, *, cursor=None, limit=50):
        self.authorize(profile, instance)
        if cursor is not None or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('invalid_arguments')
        return dict(conversations=[], cursor=None, revision=None, observed_at=None,
                    complete=False, reason=unknown()['reason'])

    def identity(self, profile, instance, chat, *, timeout=5):
        self.authorize(profile, instance)
        if not isinstance(chat, str) or not CHAT.fullmatch(chat): raise ValueError('invalid_arguments')
        cached = self.identities.get(chat)
        if cached and cached[0] > time.monotonic(): return cached[1]
        identity = dict(chat=chat, aliases=[chat], resolution_complete=chat.endswith('@g.us'))
        if not chat.endswith('@g.us'):
            try:
                value = self.bridge.call(instance_path(instance, 'chat/conversation-read-snapshot') + '?' +
                    urlencode(dict(source='identity',chat=chat)), timeout=min(5,timeout))
                aliases = value.get('aliases')
                canonical = value.get('canonical_chat')
                if (value.get('requested_chat') == chat and value.get('resolution_complete') is True
                        and isinstance(aliases,list) and len(aliases)==2 and chat in aliases
                        and all(isinstance(alias,str) and CHAT.fullmatch(alias) for alias in aliases)
                        and len({alias.rsplit('@',1)[1] for alias in aliases})==2
                        and {alias.rsplit('@',1)[1] for alias in aliases}=={'s.whatsapp.net','lid'}
                        and canonical in aliases and canonical.endswith('@s.whatsapp.net')):
                    identity = dict(chat=canonical,aliases=sorted(aliases),resolution_complete=True)
            except Exception:
                pass
        if len(self.identities)>=1000: self.identities.clear()
        expires=time.monotonic()+(60 if identity['resolution_complete'] else 5)
        for alias in identity['aliases']: self.identities[alias]=(expires,identity)
        return identity

    def state(self, profile, instance, chat, anchor, *, timeout=20):
        self.authorize(profile,instance)
        output=dict(unread_count=None,marked_unread=None,unread_refs=None,read_state=unknown())
        if not self.phone_state or anchor is None: return output
        if (not isinstance(chat,str) or not CHAT.fullmatch(chat) or not isinstance(anchor,dict)
                or not isinstance(anchor.get('id'),str) or not MESSAGE_ID.fullmatch(anchor['id'])
                or type(anchor.get('timestamp')) is not int or not 0<anchor['timestamp']<4133980800
                or type(anchor.get('from_me')) is not bool): raise ValueError('invalid_arguments')
        deadline=time.monotonic()+timeout
        identity=self.identity(profile,instance,chat,timeout=min(5,timeout))
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0: return output
            value=self.bridge.call(instance_path(instance,'chat/conversation-read-snapshot')+'?'+urlencode(dict(
                source='phone',chat=chat,anchor_id=anchor['id'],anchor_timestamp=anchor['timestamp'],
                anchor_from_me=str(anchor['from_me']).lower())),timeout=min(20,remaining))
            if not isinstance(value,dict) or value.get('chat') not in identity['aliases']: return output
            count,marked=value.get('unreadCount'),value.get('markedAsUnread')
            if count is not None and (type(count) is not int or not 0<=count<=4294967295): return output
            if marked is not None and type(marked) is not bool: return output
            status='unread' if (count is not None and count>0) or marked is True else (
                'read' if count==0 and marked is False else 'unknown')
            revision='phone:'+hashlib.sha256(canonical(dict(chat=chat,count=count,marked=marked))).hexdigest()
            output.update(unread_count=count,marked_unread=marked,read_state=dict(value=status,
                source='whatsapp_phone',revision=revision,observed_at=int(time.time()),
                reason='message_ids_unavailable' if status=='unread' else ('optional_fields_missing' if status=='unknown' else None)))
        except Exception:
            output['read_state']=unknown('phone_read_state_unavailable')
        return output

    def mark_read(self, profile: str, instance: str, refs: list[dict], operation_id: str):
        self.authorize(profile, instance)
        operation(operation_id)
        if not isinstance(refs, list) or len(refs) > 100: raise ValueError('invalid_arguments')
        for ref in refs: valid_ref(ref, instance)
        items, batches = [], {}
        for ref in refs:
            if ref['from_me']:
                status, reason = 'not_applicable', None
            elif ref['chat'].endswith('@g.us') and not ref['participant']:
                status, reason = 'failed', 'group_sender_unavailable'
            else:
                status, reason = 'failed', 'receipt_not_started'
                batches.setdefault((ref['chat'], ref['participant']), []).append(len(items))
            items.append(dict(ref=ref, status=status, reason=reason))
        if not self.receipts_enabled:
            batches.clear()
            for item in items:
                if item['status']!='not_applicable':
                    item.update(status='failed',reason='selective_read_not_verified')
        deadline = time.monotonic() + 10
        for indexes in batches.values():
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            payloads = []
            for index in indexes:
                ref = items[index]['ref']
                payload = dict(remoteJid=ref['chat'], id=ref['id'], played=False)
                if ref['participant']: payload['sender'] = ref['participant']
                payloads.append(payload)
            try:
                # v1.5.0 swallows MarkRead errors and returns {} with HTTP 200.
                # Even a successful HTTP response cannot establish submission.
                self.bridge.call(instance_path(instance, 'chat/read-messages'), 'POST',
                                 {'readMessages': payloads}, timeout=min(5, remaining))
                status, reason = 'uncertain', 'upstream_receipt_result_unavailable'
            except Exception:
                status, reason = 'uncertain', 'receipt_response_unavailable'
            for index in indexes: items[index].update(status=status, reason=reason)
        statuses = {item['status'] for item in items if item['status'] != 'not_applicable'}
        status = next(iter(statuses)) if len(statuses) == 1 else ('partial' if statuses else 'not_applicable')
        result=dict(status=status, items=items, operation_id=operation_id)
        if not self.receipts_enabled and statuses: result['reason']='selective_read_not_verified'
        return result
