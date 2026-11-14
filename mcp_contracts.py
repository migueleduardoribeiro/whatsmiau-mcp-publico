"""Conservative capabilities of the installed WhatsMiau contract."""
from urllib.parse import quote


def instance_path(instance_id, operation):
    if operation not in ('contact', 'chat/syncMessages', 'chat/read-messages', 'message/text',
                         'message/document', 'message/image', 'group/fetchAllGroups',
                         'chat/conversation-read-snapshot'):
        raise ValueError('Unsupported upstream operation')
    return '/v1/instance/' + quote(instance_id, safe='') + '/' + operation


def capabilities_from_contract(contract):
    paths = contract.get('paths', {})
    return {
        'history_requires_reference': True,
        'history_sync': any('/syncMessages' in path for path in paths),
        'history_bootstrap': False,
        # Neither sendMedia nor an encrypted media URL is a download contract.
        'image_download': False,
        'send_text': '/v1/instance/{instance}/message/text' in paths,
        'send_image': '/v1/instance/{instance}/message/image' in paths,
        'send_document': '/v1/instance/{instance}/message/document' in paths,
    }


def inspect_contracts(bridge, instance_id):
    report = capabilities_from_contract(bridge.call('/v1/swagger.json'))
    contacts = bridge.call(instance_path(instance_id, 'contact'))
    report.update(contacts=isinstance(contacts, list),
                  contact_count=len(contacts) if isinstance(contacts, list) else None)
    status = bridge.call('/v1/instance/connectionState/' + quote(instance_id, safe=''))
    report['connected'] = (status.get('state') or status.get('instance', {}).get('state')) == 'open'
    return report
