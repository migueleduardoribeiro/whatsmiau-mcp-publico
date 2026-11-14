"""File validation and visual results. No network access without an approved provider.

A provider must pin public IPs, reject redirects, enforce streaming byte/time
limits, verify ChatGPT file authorization and clean temporary files in finally.
URL validation here is only an initial gate, not an SSRF boundary by itself.
"""
import base64
import json
import re
import struct
import zlib
from urllib.parse import urlsplit
from mcp_store import digest
from mcp_tools import recipient, text_field, result


class FileError(ValueError):
    pass


class FileReferenceError(FileError):
    def __init__(self, reason, host=None):
        super().__init__('invalid_file_reference')
        self.reason = reason
        self.host = host


def file_input_schema():
    return {'type': 'object', 'properties': {key: {'type': 'string'} for key in
            ('download_url', 'file_id', 'mime_type', 'file_name')},
            'required': ['download_url', 'file_id'], 'additionalProperties': False}


def validate_file_reference(reference, allowed_hosts):
    if not isinstance(reference, dict):
        raise FileReferenceError('file_reference_not_resolved')
    if (set(reference) - set(file_input_schema()['properties'])
            or any(not isinstance(value, str) for value in reference.values())):
        raise FileReferenceError('invalid_file_fields')
    identifier = reference.get('file_id', '')
    if not identifier.strip():
        raise FileReferenceError('missing_file_id')
    # The platform's ID is opaque, not a filename, URL or path to resolve.
    # It only participates in a JSON fingerprint; transport uses download_url.
    if len(identifier) > 4096 or re.search(r'[\x00-\x1f\x7f]', identifier):
        raise FileReferenceError('invalid_file_id')
    raw = reference.get('download_url', '')
    if not raw:
        raise FileReferenceError('missing_download_url')
    if raw.startswith(('/', 'sandbox:', 'file:')) or re.match(r'^[A-Za-z]:[\\/]', raw):
        raise FileReferenceError('local_file_reference')
    try:
        url = urlsplit(raw)
        if (len(raw) > 8192 or re.search(r'[\x00-\x20\x7f\\]', raw)
                or url.scheme != 'https'
                or url.username is not None or url.password is not None or url.port not in (None, 443)
                or url.fragment or not url.path.startswith('/')):
            raise ValueError()
    except ValueError:
        raise FileReferenceError('unsafe_download_url') from None
    if url.hostname not in allowed_hosts:
        raise FileReferenceError('download_host_not_allowed', url.hostname)
    return reference


def validate_file(data, mime, kind):
    maximum = 5 * 1024 * 1024 if kind == 'image' else 10 * 1024 * 1024
    if (kind not in ('image', 'document') or not isinstance(data, bytes) or len(data) > maximum
            or (kind == 'image' and not data)):
        raise FileError('invalid_file_size')
    if kind == 'document':
        # Documents are opaque attachments of any type. Do not unpack archives,
        # run code, or require PDF/Office signatures to forward a user's file.
        mime = mime or 'application/octet-stream'
        if (not isinstance(mime, str) or len(mime) > 128
                or not re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', mime)):
            raise FileError('invalid_file_type')
        return mime
    try:
        detected = image_type(data)
    except (ValueError, struct.error, zlib.error):
        raise FileError('invalid_file_type') from None
    if mime is not None and mime != detected:
        raise FileError('invalid_file_type')
    return detected


def image_type(data):
    """Validate complete PNG chunks or JPEG marker structure with bounded parsing."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        offset, have_data, first = 8, False, True
        decoder = zlib.decompressobj()
        budget = 0
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], 'big')
            kind = data[offset + 4:offset + 8]
            end = offset + 12 + length
            if end > len(data):
                raise ValueError()
            payload = data[offset + 8:end - 4]
            if zlib.crc32(kind + payload) & 0xffffffff != int.from_bytes(data[end - 4:end], 'big'):
                raise ValueError()
            if first:
                if kind != b'IHDR' or length != 13:
                    raise ValueError()
                width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
                depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
                if (not width or not height or width * height > 40_000_000 or depth not in depths.get(color, ())
                        or compression or filtering or interlace not in (0, 1)):
                    raise ValueError()
                budget = width * height * 8 + height * 8
            elif kind == b'IHDR':
                raise ValueError()
            if kind == b'IDAT':
                have_data = True
                pending = payload
                while pending:
                    output = decoder.decompress(pending, min(65536, budget + 1))
                    budget -= len(output)
                    if budget < 0:
                        raise ValueError()
                    pending = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise ValueError()
            if kind == b'IEND':
                if length or end != len(data) or not have_data or not decoder.eof:
                    raise ValueError()
                return 'image/png'
            offset, first = end, False
        raise ValueError()
    if data.startswith(b'\xff\xd8'):
        offset, frame, scan, tables = 2, False, False, set()
        while offset < len(data):
            if data[offset] != 255:
                raise ValueError()
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset >= len(data):
                raise ValueError()
            marker = data[offset]
            offset += 1
            if marker == 0xd9:
                if not frame or not scan or not {0xdb, 0xc4}.issubset(tables) or offset != len(data):
                    raise ValueError()
                return 'image/jpeg'
            if marker in (0, 0xd8) or 0xd0 <= marker <= 0xd7:
                raise ValueError()
            if offset + 2 > len(data):
                raise ValueError()
            length = int.from_bytes(data[offset:offset + 2], 'big')
            end = offset + length
            if length < 2 or end > len(data):
                raise ValueError()
            tables.add(marker)
            if marker in (0xc0, 0xc1, 0xc2):
                if length < 8:
                    raise ValueError()
                height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                components = data[offset + 7]
                if not width or not height or width * height > 40_000_000 or length != 8 + 3 * components:
                    raise ValueError()
                frame = True
            offset = end
            if marker == 0xda:
                if not frame or length < 6:
                    raise ValueError()
                scan, entropy = True, 0
                while offset < len(data):
                    if data[offset] != 255:
                        entropy += 1
                        offset += 1
                    elif offset + 1 < len(data) and (data[offset + 1] == 0 or 0xd0 <= data[offset + 1] <= 0xd7):
                        entropy += 1
                        offset += 2
                    else:
                        break
                if not entropy:
                    raise ValueError()
        raise ValueError()
    raise ValueError()


class FileGateway:
    def __init__(self, config, store, provider=None, archive=None, send_observer=None, image_analyzer=None):
        self.image_analyzer = image_analyzer
        self.config, self.store, self.provider = config, store, provider
        self.archive = archive
        self.send_observer = send_observer

    def _authorize(self, principal, scope):
        if principal.instance_id != self.config['instance_id'] or scope not in principal.scopes:
            raise FileError('access_denied')

    def send_file(self, principal, to, file_ref, kind, caption, operation_id):
        self._authorize(principal, 'whatsapp.send')
        jid = recipient(to)
        if self.provider is None:
            raise FileError('file_transport_unavailable')
        validate_file_reference(file_ref, self.provider.allowed_hosts)
        text_field(operation_id, 128)
        if not isinstance(caption, str) or len(caption) > 4096:
            raise FileError('invalid_caption')
        fingerprint = digest(json.dumps({'to': jid, 'kind': kind, 'file_id': file_ref['file_id'],
                                         'name': file_ref.get('file_name'), 'mime': file_ref.get('mime_type'),
                                         'caption': caption}, sort_keys=True))
        key = digest(principal.profile_id + ':' + principal.instance_id + ':' + operation_id)
        previous = self.store.lookup_send(key, fingerprint)
        if previous is not None:
            if previous['status'] != 'accepted':
                raise FileError('send_uncertain')
            return previous['receipt']
        maximum = (5 if kind == 'image' else 10) * 1024 * 1024
        data = self.provider.fetch_authorized(file_ref, maximum)
        mime = validate_file(data, file_ref.get('mime_type'), kind)
        filename = file_ref.get('file_name') or ('imagem.png' if mime == 'image/png' else
                                                 'imagem.jpg' if mime == 'image/jpeg' else
                                                 'documento.pdf' if mime == 'application/pdf' else 'arquivo.bin')
        if len(filename) > 128 or re.search(r'[\x00-\x1f\x7f/\\]', filename):
            raise FileError('invalid_filename')
        claimed = self.store.claim_send(key, fingerprint)
        if not claimed['new']:
            if claimed['status'] != 'accepted':
                raise FileError('send_uncertain')
            return claimed['receipt']
        try:
            upstream = self.provider.deliver(instance=principal.instance_id, recipient=jid, data=data,
                                             mime_type=mime, filename=filename, caption=caption, kind=kind)
            receipt = {'message_id': text_field(upstream.get('message_id'), 256), 'status': 'accepted'}
            self.store.finish_send(key, 'accepted', receipt)
        except Exception:
            self.store.finish_send(key, 'uncertain', {'status': 'uncertain'})
            raise FileError('send_uncertain') from None
        if self.send_observer:
            try: self.send_observer(dict(instance=principal.instance_id, chat=jid, message_id=receipt['message_id'],
                text=caption, data=data, media_kind=kind, mime_type=mime, filename=filename))
            except Exception: pass
        return receipt

    def read_image(self, principal, to, message_id, aliases=None):
        self._authorize(principal, 'whatsapp.read')
        jid = recipient(to)
        text_field(message_id, 256)
        if self.provider is None and self.archive is None:
            raise FileError('image_download_unavailable')
        try:
            image = (self.archive.image(principal.profile_id, principal.instance_id, jid, message_id, chats=aliases)
                     if self.archive is not None else self.provider.read_image(principal.instance_id, jid, message_id))
            mime = validate_file(image['data'], image['mime_type'], 'image')
            metadata = {'message_id': message_id, 'sender': jid,
                             'caption': str(image.get('caption', ''))[:4096],
                             'timestamp': str(image.get('timestamp', ''))[:40]}
            if self.image_analyzer is not None:
                from mcp_vision import VisionError
                try:
                    metadata['analysis'] = self.image_analyzer.analyze(image['data'], mime, principal.profile_id)
                except VisionError as error:
                    metadata['analysis'] = {'status': 'unavailable', 'code': error.code}
                    if error.retry_after: metadata['analysis']['retry_after'] = error.retry_after
            output = result(metadata)
            output['content'].append({'type': 'image', 'mimeType': mime,
                                      'data': base64.b64encode(image['data']).decode('ascii')})
            return output
        except (KeyError, TypeError):
            raise FileError('image_download_unavailable') from None


def hashlib_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()
