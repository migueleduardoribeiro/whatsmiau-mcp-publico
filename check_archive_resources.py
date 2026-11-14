"""Linux-only synthetic memory check; network is a child localhost fixture only."""
import base64
import hashlib
import hmac
import json
import resource
import subprocess
import sys
import tempfile
import threading
from mcp_tools import result as mcp_result
import time
from pathlib import Path
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from server import Bridge, configure_mcp
from mcp_oauth import Principal
from mcp_drive_oauth import CALLBACK
from mcp_archive_catalog import ArchiveCatalog
from mcp_archive_query import ArchiveQuery
from mcp_archive_media import ArchiveMedia
from test_mcp_archive_worker import FakeDrive

CHILD = '''
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        left=int(self.headers['Content-Length'])
        while left:
            value=self.rfile.read(min(left,65536))
            if not value: break
            left-=len(value)
        data=b'{"key":{"id":"synthetic-receipt"}}'
        self.send_response(200); self.send_header('Content-Length',str(len(data)))
        self.end_headers(); self.wfile.write(data)
server=HTTPServer(('127.0.0.1',0),Handler)
print(server.server_port,flush=True); server.serve_forever()
'''


def main():
    child = subprocess.Popen([sys.executable, '-c', CHILD], stdout=subprocess.PIPE, text=True)
    try:
        port = int(child.stdout.readline())
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            directory = Path(temporary).resolve()
            credentials = directory / 'client.json'
            credentials.write_text(json.dumps({'web': {'client_id': 'synthetic', 'client_secret': 'synthetic', 'redirect_uris': [CALLBACK]}}))
            config = {'issuer': 'https://fixture.example', 'instance_id': 'fixture', 'allowed_user': 'owner',
                'state_path': str(directory / 'mcp.db'), 'clients': {}, 'enable_media': True, 'media_fetch_token': 'synthetic',
                'archive': {'directory': str(directory / 'archive'), 'credentials_path': str(credentials),
                    'owner_email': 'owner@example.test', 'proxy_token': 'x' * 48, 'hook_token': 'y' * 48,
                    'capture_enabled': True, 'trusted_view_once_metadata': True, 'relay_previous': False}}
            app = configure_mcp(config, Bridge('synthetic', 'http://127.0.0.1:' + str(port), 10))
            runtime = app.archive_runtime
            try:
                drive = FakeDrive(); runtime.drive = drive; runtime.worker.drive = drive
                runtime.worker.catalog = ArchiveCatalog(drive); runtime.store.state('root_id', 'root')
                runtime.query = ArchiveQuery(drive, cursor_store=app.oauth.store, health=runtime.store.health)
                app.tools.archive = runtime.query; app.tools.files.archive = runtime.query
                Cipher(algorithms.AES(b'x' * 32), modes.CBC(b'x' * 16)).decryptor()
                for index in range(8):
                    runtime.query.cache[('synthetic-' + str(index), 'hash')] = (time.time() + 300, bytes([index]) * 1048576)
                runtime.query.cache_size = 8388608
                idle = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # Independent immutable Drive fixture avoids fake in-memory writer races.
                query_drive = FakeDrive()
                records = [dict(v=1, instance='fixture', chat='15557654321@s.whatsapp.net',
                    message_id='stress-'+str(i), timestamp=1792245600, observed_at=1792245600,
                    kind='upsert', text='"'*16384, media=None, from_me=False, source='webhook', order=i)
                    for i in range(100)]
                ArchiveCatalog(query_drive).update('fixture', '15557654321@s.whatsapp.net', records,
                                                   lambda key: query_drive.create_id(), {})
                del records
                stress_query = ArchiveQuery(query_drive, cursor_store=app.oauth.store)
                query_stop, query_ready = threading.Event(), threading.Event()
                query_stats = {'iterations': 0, 'failed': False}
                def query_load():
                    try:
                        while not query_stop.is_set():
                            page=stress_query.messages('profile','fixture',{'15557654321@s.whatsapp.net'},0,2000000000,100)
                            encoded=json.dumps(mcp_result(page),ensure_ascii=False).encode()
                            assert len(encoded)<4194304
                            query_stats['iterations']+=1; query_ready.set()
                    except BaseException:
                        query_stats['failed']=True; query_ready.set()
                query_thread=threading.Thread(target=query_load,daemon=True); query_thread.start()
                assert query_ready.wait(10) and not query_stats['failed']
                app.tools.files.provider.fetch_authorized = lambda reference, maximum: b'x' * 10485760
                principal = Principal('profile', 'fixture', frozenset({'whatsapp.read', 'whatsapp.send'}))
                result = app.tools.call('enviar_documento', {'destinatario': '15551234567', 'operacao_id': 'synthetic',
                    'arquivo': {'file_id': 'synthetic', 'download_url': 'https://files.oaiusercontent.com/synthetic',
                                'file_name': 'synthetic.zip', 'mime_type': 'application/octet-stream'}}, principal)
                assert not result['isError']
                runtime.worker.step()
                messages = runtime.query.messages('profile', 'fixture', {'15551234567@s.whatsapp.net'}, 0, 2000000000, 50)
                assert len(messages['mensagens']) == 1
                assert not list(runtime.spool.iterdir())
                # Generate encrypted 10MiB input incrementally, not with large fixture buffers.
                encrypted_path = directory / 'encrypted-fixture'
                key = b'k' * 32
                expanded = HKDF(algorithm=hashes.SHA256(), length=112, salt=None, info=b'WhatsApp Document Keys').derive(key)
                cipher = Cipher(algorithms.AES(expanded[16:48]), modes.CBC(expanded[:16])).encryptor()
                padder = padding.PKCS7(128).padder()
                plaintext_hash, encrypted_hash = hashlib.sha256(), hashlib.sha256()
                mac = hmac.new(expanded[48:80], expanded[:16], hashlib.sha256)
                with encrypted_path.open('wb') as target:
                    for _ in range(160):
                        raw = b'z' * 65536; plaintext_hash.update(raw)
                        chunk = cipher.update(padder.update(raw)); target.write(chunk); encrypted_hash.update(chunk); mac.update(chunk)
                    final = cipher.update(padder.finalize()) + cipher.finalize()
                    target.write(final); encrypted_hash.update(final); mac.update(final)
                    trailer = mac.digest()[:10]; target.write(trailer); encrypted_hash.update(trailer)
                class Download:
                    def download_to(self, url, route, maximum, target):
                        with encrypted_path.open('rb') as source:
                            while chunk := source.read(65536): target.write(chunk)
                provider = ArchiveMedia(Download(), transfer_slot=runtime.transfer_slot, spool=runtime.spool)
                b64 = lambda value: base64.b64encode(value).decode()
                descriptor = {'kind': 'document', 'url': 'https://mmg.whatsapp.net/synthetic', 'fileLength': '10485760',
                    'mediaKey': b64(key), 'fileSha256': b64(plaintext_hash.digest()), 'fileEncSha256': b64(encrypted_hash.digest())}
                with provider.prepare({'media': descriptor}) as prepared:
                    assert prepared['state'] == 'archived' and prepared['size'] == 10485760
                    assert hashlib.file_digest(prepared['source'], 'sha256').digest() == plaintext_hash.digest()
                query_stop.set(); query_thread.join(10)
                assert not query_thread.is_alive() and not query_stats['failed']
                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                print(json.dumps({'idle_with_full_cache_kib': idle, 'archive_10mib_peak_kib': peak,
                    'under_96mib': peak < 98304, 'concurrent_large_query_iterations': query_stats['iterations'], 'fixture_send_accepted': True, 'in_place_10mib_verified': True}))
                assert peak < 98304, 'archive process exceeds existing 96MiB budget'
            finally:
                runtime.stop(); app.oauth.store.close()
    finally:
        child.terminate(); child.wait(timeout=5)


if __name__ == '__main__': main()
