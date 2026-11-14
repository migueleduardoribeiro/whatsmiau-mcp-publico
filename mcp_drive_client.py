"""Drive REST operations limited to this application's private archive tree."""
import json
import re
from urllib.parse import urlencode, urlsplit, parse_qs
from mcp_drive_transport import DriveError

FIELDS = 'id,name,mimeType,size,parents,trashed,ownedByMe,shared,appProperties,sha256Checksum,md5Checksum'
FOLDER = 'application/vnd.google-apps.folder'


def file_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise DriveError('drive_invalid_id')
    return value


class DriveClient:
    def __init__(self, oauth, transport):
        self.oauth, self.transport, self.root_id = oauth, transport, None

    def _request(self, method, path, **kwargs):
        headers = dict(kwargs.pop('headers', {}), Authorization='Bearer ' + self.oauth.access_token())
        result = self.transport.request(method, path, headers=headers, **kwargs)
        if not 200 <= result.status < 300: raise DriveError('drive_api_failed', result.status)
        return result

    def _metadata(self, identifier):
        return self._request('GET', '/api/drive/v3/files/' + file_id(identifier) + '?' + urlencode({'fields': FIELDS})).json()

    def metadata(self, identifier):
        if not self.root_id: raise DriveError('drive_archive_uninitialized')
        target = current = self._metadata(identifier)
        visited = set()
        for _ in range(12):
            current_id = current.get('id')
            if (not current_id or current_id in visited or current.get('trashed')
                    or current.get('shared') or current.get('ownedByMe') is not True):
                raise DriveError('drive_file_denied')
            visited.add(current_id)
            if current_id == self.root_id: return target
            parents = current.get('parents', [])
            if len(parents) != 1: raise DriveError('drive_file_denied')
            current = self._metadata(parents[0])
        raise DriveError('drive_file_denied')

    def create_id(self):
        result = self._request('GET', '/api/drive/v3/files/generateIds?count=1&space=drive&type=files').json()
        try: return file_id(result['ids'][0])
        except (KeyError, IndexError, TypeError): raise DriveError('drive_invalid_response') from None

    def create_folder(self, name, parent, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', identity):
            raise DriveError('drive_invalid_identity')
        if parent is None:
            if name != 'LylaHub WhatsMiau' or identity != 'archive-root-v1' or self.root_id:
                raise DriveError('drive_file_denied')
        else:
            metadata = self.metadata(parent)
            if metadata.get('mimeType') != FOLDER: raise DriveError('drive_file_denied')
        query = "trashed=false and mimeType='" + FOLDER + "' and appProperties has {key='archiveIdentity' and value='" + identity + "'}"
        if parent is not None: query += " and '" + file_id(parent) + "' in parents"
        result = self._request('GET', '/api/drive/v3/files?' + urlencode({'q': query, 'pageSize': 100, 'fields': 'files(' + FIELDS + '),nextPageToken'})).json()
        if result.get('nextPageToken'): raise DriveError('drive_folder_ambiguous')
        matches = result.get('files', [])
        if matches:
            created = sorted(matches, key=lambda item: item['id'])[0]
        else:
            metadata = dict(id=self.create_id(), name=name, mimeType=FOLDER, appProperties={'archiveIdentity': identity})
            if parent: metadata['parents'] = [parent]
            created = self._request('POST', '/api/drive/v3/files?' + urlencode({'fields': FIELDS}),
                headers={'Content-Type': 'application/json'}, body=json.dumps(metadata).encode()).json()
        if created.get('shared') or created.get('ownedByMe') is not True: raise DriveError('drive_file_denied')
        identifier = file_id(created.get('id'))
        if parent is None: self.root_id = identifier
        return identifier

    def list(self, parent, page_token=None):
        self.metadata(parent)
        arguments = {'q': "trashed=false and '" + file_id(parent) + "' in parents", 'pageSize': 100,
                     'fields': 'nextPageToken,files(' + FIELDS + ')', 'orderBy': 'name'}
        if page_token:
            if not isinstance(page_token, str) or len(page_token) > 4096: raise DriveError('drive_invalid_cursor')
            arguments['pageToken'] = page_token
        return self._request('GET', '/api/drive/v3/files?' + urlencode(arguments)).json()

    def lookup(self, parent, identity):
        self.metadata(parent)
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', identity):
            raise DriveError('drive_invalid_identity')
        query = ("trashed=false and '" + file_id(parent) + "' in parents and appProperties has "
                 "{key='archiveIdentity' and value='" + identity + "'}")
        result = self._request('GET', '/api/drive/v3/files?' + urlencode({
            'q': query, 'pageSize': 2, 'fields': 'nextPageToken,files(' + FIELDS + ')'})).json()
        matches = result.get('files', [])
        if len(matches) > 1 or result.get('nextPageToken'): raise DriveError('drive_index_ambiguous')
        if not matches: return None
        return self.metadata(matches[0]['id'])

    def read(self, identifier, maximum):
        metadata = self.metadata(identifier)
        if metadata.get('mimeType') == FOLDER: raise DriveError('drive_file_denied')
        size = metadata.get('size')
        if size is None or not str(size).isdigit() or int(size) > maximum: raise DriveError('drive_response_limit')
        result = self._request('GET', '/api/drive/v3/files/' + file_id(identifier) + '?alt=media', maximum=maximum)
        if len(result.data) != int(size): raise DriveError('drive_invalid_response')
        return result.data

    def put(self, identifier, parent, metadata, source, size):
        if self.metadata(parent).get('mimeType') != FOLDER: raise DriveError('drive_file_denied')
        return self._upload('POST', '/upload/drive/v3/files', dict(metadata, id=file_id(identifier), parents=[parent]), source, size)

    def download(self, identifier, target, maximum):
        metadata = self.metadata(identifier)
        size = metadata.get('size')
        if (metadata.get('mimeType') == FOLDER or size is None or not str(size).isdigit()
                or int(size) > maximum): raise DriveError('drive_response_limit')
        before = target.tell()
        self._request('GET', '/api/drive/v3/files/' + file_id(identifier) + '?alt=media', maximum=maximum, sink=target)
        received = target.tell() - before
        if received != int(size): raise DriveError('drive_invalid_response')
        return received

    def replace(self, identifier, metadata, source, size):
        self.metadata(identifier)
        return self._upload('PATCH', '/upload/drive/v3/files/' + file_id(identifier), metadata, source, size)

    def _upload(self, method, path, metadata, source, size):
        if type(size) is not int or not 0 <= size <= 10485760: raise DriveError('drive_request_limit')
        allowed = {'id', 'name', 'mimeType', 'parents', 'appProperties'}
        if not set(metadata) <= allowed: raise DriveError('drive_invalid_metadata')
        start = self._request(method, path + '?' + urlencode({'uploadType': 'resumable', 'fields': FIELDS}),
            headers={'Content-Type': 'application/json', 'X-Upload-Content-Length': str(size),
                     'X-Upload-Content-Type': metadata.get('mimeType', 'application/octet-stream')}, body=json.dumps(metadata).encode())
        location = start.headers.get('location', '')
        parsed = urlsplit(location)
        if (parsed.scheme != 'https' or parsed.netloc != 'www.googleapis.com'
                or parsed.path != path or parsed.fragment or not parse_qs(parsed.query).get('upload_id')):
            raise DriveError('drive_invalid_upload_session')
        source.seek(0)
        reply = self._request('PUT', parsed.path + '?' + parsed.query,
            headers={'Content-Type': metadata.get('mimeType', 'application/octet-stream')}, body=source, size=size)
        result = reply.json()
        if result.get('shared') or result.get('trashed') or result.get('ownedByMe') is not True:
            raise DriveError('drive_file_denied')
        return result

    def trash(self, identifier):
        self.metadata(identifier)
        return self._request('PATCH', '/api/drive/v3/files/' + file_id(identifier),
                             headers={'Content-Type': 'application/json'}, body=b'{"trashed":true}').json()
