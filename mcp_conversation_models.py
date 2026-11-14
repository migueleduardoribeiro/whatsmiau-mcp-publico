"""Metadata contracts. WhatsApp read state and archive coverage are independent."""
import re
from typing import Literal, TypedDict
from mcp_archive_models import CHAT, INSTANCE, MESSAGE_ID, PARTICIPANT


class MessageRef(TypedDict):
    instance: str
    chat: str
    id: str
    from_me: bool
    participant: str | None
    timestamp: int
    revision: str


class ReadState(TypedDict):
    value: Literal['read', 'unread', 'unknown']
    source: str
    revision: str | None
    observed_at: int | None
    reason: str | None


class Conversation(TypedDict):
    chat: str
    aliases: list[str]
    name: str | None
    kind: Literal['individual', 'group']
    last_message_at: int | None
    unread_count: int | None
    marked_unread: bool | None
    read_state: ReadState
    unread_refs: list[MessageRef] | None


class Snapshot(TypedDict):
    conversations: list[Conversation]
    cursor: str | None
    revision: str | None
    observed_at: int | None
    complete: bool
    reason: str | None


class Selection(TypedDict):
    refs: list[MessageRef]
    cursor: str | None
    selection_complete: bool
    scope_complete: bool
    coverage: dict
    read_revision: str | None
    read_state: ReadState


class ReadResult(TypedDict):
    status: Literal['confirmed', 'submitted', 'partial', 'uncertain', 'failed', 'not_applicable']
    items: list[dict]
    operation_id: str


def unknown(reason='whatsapp_read_state_unverified') -> ReadState:
    return dict(value='unknown', source='unavailable', revision=None, observed_at=None, reason=reason)


def valid_ref(ref, instance):
    if (not isinstance(ref, dict) or set(ref) != set(MessageRef.__annotations__)
            or ref.get('instance') != instance or not INSTANCE.fullmatch(instance)
            or not isinstance(ref.get('chat'), str) or not CHAT.fullmatch(ref['chat'])
            or not isinstance(ref.get('id'), str) or not MESSAGE_ID.fullmatch(ref['id'])
            or type(ref.get('from_me')) is not bool or type(ref.get('timestamp')) is not int
            or not 0 < ref['timestamp'] < 4133980800
            or not isinstance(ref.get('revision'), str) or not 1 <= len(ref['revision']) <= 256
            or (ref.get('participant') is not None and (not isinstance(ref['participant'], str)
                 or not PARTICIPANT.fullmatch(ref['participant'])))):
        raise ValueError('invalid_arguments')


def operation(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
        raise ValueError('invalid_arguments')
    return value


def output_schema(name):
    string = {'type': 'string'}; nullable = {'type': ['string','null']}
    read_state = {'type':'object','properties':{
        'value':{'enum':['read','unread','unknown']}, 'source':string, 'revision':nullable,
        'observed_at':{'type':['integer','null']},'reason':nullable},
        'required':['value','source','revision','observed_at','reason'],'additionalProperties':False}
    ref = {'type':'object','properties':{'instance':string,'chat':string,'id':string,
        'from_me':{'type':'boolean'},'participant':nullable,'timestamp':{'type':'integer'},'revision':string},
        'required':list(MessageRef.__annotations__),'additionalProperties':False}
    outcome = {'type':'object','properties':{
        'status':{'enum':['confirmed','submitted','partial','uncertain','failed','not_applicable']},
        'operation_id':string,'reason':nullable,
        'items':{'type':'array','items':{'type':'object','properties':{'ref':ref,'status':string,'reason':nullable},
            'required':['ref','status','reason'],'additionalProperties':False}}},
        'required':['status','items','operation_id'],'additionalProperties':False}
    common = dict(cursor=nullable,reason=nullable,coverage={'type':'object'},code=string)
    if name == 'read_result': return outcome
    if name == 'listar_conversas':
        row = {'type':'object','properties':dict(chat=string,aliases={'type':'array','items':string},name=nullable,
            kind={'enum':['individual','group']},last_message_at={'type':['integer','null']},
            unread_count={'type':['integer','null']},marked_unread={'type':['boolean','null']},
            read_state=read_state,unread_refs={'type':['array','null'],'items':ref}),
            'required':list(Conversation.__annotations__),'additionalProperties':False}
        common.update(conversations={'type':'array','items':row},unknown_conversations={'type':'array','items':row},
            complete={'type':'boolean'},archive_complete={'type':'boolean'},revision=nullable,
            observed_at={'type':['integer','null']})
    else:
        common.update(mensagens={'type':'array','items':{'type':'object','properties':dict(id=string,destinatario=string,
            nome={'type':['string','null']},tipo_conversa={'enum':['individual','group']},timestamp=string,
            from_me={'type':'boolean'},text=string,participant=string,participant_name=string,
            has_image={'type':'boolean'},attachments={'type':'array','items':{'type':'object'}},location={'type':'object'}),
            'required':['id','destinatario','tipo_conversa','timestamp','from_me','text'],'additionalProperties':False}},
            selection_complete={'type':'boolean'},scope_complete={'type':'boolean'},
            warning=string,fallback_mode={'enum':['available_messages']},
            read_revision=nullable,read_state=read_state,read_result=outcome)
    return {'type':'object','properties':common,'additionalProperties':False}
